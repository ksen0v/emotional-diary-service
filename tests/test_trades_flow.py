"""Сквозной путь: фейковый источник → сверка → лента.

Проверяется тот же путь, по которому пойдут настоящие сделки: dev-панель кладёт
сделку в источник, а не в таблицу сделок напрямую.
"""

import httpx
import pytest

from eds.app.consumers import all_consumers
from eds.platform import bus
from tests.test_identity import csrf, move_day_boundary_away, register

pytestmark = pytest.mark.usefixtures("clean_users")

SYSTEM_TAG = "ПО СИСТЕМЕ"
VIOLATION_TAG = "НЕ СИСТЕМНАЯ ТОРГОВЛЯ"


async def setup_user(client: httpx.AsyncClient) -> None:
    await register(client)
    await move_day_boundary_away(client)
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


async def drain(consumer: bus.Consumer) -> None:
    """Догнать шину до конца.

    Один проход берёт ограниченную порцию, а событий в базе к этому моменту
    может быть много: без догона тест начал бы падать просто от их количества.
    """
    for _ in range(50):
        if await consumer.step() == 0:
            return


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
    # Берём настоящий из регистрации, а не собираем копию: копия не знала бы
    # про фильтр по типу события и спотыкалась бы на чужих событиях.
    await drain(all_consumers()[0])

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


# --- шаг 3: своя разметка и метрики ---


async def tagless(client: httpx.AsyncClient) -> None:
    """Заставить тестовый источник изображать источник без тегов (как Binance)."""
    res = await client.patch(
        "/api/v1/source/connections/fake/capabilities",
        headers=csrf(client),
        json={"provides_tags": False},
    )
    assert res.status_code == 200, res.text
    assert res.json()["capabilities"]["provides_tags"] is False


async def metrics(client: httpx.AsyncClient, period: str = "month") -> dict:
    res = await client.get("/api/v1/trades/metrics", params={"period": period})
    assert res.status_code == 200, res.text
    return res.json()


async def test_own_marking_refused_when_source_has_tags(
    app_client: httpx.AsyncClient,
) -> None:
    """При источнике с тегами разметка живёт в дневнике источника, не у нас.

    Две точки правды означали бы, что одна сделка размечена двумя способами.
    """
    await setup_user(app_client)
    await push(app_client, tags=[SYSTEM_TAG])
    await sync(app_client)
    trade_id = (await feed(app_client))["items"][0]["id"]

    res = await app_client.put(
        f"/api/v1/trades/{trade_id}/marking",
        headers=csrf(app_client),
        json={"marking": "violation"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "not_supported_by_source"


async def test_own_marking_works_without_tags(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    await tagless(app_client)
    await push(app_client, tags=[SYSTEM_TAG])  # теги будут отброшены источником
    await sync(app_client)

    item = (await feed(app_client))["items"][0]
    assert item["marking"] == "unreviewed"
    assert item["tags"] == []

    res = await app_client.put(
        f"/api/v1/trades/{item['id']}/marking",
        headers=csrf(app_client),
        json={"marking": "violation"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["trade"]["marking"] == "violation"
    assert body["trade"]["marked_by"] == "user"
    assert body["effects"] == {"changed": True, "marking_before": "unreviewed"}
    # метрики приходят тем же ответом: разметка их меняет немедленно
    assert body["metrics"]["violations"]["count"] == 1
    assert body["metrics"]["coverage_pct"] == "100.00"


async def test_own_marking_can_be_changed_back(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    await tagless(app_client)
    await push(app_client)
    await sync(app_client)
    trade_id = (await feed(app_client))["items"][0]["id"]

    async def mark(value: str) -> dict:
        res = await app_client.put(
            f"/api/v1/trades/{trade_id}/marking",
            headers=csrf(app_client),
            json={"marking": value},
        )
        assert res.status_code == 200, res.text
        return res.json()

    await mark("violation")
    back = await mark("clean")
    assert back["trade"]["marking"] == "clean"
    assert back["metrics"]["violations"]["count"] == 0
    assert back["metrics"]["discipline_pct"] == "100.00"


async def test_marking_survives_sync(app_client: httpx.AsyncClient) -> None:
    """Повторная сверка не затирает разметку, сделанную руками."""
    await setup_user(app_client)
    await tagless(app_client)
    await push(app_client)
    await sync(app_client)
    trade_id = (await feed(app_client))["items"][0]["id"]

    await app_client.put(
        f"/api/v1/trades/{trade_id}/marking",
        headers=csrf(app_client),
        json={"marking": "violation"},
    )
    report = await sync(app_client)
    assert report["unchanged"] == 1

    assert (await feed(app_client))["items"][0]["marking"] == "violation"


async def test_discipline_counted_over_marked_only(app_client: httpx.AsyncClient) -> None:
    """Коэффициент дисциплины — доля чистых среди размеченных.

    Если считать среди всех, выгоднее было бы не размечать вовсе.
    """
    await setup_user(app_client)
    await tagless(app_client)
    for _ in range(4):
        await push(app_client)
    await sync(app_client)

    ids = [item["id"] for item in (await feed(app_client))["items"]]
    for trade_id, value in zip(ids[:3], ["clean", "clean", "violation"], strict=True):
        await app_client.put(
            f"/api/v1/trades/{trade_id}/marking",
            headers=csrf(app_client),
            json={"marking": value},
        )

    body = await metrics(app_client)
    assert body["marking"]["trades"]["all"] == 4
    assert body["marking"]["marked"] == 3
    assert body["marking"]["unmarked"] == 1
    assert body["marking"]["coverage_pct"] == "75.00"
    # 2 чистых из 3 размеченных, а не из 4 сделок
    assert body["marking"]["discipline_pct"] == "66.67"


async def test_discipline_is_null_without_marked_trades(
    app_client: httpx.AsyncClient,
) -> None:
    """Без размеченных сделок коэффициента не существует — не ноль, а «нет»."""
    await setup_user(app_client)
    await tagless(app_client)
    await push(app_client)
    await sync(app_client)

    body = await metrics(app_client)
    assert body["marking"]["discipline_pct"] is None
    assert body["marking"]["coverage_pct"] == "0.00"


async def test_emotion_cost_and_profitable_violations(
    app_client: httpx.AsyncClient,
) -> None:
    """Нарушение в плюс остаётся нарушением и считается отдельно."""
    await setup_user(app_client)
    await tagless(app_client)
    await push(app_client, profit="-140.00", pct="-1.10")
    await push(app_client, profit="126.40", pct="1.02", minutes_ago=4)
    await sync(app_client)

    ids = [item["id"] for item in (await feed(app_client))["items"]]
    for trade_id in ids:
        await app_client.put(
            f"/api/v1/trades/{trade_id}/marking",
            headers=csrf(app_client),
            json={"marking": "violation"},
        )

    body = await metrics(app_client)
    assert body["marking"]["violations"]["count"] == 2
    assert body["marking"]["violations"]["profitable"] == 1
    # цена эмоций — сумма по нарушениям, включая прибыльное
    assert body["marking"]["emotion_cost_usd"] == "-13.60"


async def test_confidence_threshold(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    await tagless(app_client)
    await push(app_client)
    await sync(app_client)

    body = await metrics(app_client)
    assert body["confidence"]["enough_data"] is False
    assert body["confidence"]["days_available"] == 1
    assert body["confidence"]["days_required"] == 30


async def test_bad_marking_value_rejected(app_client: httpx.AsyncClient) -> None:
    await setup_user(app_client)
    await tagless(app_client)
    await push(app_client)
    await sync(app_client)
    trade_id = (await feed(app_client))["items"][0]["id"]

    res = await app_client.put(
        f"/api/v1/trades/{trade_id}/marking",
        headers=csrf(app_client),
        json={"marking": "unreviewed"},
    )
    assert res.status_code == 400
