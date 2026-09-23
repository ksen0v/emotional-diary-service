"""Стрик через HTTP: день с нарушением не зачтён, причина показана.

Это проверка шага 7 целиком: чистый прошлый день удлиняет серию, день
с нарушением её рвёт, причина видна в дневнике, а заморозка не делает
ни того ни другого.
"""

import datetime as dt

import httpx
import pytest
from sqlalchemy import text

from eds.app.consumers import all_consumers
from tests.test_identity import csrf, move_day_boundary_away, register
from tests.test_trades_flow import drain, push, sync

pytestmark = pytest.mark.usefixtures("clean_users")

SYSTEM_TAG = "ПО СИСТЕМЕ"
VIOLATION_TAG = "НЕ СИСТЕМНАЯ ТОРГОВЛЯ"


async def setup(client: httpx.AsyncClient) -> None:
    await register(client)
    await move_day_boundary_away(client)
    res = await client.post("/api/v1/source/connections/fake", headers=csrf(client))
    assert res.status_code == 200, res.text


async def user_id(client: httpx.AsyncClient) -> str:
    return (await client.get("/api/v1/me")).json()["user"]["id"]


async def streak(client: httpx.AsyncClient) -> dict:
    res = await client.get("/api/v1/streak")
    assert res.status_code == 200, res.text
    return res.json()


async def push_on(client: httpx.AsyncClient, days_ago: int, tags: list[str]) -> str:
    """Подать сделку N дней назад и вернуть её торговый день так, как его понял
    сервер: считать границу дня в тесте заново — верный способ разойтись с ним."""
    await push(client, tags=tags, minutes_ago=days_ago * 24 * 60 + 120)
    await sync(client)
    feed = await client.get("/api/v1/trades", params={"period": "month", "limit": "50"})
    return feed.json()["items"][0]["trading_day"]


async def close_day(factory, uid: str, day: str) -> None:
    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO daybook.trading_days "
                "(user_id, day, admission, check_score, session_opened_at, "
                " session_closed_at, review_state) "
                "VALUES (:uid, :day, 'green', 21, :opened, :closed, 'pending') "
                "ON CONFLICT (user_id, day) DO UPDATE SET review_state = 'pending'"
            ),
            {
                "uid": uid,
                "day": dt.date.fromisoformat(day),
                "opened": dt.datetime.now(dt.UTC) - dt.timedelta(days=3),
                "closed": dt.datetime.now(dt.UTC) - dt.timedelta(days=2, hours=12),
            },
        )
        await s.commit()


async def fill_day(client: httpx.AsyncClient, day: str) -> None:
    """Закрыть день по-человечески: запись дневника и разбор."""
    entry = await client.put(
        f"/api/v1/entries/day/{day}",
        headers=csrf(client),
        json={"score": 4, "status": "Дисциплинированный", "tags": [], "body": "Ровно."},
    )
    assert entry.status_code == 200, entry.text
    review = await client.post(
        "/api/v1/reviews",
        headers=csrf(client),
        json={
            "day": day,
            "plan_followed": "yes",
            "pull_text": None,
            "execution_score": 4,
            "takeaway": "Держать размер.",
        },
    )
    assert review.status_code == 201, review.text


async def test_clean_day_counts_and_violation_breaks_the_streak(
    app_client, factory
) -> None:
    await setup(app_client)
    uid = await user_id(app_client)

    clean_day = await push_on(app_client, 2, [SYSTEM_TAG])
    await close_day(factory, uid, clean_day)
    await fill_day(app_client, clean_day)

    after_clean = await streak(app_client)
    assert after_clean["current"] == 1
    assert after_clean["best"] == 1
    today_mark = next(d for d in after_clean["days"] if d["day"] == clean_day)
    assert today_mark["counted"] is True
    assert today_mark["reason"] == "ok"

    bad_day = await push_on(app_client, 1, [VIOLATION_TAG])
    tags = (await app_client.get("/api/v1/source/tags")).json()["tags"]
    violation = next(t for t in tags if t["name"] == VIOLATION_TAG)
    await app_client.put(
        "/api/v1/source/tags/violations",
        headers=csrf(app_client),
        json={"violation_tag_ids": [violation["external_id"]]},
    )
    await drain(all_consumers()[0])
    await close_day(factory, uid, bad_day)
    await fill_day(app_client, bad_day)

    after_bad = await streak(app_client)
    # Серия оборвалась, но лучший результат остался: он и есть то, к чему
    # возвращаются после срыва (ТЗ 7.2).
    assert after_bad["current"] == 0
    assert after_bad["best"] == 1
    mark = next(d for d in after_bad["days"] if d["day"] == bad_day)
    assert mark["counted"] is False
    assert mark["reason"] == "violation"
    assert mark["text"] == "Есть нарушения"

    # Причина видна там, где трейдер смотрит на день, — в дневнике.
    entries = await app_client.get(
        "/api/v1/entries", params={"level": "day", "from": bad_day, "to": bad_day}
    )
    facts = entries.json()["items"][0]["facts"]
    assert facts["counted_in_streak"] is False
    assert facts["streak_reason"] == "violation"
    assert facts["streak_reason_text"] == "Есть нарушения"

    # Счётчик месяца показывается рядом с серией и переживает обрыв (ТЗ 7.2):
    # серия обнулилась, а месяц потерял ровно один день.
    month = after_bad["month"]
    assert month["days"] - month["clean"] == 1


async def test_day_without_review_is_not_counted(app_client, factory) -> None:
    await setup(app_client)
    uid = await user_id(app_client)
    day = await push_on(app_client, 2, [SYSTEM_TAG])
    await close_day(factory, uid, day)
    await app_client.put(
        f"/api/v1/entries/day/{day}",
        headers=csrf(app_client),
        json={"score": 4, "body": "Запись есть, разбора нет."},
    )

    body = await streak(app_client)
    mark = next(d for d in body["days"] if d["day"] == day)
    assert mark["reason"] == "no_review"
    assert body["current"] == 0


async def test_trading_without_admission_is_not_counted(app_client) -> None:
    """Сделки были, чек не проходился — день не зачтён (ТЗ 7.1)."""
    await setup(app_client)
    day = await push_on(app_client, 2, [SYSTEM_TAG])

    body = await streak(app_client)
    mark = next(d for d in body["days"] if d["day"] == day)
    assert mark["reason"] == "no_check"
    assert mark["counted"] is False


async def test_quiet_days_do_not_break_anything(app_client) -> None:
    """Выходные без торговли и без записи в расчёт не входят вовсе."""
    body = await setup(app_client) or await streak(app_client)
    assert body["days"] == []
    assert body["current"] == 0
    assert body["conditions"]


async def test_freeze_is_neutral_and_limited(app_client) -> None:
    await setup(app_client)
    today = (await app_client.get("/api/v1/today")).json()["day"]
    tomorrow = (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat()

    first = await app_client.post(
        "/api/v1/streak/freeze", headers=csrf(app_client), json={"day": today}
    )
    assert first.status_code == 200, first.text
    assert first.json()["freezes"] == {
        "used": 1,
        "left": 1,
        "per_month": 2,
        "month": today[:7],
    }

    second = await app_client.post(
        "/api/v1/streak/freeze", headers=csrf(app_client), json={"day": tomorrow}
    )
    assert second.status_code == 200
    assert second.json()["freezes"]["left"] == 0

    # Повтор по уже замороженному дню ничего не стоит: иначе второй клик
    # по кнопке сжигал бы заморозку, которой трейдер не распоряжался.
    again = await app_client.post(
        "/api/v1/streak/freeze", headers=csrf(app_client), json={"day": tomorrow}
    )
    assert again.status_code == 200
    assert again.json()["freezes"]["used"] == 2

    after = (dt.date.fromisoformat(today) + dt.timedelta(days=2)).isoformat()
    third = await app_client.post(
        "/api/v1/streak/freeze", headers=csrf(app_client), json={"day": after}
    )
    assert third.status_code == 409
    assert third.json()["error"]["code"] == "no_freezes_left"


async def test_past_day_cannot_be_frozen(app_client) -> None:
    """Иначе заморозка — способ переписать историю задним числом (ТЗ 9.2)."""
    await setup(app_client)
    today = (await app_client.get("/api/v1/today")).json()["day"]
    yesterday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()

    res = await app_client.post(
        "/api/v1/streak/freeze", headers=csrf(app_client), json={"day": yesterday}
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "day_in_past"


async def test_today_shows_the_streak(app_client) -> None:
    await setup(app_client)
    body = (await app_client.get("/api/v1/today")).json()
    assert body["streak"]["current"] == 0
    assert body["streak"]["freezes"]["left"] == 2
    assert len(body["streak"]["conditions"]) == 5
    # Пока нарушений не было, месяц чист целиком.
    assert body["streak"]["month"]["clean"] == body["streak"]["month"]["days"]
