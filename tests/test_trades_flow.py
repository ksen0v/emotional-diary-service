"""Сквозной путь: фейковый источник → сверка → лента.

Проверяется тот же путь, по которому пойдут настоящие сделки: dev-панель кладёт
сделку в источник, а не в таблицу сделок напрямую.
"""

import httpx
import pytest

from tests.test_identity import csrf, register

pytestmark = pytest.mark.usefixtures("clean_users")

SYSTEM_TAG = "ПО СИСТЕМЕ"
VIOLATION_TAG = "НЕ СИСТЕМНАЯ ТОРГОВЛЯ"


async def setup_user(client: httpx.AsyncClient) -> None:
    await register(client)
    res = await client.post("/api/v1/source/connections/fake", headers=csrf(client))
    assert res.status_code == 200, res.text
    assert res.json()["is_active"] is True
    assert res.json()["capabilities"]["provides_tags"] is True


async def push(
    client: httpx.AsyncClient,
    *,
    profit: str = "-84.20",
    pct: str = "-0.68",
    tags: list[str] | None = None,
    symbol: str = "BTCUSDT",
    minutes_ago: int = 5,
) -> httpx.Response:
    res = await client.post(
        "/api/v1/source/dev/trade",
        headers=csrf(client),
        json={
            "symbol": symbol,
            "side": "long",
            "profit_usd": profit,
            "account_return_pct": pct,
            "tags": tags or [],
            "minutes_ago": minutes_ago,
            "duration_sec": 300,
        },
    )
    assert res.status_code == 200, res.text
    return res


async def sync(client: httpx.AsyncClient) -> dict:
    res = await client.post("/api/v1/sync", headers=csrf(client))
    assert res.status_code == 200, res.text
    return res.json()


async def feed(client: httpx.AsyncClient, **params: str) -> dict:
    res = await client.get("/api/v1/trades", params=params)
    assert res.status_code == 200, res.text
    return res.json()


async def test_trade_reaches_feed(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    await push(app_client, tags=[SYSTEM_TAG])
    report = await sync(app_client)

    assert report["received"] == 1
    assert report["inserted"] == 1

    body = await feed(app_client)
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["symbol"] == "BTCUSDT"
    assert item["profit_usd"] == "-84.20"
    assert item["is_significant"] is True  # 0.68% при пороге 0.50%
    assert item["marking"] == "clean"  # тег есть, нарушением не отмечен
    assert [t["name"] for t in item["tags"]] == [SYSTEM_TAG]


async def test_second_sync_does_not_duplicate(app_client: httpx.AsyncClient) -> None:
    """Идемпотентность: одна и та же сделка приходит из потока и из сверки."""
    await setup_user(app_client)
    await push(app_client, tags=[SYSTEM_TAG])
    await sync(app_client)
    second = await sync(app_client)

    assert second["inserted"] == 0
    assert second["unchanged"] == 1
    assert (await feed(app_client))["totals"]["count"] == 1


async def test_untagged_trade_is_unreviewed(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    await push(app_client, tags=[])
    await sync(app_client)

    body = await feed(app_client)
    assert body["items"][0]["marking"] == "unreviewed"
    assert body["totals"]["unmarked_count"] == 1
    assert body["totals"]["coverage_pct"] == "0.00"


async def test_dust_is_marked_insignificant(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    await push(app_client, profit="-3.10", pct="-0.04", tags=[SYSTEM_TAG])
    await sync(app_client)

    body = await feed(app_client)
    assert body["items"][0]["is_significant"] is False
    assert body["totals"]["count"] == 1
    assert body["totals"]["significant_count"] == 0


async def test_violation_tag_remarks_existing_trades(app_client: httpx.AsyncClient) -> None:
    """Трейдер отметил тег нарушением → сделка переразмечается через шину.

    Это не прямой вызов: source публикует событие, trades его обрабатывает.
    Тест ждёт консьюмера, поэтому проверяет и шину заодно.
    """
    from eds.app.consumers import on_tag_dictionary_changed
    from eds.platform import bus

    await setup_user(app_client)
    await push(app_client, tags=[VIOLATION_TAG])
    await sync(app_client)
    assert (await feed(app_client))["items"][0]["marking"] == "clean"

    tags = (await app_client.get("/api/v1/source/tags")).json()["tags"]
    target = next(t for t in tags if t["name"] == VIOLATION_TAG)

    res = await app_client.put(
        "/api/v1/source/tags/violations",
        headers=csrf(app_client),
        json={"violation_tag_ids": [target["external_id"]]},
    )
    assert res.status_code == 200

    # Консьюмер в тесте запускаем вручную: в приложении он работает фоном.
    consumer = bus.Consumer("test-remark", on_tag_dictionary_changed)
    await consumer.step()

    body = await feed(app_client)
    assert body["items"][0]["marking"] == "violation"
    assert body["totals"]["violations_count"] == 1


async def test_filters_and_totals(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    await push(app_client, tags=[SYSTEM_TAG], symbol="BTCUSDT")
    await push(app_client, tags=[], symbol="ETHUSDT", minutes_ago=4)
    await push(app_client, tags=[], symbol="SOLUSDT", minutes_ago=3)
    await sync(app_client)

    everything = await feed(app_client)
    assert everything["totals"]["count"] == 3
    assert everything["totals"]["unmarked_count"] == 2
    assert everything["totals"]["coverage_pct"] == "33.33"

    unmarked = await feed(app_client, filter="unmarked")
    assert len(unmarked["items"]) == 2
    # итоги считаются по выборке фильтра, а не по странице
    assert unmarked["totals"]["count"] == 2


async def test_cursor_pagination(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    for i in range(5):
        await push(app_client, tags=[SYSTEM_TAG], minutes_ago=10 + i)
    await sync(app_client)

    first = await feed(app_client, limit="2")
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    assert first["next_cursor"]

    second = await feed(app_client, limit="2", cursor=first["next_cursor"])
    assert len(second["items"]) == 2
    ids_first = {i["id"] for i in first["items"]}
    ids_second = {i["id"] for i in second["items"]}
    # страницы не пересекаются — иначе трейдер увидел бы одну сделку дважды
    assert not (ids_first & ids_second)


async def test_day_curve(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    await push(app_client, profit="-84.20", pct="-0.68", tags=[SYSTEM_TAG], minutes_ago=20)
    await push(app_client, profit="-91.00", pct="-0.72", tags=[SYSTEM_TAG], minutes_ago=10)
    await sync(app_client)

    res = await app_client.get("/api/v1/trades/day-curve")
    assert res.status_code == 200, res.text
    body = res.json()

    assert len(body["points"]) == 2
    assert body["points"][0]["equity_pct"] == "-0.68"
    assert body["points"][1]["equity_pct"] == "-1.40"
    assert body["points"][1]["drawdown_pct"] == "1.40"
    # источник без открытых позиций: null, а не ноль
    assert body["unrealized"] == {"available": False, "pct": None}
    assert body["close"]["max_drawdown_pct"] == "1.40"


async def test_sync_without_source_is_rejected(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.post("/api/v1/sync", headers=csrf(app_client))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "no_active_source"


async def test_trades_require_auth(app_client: httpx.AsyncClient) -> None:
    res = await app_client.get("/api/v1/trades")
    assert res.status_code == 401
