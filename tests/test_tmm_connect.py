"""Подключение TMM ключом: сквозной путь через HTTP с подменённым провайдером.

Провайдер подменён, всё остальное настоящее: база, сессии, шифрование, приём
сделок. Проверяется то, что на живом ключе проверить нельзя без риска —
отказ по неверному ключу, отсутствие ключа в ответах API, переключение
источников с подтверждением.
"""

import base64
import datetime as dt
from decimal import Decimal

import httpx
import pytest
import respx

from eds.modules.source.adapters.tmm.rest import BASE_URLS
from eds.platform.config import settings
from tests.test_identity import csrf, register
from tests.test_tmm_mapping import COLUMNS, TAGS, trade_row

pytestmark = pytest.mark.usefixtures("clean_users")

PRIMARY = BASE_URLS[0]
KEY = "R3KY" + "x" * 24 + "p2a9"
MASTER = base64.urlsafe_b64encode(b"M" * 32).decode()

ACCOUNTS = [
    {
        "id": 217071,
        "name": "Binance Main",
        "exchange": "Binance Futures",
        "market": "futures",
    }
]


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("EDS_SECRET_KEY", MASTER)
    settings.cache_clear()
    yield
    settings.cache_clear()


def ms(when: dt.datetime) -> int:
    return int(when.timestamp() * 1000)


def fresh_trade(request: httpx.Request) -> httpx.Response:
    """Сделка, закрытая «сейчас»: время считается в момент запроса.

    Иначе она оказалась бы раньше точки отсчёта, которая ставится в момент
    подключения, и приём законно бы её отбросил.
    """
    now = dt.datetime.now(dt.UTC)
    return httpx.Response(
        200,
        json=[
            trade_row(
                id=900001,
                open_time=ms(now - dt.timedelta(minutes=7)),
                close_time=ms(now),
            )
        ],
        headers={"x-ratelimit-limit": "120", "x-ratelimit-remaining": "118"},
    )


def mock_provider(trades=fresh_trade) -> None:
    respx.get(f"{PRIMARY}/api-key").mock(return_value=httpx.Response(200, json=ACCOUNTS))
    respx.get(f"{PRIMARY}/trades/tags").mock(return_value=httpx.Response(200, json=TAGS))
    respx.get(f"{PRIMARY}/trades/tag-categories").mock(
        return_value=httpx.Response(200, json=COLUMNS)
    )
    route = respx.get(f"{PRIMARY}/trades/")
    if callable(trades):
        route.mock(side_effect=trades)
    else:
        route.mock(return_value=httpx.Response(200, json=trades))


async def connect(client: httpx.AsyncClient, key: str = KEY) -> httpx.Response:
    return await client.post(
        "/api/v1/source/connections",
        headers=csrf(client),
        json={"provider": "tmm", "key": key},
    )


async def test_connect_stores_key_encrypted_and_never_returns_it(app_client) -> None:
    await register(app_client)
    with respx.mock:
        mock_provider()
        res = await connect(app_client)

    assert res.status_code == 201, res.text
    body = res.json()
    assert KEY not in res.text
    assert body["connection"]["key_masked"] == "R3KY********p2a9"
    assert body["connection"]["is_active"] is True
    assert body["connection"]["capabilities"]["provides_tags"] is True
    assert [a["name"] for a in body["accounts"]] == ["Binance Main"]

    # Проба показывает, что видно по ключу, и ничего не сохраняет.
    assert body["probe"]["trades_seen"] == 1
    assert len(body["probe"]["entry_tags"]) == 2
    assert body["probe"]["sample"][0]["symbol"] == "BTCUSDT"

    listed = await app_client.get("/api/v1/source/connections")
    assert KEY not in listed.text

    # Сделок в сервисе ещё нет: проба — не приём.
    feed = await app_client.get("/api/v1/trades", params={"period": "month"})
    assert feed.json()["totals"]["count"] == 0


async def test_tag_dictionary_is_ready_before_any_trade(app_client) -> None:
    """Историю не импортируем, но отметить «тег = нарушение» надо сразу."""
    await register(app_client)
    with respx.mock:
        mock_provider(trades=[])
        await connect(app_client)

    tags = await app_client.get("/api/v1/source/tags")
    body = tags.json()
    assert body["available"] is True
    assert sorted(t["name"] for t in body["tags"]) == ["Sc. 1.1", "НЕ СИСТЕМНАЯ ТОРГОВЛЯ"]
    # Тег выхода в словарь не попал: нарушение определяется тегом входа.
    assert "СТОП" not in [t["name"] for t in body["tags"]]


async def test_rejected_key_leaves_no_connection(app_client) -> None:
    await register(app_client)
    with respx.mock:
        respx.get(f"{PRIMARY}/trades/tags").mock(return_value=httpx.Response(401))
        res = await connect(app_client, "wrong-key-value")

    assert res.status_code == 400
    assert res.json()["error"]["code"] == "key_rejected"
    listed = await app_client.get("/api/v1/source/connections")
    assert listed.json()["connections"] == []


async def test_sync_ingests_real_shaped_trades(app_client) -> None:
    await register(app_client)
    with respx.mock:
        mock_provider()
        await connect(app_client)
        res = await app_client.post("/api/v1/sync", headers=csrf(app_client))
        assert res.status_code == 200, res.text
        report = res.json()
        assert report["inserted"] == 1

        # Повтор идемпотентен: та же сделка не удваивается.
        again = await app_client.post("/api/v1/sync", headers=csrf(app_client))
        assert again.json()["inserted"] == 0

    feed = await app_client.get("/api/v1/trades", params={"period": "month"})
    items = feed.json()["items"]
    assert len(items) == 1
    trade = items[0]
    assert trade["symbol"] == "BTCUSDT"
    assert trade["side"] == "short"
    assert Decimal(trade["account_return_pct"]) == Decimal("-0.68")
    assert Decimal(trade["profit_usd"]) == Decimal("-84.20")
    assert trade["duration_sec"] == 420
    assert [t["name"] for t in trade["tags"]] == ["НЕ СИСТЕМНАЯ ТОРГОВЛЯ"]
    # Тег есть, но нарушением он станет только когда трейдер его отметит.
    assert trade["marking"] == "clean"

    listed = await app_client.get("/api/v1/source/connections")
    connection = listed.json()["connections"][0]
    assert connection["last_reconcile"]["status"] == "ok"
    assert connection["last_reconcile"]["trades_new"] == 0  # последняя сверка — повтор
    assert connection["rate_limit"]["remaining"] == 118


async def test_failed_sync_is_written_into_the_journal(app_client) -> None:
    """«Сделки не приехали» должно быть чем объяснить."""
    await register(app_client)
    with respx.mock:
        mock_provider()
        await connect(app_client)
    with respx.mock:
        for base in BASE_URLS:
            respx.get(f"{base}/api-key").mock(return_value=httpx.Response(503))
        res = await app_client.post("/api/v1/sync", headers=csrf(app_client))

    assert res.status_code == 502
    assert res.json()["error"]["code"] == "provider_unavailable"
    listed = await app_client.get("/api/v1/source/connections")
    connection = listed.json()["connections"][0]
    assert connection["state"] == "error"
    assert connection["last_reconcile"]["status"] == "error"
    assert connection["last_error"]


async def test_switching_source_requires_confirmation(app_client) -> None:
    await register(app_client)
    fake = await app_client.post("/api/v1/source/connections/fake", headers=csrf(app_client))
    assert fake.json()["is_active"] is True

    with respx.mock:
        mock_provider(trades=[])
        connected = await connect(app_client)
    tmm_id = connected.json()["connection"]["id"]
    assert connected.json()["connection"]["is_active"] is False

    without = await app_client.post(
        f"/api/v1/source/connections/{tmm_id}/activate",
        headers=csrf(app_client),
        json={"confirm": False},
    )
    assert without.status_code == 409
    error = without.json()["error"]
    assert error["code"] == "confirmation_required"
    assert error["details"]["consequences"]

    with_confirm = await app_client.post(
        f"/api/v1/source/connections/{tmm_id}/activate",
        headers=csrf(app_client),
        json={"confirm": True},
    )
    assert with_confirm.status_code == 200
    assert with_confirm.json()["is_active"] is True


async def test_delete_removes_key_and_keeps_trades(app_client) -> None:
    await register(app_client)
    with respx.mock:
        mock_provider()
        connected = await connect(app_client)
        await app_client.post("/api/v1/sync", headers=csrf(app_client))
    connection_id = connected.json()["connection"]["id"]

    removed = await app_client.delete(
        f"/api/v1/source/connections/{connection_id}", headers=csrf(app_client)
    )
    assert removed.status_code == 204

    listed = await app_client.get("/api/v1/source/connections")
    assert listed.json()["connections"] == []
    feed = await app_client.get("/api/v1/trades", params={"period": "month"})
    assert feed.json()["totals"]["count"] == 1


async def test_connect_without_master_key_is_refused(app_client, monkeypatch) -> None:
    await register(app_client)
    monkeypatch.setenv("EDS_SECRET_KEY", "")
    settings.cache_clear()
    with respx.mock:
        mock_provider(trades=[])
        res = await connect(app_client)
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "secret_key_missing"
