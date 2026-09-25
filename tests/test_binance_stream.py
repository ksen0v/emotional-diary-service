"""Поток исполнений и самопочинка агрегатора.

Поток быстрый, но теряет события на разрывах; надёжна сверка. Из этого следуют
два требования, и оба проверяются здесь: исполнение из потока должно давать
ровно ту же сделку, что и сверка, а исполнение, доехавшее позже соседних,
должно вставать на своё место, а не оставаться потерянным.
"""

import base64
import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import text

from eds.modules.source import repo as source_repo
from eds.modules.source.adapters.binance import mapping
from eds.modules.source.adapters.binance.source import BinanceSource
from eds.platform.config import settings
from tests.test_binance_connect import (
    BALANCE,
    connect,
    mock_binance,
    ms,
)
from tests.test_identity import csrf, register

pytestmark = pytest.mark.usefixtures("clean_users")

MASTER = base64.urlsafe_b64encode(b"B" * 32).decode()


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("EDS_SECRET_KEY", MASTER)
    settings.cache_clear()
    yield
    settings.cache_clear()


def order_event(
    trade_id: int,
    side: str,
    qty: str,
    price: str,
    pnl: str,
    at: dt.datetime | None = None,
) -> dict:
    at = at or dt.datetime.now(dt.UTC)
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": ms(at),
        "o": {
            "s": "SOLUSDT",
            "i": trade_id * 10,
            "t": trade_id,
            "S": side,
            "ps": "BOTH",
            "x": "TRADE",
            "X": "FILLED",
            "l": qty,
            "L": price,
            "n": "0.2",
            "N": "USDT",
            "rp": pnl,
            "T": ms(at),
        },
    }


async def connected(app_client: httpx.AsyncClient, factory):
    """Подключить Binance и вернуть подключение для прямой работы с источником."""
    await register(app_client)
    mock_binance(fills=[], income=[])
    res = await connect(app_client)
    assert res.status_code == 201, res.text
    connection_id = uuid.UUID(res.json()["connection"]["id"])
    async with factory() as s:
        row = await s.execute(
            text("SELECT user_id FROM source.connections WHERE id = :id"),
            {"id": connection_id},
        )
        user_id = row.scalar_one()
    return connection_id, user_id


@respx.mock
async def test_stream_fill_becomes_a_trade(app_client, factory) -> None:
    """Два исполнения из потока дают закрытую сделку — ту же, что дала бы сверка."""
    connection_id, user_id = await connected(app_client, factory)

    async with factory() as s:
        source = BinanceSource(
            None, s=s, connection_id=connection_id, user_id=user_id
        )
        open_event = order_event(7001, "BUY", "10", "150", "0")
        close_event = order_event(7002, "SELL", "10", "147", "-30.0")

        opened = await source.accept_fills(
            [(mapping.fill_from_stream(open_event), open_event)]
        )
        # Позиция открыта, но ещё не закрыта — сделки нет, и это правильно.
        assert opened == []

        closed = await source.accept_fills(
            [(mapping.fill_from_stream(close_event), close_event)]
        )
        await s.commit()

    assert len(closed) == 1
    trade = closed[0]
    assert trade.symbol == "SOLUSDT"
    assert trade.side == "long"
    # −30 результата минус комиссия по 0.2 с каждого исполнения.
    assert trade.profit_usd == Decimal("-30.4")


@respx.mock
async def test_duplicate_stream_fill_changes_nothing(app_client, factory) -> None:
    """Одно и то же исполнение приходит и потоком, и сверкой."""
    connection_id, user_id = await connected(app_client, factory)
    event = order_event(7101, "BUY", "5", "150", "0")

    async with factory() as s:
        source = BinanceSource(
            None, s=s, connection_id=connection_id, user_id=user_id
        )
        await source.accept_fills([(mapping.fill_from_stream(event), event)])
        assert source.fills_new == 1
        await source.accept_fills([(mapping.fill_from_stream(event), event)])
        assert source.fills_new == 0
        await s.commit()


@respx.mock
async def test_late_fill_rebuilds_the_position(app_client, factory) -> None:
    """Исполнение, доехавшее после закрытия позиции, пересобирает её целиком.

    Так выглядит пропущенный при разрыве филл: он старше границы, до которой
    агрегатор уже досчитал. Оставить его лежать значило бы держать в ленте
    сделку, собранную не из всех своих исполнений, — и никогда об этом
    не узнать.
    """
    connection_id, user_id = await connected(app_client, factory)

    # Времена заданы явно и по порядку: у потерянного исполнения своё время,
    # и после доезда оно встаёт между соседями, а не в конец. Собрать это
    # «по времени получения» значило бы получить другую сделку.
    base = dt.datetime.now(dt.UTC)

    def fill_row(trade_id: int, side: str, qty: str, price: str, pnl: str, sec: int):
        event = order_event(
            trade_id, side, qty, price, pnl, at=base + dt.timedelta(seconds=sec)
        )
        return mapping.fill_from_stream(event), event

    async with factory() as s:
        source = BinanceSource(
            None, s=s, connection_id=connection_id, user_id=user_id
        )
        # Поток принёс вход и выход, а доливку между ними потерял.
        await source.accept_fills([fill_row(7201, "BUY", "4", "150", "0", 0)])
        trades = await source.accept_fills(
            [fill_row(7203, "SELL", "8", "160", "40.0", 2)]
        )
        assert len(trades) == 1
        first = trades[0]
        # Без доливки выход выглядит как переворот: закрылось четыре, открылось
        # четыре в другую сторону. Собранная так сделка неверна, и узнать об
        # этом из неё самой нельзя.
        assert Decimal(first.raw["qty"]) == 4
        await s.commit()

    async with factory() as s:
        source = BinanceSource(
            None, s=s, connection_id=connection_id, user_id=user_id
        )
        # Пропущенная доливка между ними доезжает позже.
        rebuilt = await source.accept_fills([fill_row(7202, "BUY", "4", "170", "0", 1)])
        await s.commit()

    assert source.rebuilt_positions == ["SOLUSDT BOTH"]
    assert len(rebuilt) == 1
    # Тот же ключ сделки — значит в ленте она обновится, а не задвоится:
    # ключ собран из первого исполнения позиции и пересборку переживает.
    assert rebuilt[0].external_id == first.external_id
    # Вход стал средневзвешенным, объём — полным.
    assert Decimal(rebuilt[0].raw["entry_price"]) == 160
    assert Decimal(rebuilt[0].raw["qty"]) == 8


@respx.mock
async def test_stream_events_that_are_not_fills_are_skipped(
    app_client, factory
) -> None:
    await connected(app_client, factory)
    event = order_event(7301, "BUY", "1", "150", "0")
    event["o"]["x"] = "NEW"
    # Постановка ордера — не исполнение. Принимать её значило бы собирать
    # позицию из ордеров, которых не было.
    assert mapping.fill_from_stream(event) is None


@respx.mock
async def test_sync_keeps_a_balance_snapshot(app_client, factory) -> None:
    """Снимок баланса нужен, чтобы проценты от депозита были настоящими.

    У TMM кривая дня восстанавливается из процентов, посчитанных каждый от
    своей базы. Здесь база настоящая, и ради неё снимки и хранятся.
    """
    connection_id, _user_id = await connected(app_client, factory)
    await app_client.post("/api/v1/sync", headers=csrf(app_client))

    async with factory() as s:
        latest = await source_repo.latest_balance(s, connection_id)
    assert latest is not None
    assert str(latest.wallet_usdt) == BALANCE[0]["balance"] + ".0000000000"
