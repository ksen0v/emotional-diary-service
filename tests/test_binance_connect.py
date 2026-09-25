"""Подключение Binance ключом и приём сделок: сквозной путь через HTTP.

Биржа подменена, всё остальное настоящее: база, сессии, шифрование, агрегатор,
приём сделок, движок правил. Проверяется то, что на живом ключе проверить
нельзя без риска, — отказы по правам, отсутствие ключа и секрета в ответах API,
переключение источника и появление своей разметки.
"""

import base64
import datetime as dt

import httpx
import pytest
import respx

from eds.modules.source.adapters.binance.rest import FAPI, SAPI
from eds.platform.config import settings
from tests.test_identity import csrf, register

pytestmark = pytest.mark.usefixtures("clean_users")

KEY = "BNKEY" + "x" * 27
SECRET = "BNSECRET" + "y" * 24
MASTER = base64.urlsafe_b64encode(b"B" * 32).decode()

NOW = dt.datetime.now(dt.UTC)


def ms(when: dt.datetime) -> int:
    return int(when.timestamp() * 1000)


READ_ONLY = {
    "enableReading": True,
    "enableWithdrawals": False,
    "enableFutures": True,
    "enableSpotAndMarginTrading": False,
    "ipRestrict": False,
    "createTime": ms(NOW),
}


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("EDS_SECRET_KEY", MASTER)
    settings.cache_clear()
    yield
    settings.cache_clear()


def fill(n: int, side: str, qty: str, price: str, pnl: str, at: dt.datetime) -> dict:
    return {
        "symbol": "BTCUSDT",
        "id": n,
        "orderId": n * 10,
        "side": side,
        "positionSide": "BOTH",
        "price": price,
        "qty": qty,
        "realizedPnl": pnl,
        "commission": "0.5",
        "commissionAsset": "USDT",
        "time": ms(at),
    }


def fresh_fills() -> list[dict]:
    """Исполнения «только что».

    Время берётся в момент запроса, а не при импорте, и это не мелочь:
    истории мы не импортируем, точка отсчёта ставится в момент подключения,
    и филл, датированный получасом раньше, сервис отбросит как «до отсчёта» —
    совершенно правильно, но тест проверял бы тогда не то.
    """
    at = dt.datetime.now(dt.UTC)
    return [
        fill(1001, "BUY", "0.2", "64000", "0", at),
        fill(1002, "SELL", "0.2", "63400", "-120.0", at),
    ]


def fresh_income() -> list[dict]:
    return [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-120.0",
            "asset": "USDT",
            "time": ms(dt.datetime.now(dt.UTC)),
            "tranId": 5001,
        }
    ]


BALANCE = [{"asset": "USDT", "balance": "20000", "crossUnPnl": "0"}]


def mock_binance(
    *,
    restrictions: dict | None = None,
    fills: list[dict] | None = None,
    income: list[dict] | None = None,
    positions: list[dict] | None = None,
) -> None:
    respx.get(f"{FAPI}/fapi/v1/time").mock(
        return_value=httpx.Response(200, json={"serverTime": ms(NOW)})
    )
    respx.get(f"{SAPI}/sapi/v1/account/apiRestrictions").mock(
        return_value=httpx.Response(200, json=restrictions or READ_ONLY)
    )
    respx.get(f"{FAPI}/fapi/v2/balance").mock(
        return_value=httpx.Response(200, json=BALANCE)
    )
    respx.get(f"{FAPI}/fapi/v1/income").mock(
        side_effect=lambda _request: httpx.Response(
            200, json=income if income is not None else fresh_income()
        )
    )
    respx.get(f"{FAPI}/fapi/v1/userTrades").mock(
        side_effect=lambda _request: httpx.Response(
            200, json=fills if fills is not None else fresh_fills()
        )
    )
    respx.get(f"{FAPI}/fapi/v3/positionRisk").mock(
        return_value=httpx.Response(200, json=positions or [])
    )


async def connect(client: httpx.AsyncClient, **over) -> httpx.Response:
    body = {"provider": "binance", "market": "futures", "key": KEY, "secret": SECRET}
    body.update(over)
    return await client.post(
        "/api/v1/source/connections", headers=csrf(client), json=body
    )


# --- права ключа ---


@respx.mock
async def test_key_with_withdrawal_is_refused(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    mock_binance(restrictions={**READ_ONLY, "enableWithdrawals": True})

    res = await connect(app_client)
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "key_has_withdrawal"

    # Подключение не создано: в настройках не должно остаться источника,
    # который никогда не заработает.
    listed = (await app_client.get("/api/v1/source/connections")).json()
    assert [c for c in listed["connections"] if c["provider"] == "binance"] == []


@respx.mock
async def test_key_bound_to_ip_is_refused(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    mock_binance(restrictions={**READ_ONLY, "ipRestrict": True})
    res = await connect(app_client)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "key_ip_restricted"


@respx.mock
async def test_key_without_reading_is_refused(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    mock_binance(restrictions={**READ_ONLY, "enableReading": False})
    res = await connect(app_client)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "key_no_reading"


@respx.mock
async def test_spot_market_is_refused(app_client: httpx.AsyncClient) -> None:
    """Спот — не параметр, а отдельная работа: там нет позиций и realizedPnl."""
    await register(app_client)
    mock_binance()
    res = await connect(app_client, market="spot")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "market_not_supported"


@respx.mock
async def test_missing_secret_is_refused(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    mock_binance()
    res = await connect(app_client, secret=None)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "validation_failed"


# --- успешное подключение ---


@respx.mock
async def test_connect_keeps_the_permissions_snapshot(
    app_client: httpx.AsyncClient,
) -> None:
    """Снимок прав сохраняется: потом будет видно, с чем сервис работал."""
    await register(app_client)
    mock_binance()

    res = await connect(app_client)
    assert res.status_code == 201, res.text
    body = res.json()

    assert body["connection"]["provider"] == "binance"
    assert body["connection"]["permissions"]["enableWithdrawals"] is False
    assert body["probe"]["permissions"]["enableReading"] is True
    # Право торговли — предупреждение, а не отказ.
    assert "key_can_trade" in {w["code"] for w in body["warnings"]}


@respx.mock
async def test_neither_key_nor_secret_come_back_from_the_api(
    app_client: httpx.AsyncClient,
) -> None:
    """Ключи не возвращаются в API ни в каком виде (ТЗ 9.3)."""
    await register(app_client)
    mock_binance()
    await connect(app_client)

    listed = (await app_client.get("/api/v1/source/connections")).text
    assert KEY not in listed
    assert SECRET not in listed
    assert "*" in listed  # маска показывается, сам ключ — нет


@respx.mock
async def test_capabilities_turn_on_own_marking(
    app_client: httpx.AsyncClient,
) -> None:
    """Тегов у биржи нет, поэтому разметка нарушений переезжает в нашу ленту.

    Это не отдельная фича, а условие работоспособности: без разметки при этом
    источнике не считаются ни SR-1, ни цена эмоций, ни дисциплина (ТЗ 4.2).
    """
    await register(app_client)
    mock_binance()
    body = (await connect(app_client)).json()

    caps = body["connection"]["capabilities"]
    assert caps["provides_tags"] is False
    assert caps["provides_positions"] is True
    assert caps["provides_balance"] is True
    assert caps["needs_aggregation"] is True
    assert caps["history_depth"] == "days:90"

    tags = (await app_client.get("/api/v1/source/tags")).json()
    assert tags["available"] is False


@respx.mock
async def test_probe_shows_the_aggregated_trade(app_client: httpx.AsyncClient) -> None:
    """Проба показывает не ключ, а работу агрегатора: сделку, собранную из филлов."""
    await register(app_client)
    mock_binance()
    body = (await connect(app_client)).json()

    assert body["probe"]["trades_seen"] == 1
    sample = body["probe"]["sample"][0]
    assert sample["symbol"] == "BTCUSDT"
    assert sample["side"] == "long"
    # −120 результата минус доллар комиссии с двух филлов.
    assert sample["profit_usd"] == "-121.0"
    assert body["probe"]["symbols"] == ["BTCUSDT"]


@respx.mock
async def test_sync_brings_the_trade_into_the_feed(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    mock_binance()
    await connect(app_client)

    report = (await app_client.post("/api/v1/sync", headers=csrf(app_client))).json()
    assert report["inserted"] == 1

    feed = (await app_client.get("/api/v1/trades?period=month")).json()
    assert feed["totals"]["count"] == 1
    item = feed["items"][0]
    assert item["source"] == "binance"
    assert item["symbol"] == "BTCUSDT"
    # Проценты от депозита считаются от настоящего баланса, а не восстановленного.
    assert item["account_return_pct"] == "-0.60"  # −121 на балансе 20 000
    assert item["marking"] == "unreviewed"
    assert item["tags"] == []


@respx.mock
async def test_second_sync_does_not_double_anything(
    app_client: httpx.AsyncClient,
) -> None:
    """Тот же филл приходит и потоком, и сверкой: дубль — норма приёма."""
    await register(app_client)
    mock_binance()
    await connect(app_client)

    first = (await app_client.post("/api/v1/sync", headers=csrf(app_client))).json()
    second = (await app_client.post("/api/v1/sync", headers=csrf(app_client))).json()

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert (await app_client.get("/api/v1/trades?period=month")).json()["totals"][
        "count"
    ] == 1


@respx.mock
async def test_own_marking_works_at_this_source(app_client: httpx.AsyncClient) -> None:
    """Отметка «не по системе» поднимает SR-1 ровно так же, как тег у TMM."""
    await register(app_client)
    mock_binance()
    await connect(app_client)
    await app_client.post("/api/v1/sync", headers=csrf(app_client))

    trade_id = (await app_client.get("/api/v1/trades?period=month")).json()["items"][0][
        "id"
    ]
    res = await app_client.put(
        f"/api/v1/trades/{trade_id}/marking",
        headers=csrf(app_client),
        json={"marking": "violation"},
    )
    assert res.status_code == 200, res.text
    effects = res.json()["effects"]
    assert effects["incident_opened"] is not None
    assert effects["lock_started"] is not None


@respx.mock
async def test_positions_give_the_honest_drawdown(
    app_client: httpx.AsyncClient,
) -> None:
    """Просадка с учётом открытой позиции — тот самый момент тильта.

    При источнике без позиций эти показатели равны `null`, а не нулю: ноль
    означал бы «открытых позиций нет» (Архитектура ч.2 §3.5).
    """
    await register(app_client)
    mock_binance(
        positions=[
            {
                "symbol": "ETHUSDT",
                "positionSide": "BOTH",
                "positionAmt": "1.5",
                "entryPrice": "2500",
                "markPrice": "2400",
                "unRealizedProfit": "-150.0",
                "liquidationPrice": "1800",
            }
        ]
    )
    await connect(app_client)
    await app_client.post("/api/v1/sync", headers=csrf(app_client))

    before = (await app_client.get("/api/v1/today")).json()["counters"]
    assert before["unrealized_pct"] is None

    res = await app_client.post("/api/v1/positions/refresh", headers=csrf(app_client))
    assert res.status_code == 200, res.text
    assert res.json()["positions"] == 1

    after = (await app_client.get("/api/v1/today")).json()["counters"]
    # −150 на балансе 20 000 это −0.75%.
    assert after["unrealized_pct"] == "-0.75"
    # Просадка от пика с учётом открытой позиции глубже реализованной.
    assert float(after["drawdown_full_pct"]) > float(after["drawdown_pct"])


# --- переключение источника ---


@respx.mock
async def test_switching_source_requires_confirmation(
    app_client: httpx.AsyncClient,
) -> None:
    """Переключение — событие с последствиями, а не настройка (Архитектура ч.1 §5.8)."""
    await register(app_client)
    await app_client.post("/api/v1/source/connections/fake", headers=csrf(app_client))
    mock_binance()
    binance_id = (await connect(app_client)).json()["connection"]["id"]

    res = await app_client.post(
        f"/api/v1/source/connections/{binance_id}/activate",
        headers=csrf(app_client),
        json={"confirm": False},
    )
    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "confirmation_required"
    assert "provides_tags" in error["details"]["losing_capabilities"]
    assert "provides_positions" in error["details"]["gaining_capabilities"]
    assert any("стрик" in line.lower() for line in error["details"]["consequences"])

    ok = await app_client.post(
        f"/api/v1/source/connections/{binance_id}/activate",
        headers=csrf(app_client),
        json={"confirm": True},
    )
    assert ok.status_code == 200, ok.text
    listed = (await app_client.get("/api/v1/source/connections")).json()
    active = [c for c in listed["connections"] if c["is_active"]]
    assert len(active) == 1
    assert active[0]["provider"] == "binance"


@respx.mock
async def test_ordinary_recompute_does_not_wipe_the_open_position(
    app_client: httpx.AsyncClient,
) -> None:
    """Пересчёт дня не трогает нереализованное, если про позиции не спрашивали.

    Поймано снимком экрана: просадка с открытой позицией считалась правильно,
    а первое же открытие «Сегодня» пересчитывало день и затирало её пустотой.
    «Не передано» и «передано пусто» — разные вещи: первое значит «эта ветка
    про позиции ничего не знает», второе — «открытых позиций нет».
    """
    await register(app_client)
    mock_binance(
        positions=[
            {
                "symbol": "ETHUSDT",
                "positionSide": "BOTH",
                "positionAmt": "1.5",
                "entryPrice": "2500",
                "markPrice": "2400",
                "unRealizedProfit": "-150.0",
                "liquidationPrice": "1800",
            }
        ]
    )
    await connect(app_client)
    await app_client.post("/api/v1/sync", headers=csrf(app_client))
    await app_client.post("/api/v1/positions/refresh", headers=csrf(app_client))

    first = (await app_client.get("/api/v1/today")).json()["counters"]
    assert first["unrealized_pct"] == "-0.75"

    # Второе чтение экрана снова гоняет движок по дню — и не должно ничего терять.
    second = (await app_client.get("/api/v1/today")).json()["counters"]
    assert second["unrealized_pct"] == "-0.75"
    assert second["drawdown_full_pct"] == first["drawdown_full_pct"]

    # Сверка тоже: она приносит сделки и пересчитывает день.
    await app_client.post("/api/v1/sync", headers=csrf(app_client))
    third = (await app_client.get("/api/v1/today")).json()["counters"]
    assert third["unrealized_pct"] == "-0.75"
