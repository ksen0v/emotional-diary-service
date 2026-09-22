"""Дневник: запись, правка, комментарии, факты периода — через HTTP.

Главное здесь — что факты периода считает сервер и ставит рядом с записью.
Оценка «ровно» рядом с «0 нарушений, +$140» читается иначе, чем сама по себе,
и именно это отличает дневник трейдера от блокнота.
"""

import datetime as dt

import httpx
import pytest

from eds.app.consumers import all_consumers
from tests.test_identity import csrf, move_day_boundary_away, register
from tests.test_premarket import BEST
from tests.test_trades_flow import drain, push, sync

pytestmark = pytest.mark.usefixtures("clean_users")


async def setup(client: httpx.AsyncClient) -> None:
    await register(client)
    await move_day_boundary_away(client)
    res = await client.post("/api/v1/source/connections/fake", headers=csrf(client))
    assert res.status_code == 200, res.text


async def today_day(client: httpx.AsyncClient) -> dt.date:
    res = await client.get("/api/v1/today")
    return dt.date.fromisoformat(res.json()["day"])


async def put_entry(
    client: httpx.AsyncClient, level: str, period_start: dt.date, **body
) -> httpx.Response:
    return await client.put(
        f"/api/v1/entries/{level}/{period_start.isoformat()}",
        headers=csrf(client),
        json=body,
    )


async def test_presets_are_suggestions_from_the_server(app_client) -> None:
    await setup(app_client)
    res = await app_client.get("/api/v1/diary/presets")
    body = res.json()
    assert "Тильтовый" in body["statuses"]
    assert "Хочу отыграться" in body["tags"]


async def test_entry_is_created_and_updated(app_client) -> None:
    await setup(app_client)
    day = await today_day(app_client)

    created = await put_entry(
        app_client,
        "day",
        day,
        score=4,
        status="Дисциплинированный",
        tags=["Фокус 100%", "Кофе x3"],
        body="Отработал по плану.",
    )
    assert created.status_code == 200, created.text
    entry = created.json()["entry"]
    assert entry["score"] == 4
    assert entry["status"] == "Дисциплинированный"
    assert entry["tags"] == ["Кофе x3", "Фокус 100%"]
    assert entry["editable"] is True

    updated = await put_entry(
        app_client, "day", day, score=2, status="Тильтовый", tags=["Устал"], body="Нет."
    )
    assert updated.status_code == 200
    second = updated.json()["entry"]
    assert second["id"] == entry["id"]  # та же запись, а не вторая
    assert second["score"] == 2
    assert second["tags"] == ["Устал"]


async def test_tags_are_free_text_without_duplicates(app_client) -> None:
    """Пресеты подсказывают, но не ограничивают: словарь состояний у каждого свой."""
    await setup(app_client)
    day = await today_day(app_client)
    res = await put_entry(
        app_client, "day", day, tags=["Своё слово", " Своё слово ", "", "Ещё"]
    )
    assert res.json()["entry"]["tags"] == ["Ещё", "Своё слово"]


async def test_old_entry_is_not_editable_only_commentable(app_client) -> None:
    """Через 48 часов запись остаётся тем, что трейдер думал тогда (ТЗ 9.2)."""
    await setup(app_client)
    long_ago = await today_day(app_client) - dt.timedelta(days=5)

    first = await put_entry(app_client, "day", long_ago, score=3, body="Записал поздно.")
    assert first.status_code == 200
    entry = first.json()["entry"]
    assert entry["editable"] is False

    again = await put_entry(app_client, "day", long_ago, score=5)
    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "not_editable"
    assert error["details"]["comments_url"].endswith(f"/entries/{entry['id']}/comments")

    comment = await app_client.post(
        f"/api/v1/entries/{entry['id']}/comments",
        headers=csrf(app_client),
        json={"body": "Через месяц вижу, что дело было не в усталости."},
    )
    assert comment.status_code == 201

    listed = await app_client.get(
        "/api/v1/entries",
        params={"level": "day", "from": long_ago.isoformat(), "to": long_ago.isoformat()},
    )
    item = listed.json()["items"][0]
    assert len(item["entry"]["comments"]) == 1
    assert item["entry"]["score"] == 3  # комментарий не переписал запись


async def test_future_entry_is_refused(app_client) -> None:
    await setup(app_client)
    tomorrow = await today_day(app_client) + dt.timedelta(days=1)
    res = await put_entry(app_client, "day", tomorrow, score=5)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "period_in_future"


async def test_calendar_shows_empty_days_too(app_client) -> None:
    await setup(app_client)
    day = await today_day(app_client)
    await put_entry(app_client, "day", day, score=4)

    res = await app_client.get(
        "/api/v1/entries",
        params={
            "level": "day",
            "from": (day - dt.timedelta(days=3)).isoformat(),
            "to": day.isoformat(),
        },
    )
    items = res.json()["items"]
    assert len(items) == 4
    # Порядок — от свежего к старому: так календарь и дневник читаются.
    assert items[0]["period_start"] == day.isoformat()
    assert items[0]["entry"]["score"] == 4
    assert items[-1]["entry"] is None
    assert items[-1]["facts"]["trades"] == 0


async def test_day_facts_come_from_trades_and_admission(app_client) -> None:
    await setup(app_client)
    day = await today_day(app_client)
    await app_client.post(
        "/api/v1/premarket/checks", headers=csrf(app_client), json={"answers": BEST}
    )
    await push(app_client, profit="-84.20", pct="-0.68", tags=[])
    await sync(app_client)

    res = await app_client.get(
        "/api/v1/entries",
        params={"level": "day", "from": day.isoformat(), "to": day.isoformat()},
    )
    facts = res.json()["items"][0]["facts"]
    assert facts["trades"] == 1
    assert facts["unmarked"] == 1
    assert float(facts["profit_usd"]) == -84.20
    assert facts["admission"] == "green"
    assert facts["check_score"] == 25
    # Стрик появится на шаге 7: null, а не false.
    assert facts["counted_in_streak"] is None


async def test_week_facts_carry_the_metrics_from_the_spec(app_client) -> None:
    await setup(app_client)
    day = await today_day(app_client)

    await push(app_client, profit="-140.00", pct="-1.10", tags=["НЕ СИСТЕМНАЯ ТОРГОВЛЯ"])
    await push(app_client, profit="+28.00", pct="+0.22", tags=["НЕ СИСТЕМНАЯ ТОРГОВЛЯ"])
    await push(app_client, profit="+126.40", pct="+1.02", tags=["ПО СИСТЕМЕ"])
    await sync(app_client)

    tags = (await app_client.get("/api/v1/source/tags")).json()["tags"]
    violation = next(t for t in tags if t["name"] == "НЕ СИСТЕМНАЯ ТОРГОВЛЯ")
    await app_client.put(
        "/api/v1/source/tags/violations",
        headers=csrf(app_client),
        json={"violation_tag_ids": [violation["external_id"]]},
    )
    await drain(all_consumers()[0])

    res = await app_client.get("/api/v1/entries", params={"level": "week"})
    items = res.json()["items"]
    current = next(i for i in items if i["period_start"] <= day.isoformat() <= i["period_end"])
    facts = current["facts"]

    assert facts["trades"] == 3
    assert facts["violations"] == 2
    assert facts["coverage_pct"] == "100.00"
    assert facts["discipline_pct"] == "33.33"
    # Цена эмоций — сальдо нарушений, «слито» — только убыточные,
    # «в плюс» — только прибыльные. Три разных факта, а не одно число.
    assert float(facts["emotion_cost_usd"]) == -112.00
    assert float(facts["lost_on_emotions_usd"]) == -140.00
    assert float(facts["violations_gain_usd"]) == 28.00
    assert facts["violations_profitable"] == 1
    # Сделки были, а чек не проходился — день считается без допуска (ТЗ 5.2).
    assert facts["days_without_admission"] == 1
    assert facts["lock_compliance_pct"] is None


async def test_week_entry_snaps_to_monday(app_client) -> None:
    await setup(app_client)
    day = await today_day(app_client)
    res = await put_entry(app_client, "week", day, score=3, body="Неделя ровная.")
    assert res.status_code == 200
    body = res.json()
    start = dt.date.fromisoformat(body["period_start"])
    assert start.weekday() == 0
    assert dt.date.fromisoformat(body["period_end"]) == start + dt.timedelta(days=6)


async def test_too_wide_range_is_refused(app_client) -> None:
    await setup(app_client)
    day = await today_day(app_client)
    res = await app_client.get(
        "/api/v1/entries",
        params={
            "level": "day",
            "from": (day - dt.timedelta(days=500)).isoformat(),
            "to": day.isoformat(),
        },
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "range_too_wide"
