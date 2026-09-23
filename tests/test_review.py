"""Пост-сессионный разбор и его власть над чеком.

Жёсткость сознательная (ОВ-15): пока разбор за прошлый день не заполнен,
допуск не выдаётся, и других путей нет. Проверяется весь цикл — от закрытия
сессии до разблокировки чека, потому что по частям он ничего не значит.
"""

import datetime as dt

import httpx
import pytest
from sqlalchemy import text

from tests.test_identity import csrf, move_day_boundary_away, register
from tests.test_premarket import BEST

pytestmark = pytest.mark.usefixtures("clean_users")


async def setup(client: httpx.AsyncClient) -> None:
    await register(client)
    await move_day_boundary_away(client)
    res = await client.post("/api/v1/source/connections/fake", headers=csrf(client))
    assert res.status_code == 200, res.text


async def today(client: httpx.AsyncClient) -> dict:
    res = await client.get("/api/v1/today")
    assert res.status_code == 200, res.text
    return res.json()


async def user_id(client: httpx.AsyncClient) -> str:
    res = await client.get("/api/v1/me")
    return res.json()["user"]["id"]


async def post_review(client: httpx.AsyncClient, day: dt.date, **over) -> httpx.Response:
    body = {
        "day": day.isoformat(),
        "plan_followed": "partial",
        "pull_text": "Хотел отбить второй стоп.",
        "execution_score": 3,
        "takeaway": "После второго стопа закрывать терминал.",
    }
    body.update(over)
    return await client.post("/api/v1/reviews", headers=csrf(client), json=body)


async def insert_closed_day(factory, uid: str, day: dt.date, state: str) -> None:
    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO daybook.trading_days "
                "(user_id, day, admission, check_score, session_opened_at, "
                " session_closed_at, review_state) "
                "VALUES (:uid, :day, 'green', 21, :opened, :closed, :state)"
            ),
            {
                "uid": uid,
                "day": day,
                "opened": dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
                "closed": dt.datetime.now(dt.UTC) - dt.timedelta(hours=12),
                "state": state,
            },
        )
        await s.commit()


async def test_review_of_a_day_without_session_is_refused(app_client) -> None:
    await setup(app_client)
    day = dt.date.fromisoformat((await today(app_client))["day"])
    res = await post_review(app_client, day)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "no_session"


async def test_review_while_the_day_is_running_is_refused(app_client) -> None:
    """Разбор в середине дня стал бы разбором заранее — разбирать ещё нечего."""
    await setup(app_client)
    await app_client.post(
        "/api/v1/premarket/checks", headers=csrf(app_client), json={"answers": BEST}
    )
    day = dt.date.fromisoformat((await today(app_client))["day"])

    res = await post_review(app_client, day)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "day_not_closed"


async def test_closing_session_puts_the_review_in_the_queue(app_client) -> None:
    await setup(app_client)
    await app_client.post(
        "/api/v1/premarket/checks", headers=csrf(app_client), json={"answers": BEST}
    )
    await app_client.post("/api/v1/session/close", headers=csrf(app_client))

    state = await today(app_client)
    # Разбор за сегодня ждёт, но сегодняшнему дню он уже не мешает:
    # чек пройден, сессия закрыта.
    assert state["review"]["state"] == "pending"
    assert state["review"]["pending_day"] is None
    assert state["state"] == "session_closed"

    day = dt.date.fromisoformat(state["day"])
    filled = await post_review(app_client, day)
    assert filled.status_code == 201, filled.text
    body = filled.json()
    assert body["review"]["plan_followed"] == "partial"
    assert body["review"]["execution_score"] == 3
    # Ответ показывает стрик: разбор входит в условия зачёта дня. Сегодняшний
    # день в серию ещё не входит — он не кончился, и нарушение может случиться
    # через минуту, поэтому ноль здесь правильный.
    assert body["streak"] == {"current": 0, "previous": 0, "best": 0}

    again = await post_review(app_client, day)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_done"

    after = await today(app_client)
    assert after["review"]["state"] == "done"

    saved = await app_client.get(f"/api/v1/reviews/{day.isoformat()}")
    assert saved.json()["review"]["takeaway"].startswith("После второго стопа")
    assert saved.json()["state"] == "done"


async def test_unfilled_review_blocks_the_check_and_review_unblocks_it(
    app_client, factory
) -> None:
    """Полный цикл: заперто вчерашним разбором → заполнил → чек снова доступен."""
    await setup(app_client)
    uid = await user_id(app_client)
    day = dt.date.fromisoformat((await today(app_client))["day"])
    yesterday = day - dt.timedelta(days=1)
    await insert_closed_day(factory, uid, yesterday, "pending")

    blocked = await app_client.post(
        "/api/v1/premarket/checks", headers=csrf(app_client), json={"answers": BEST}
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "review_pending"
    assert (await today(app_client))["state"] == "review_pending"

    filled = await post_review(app_client, yesterday, plan_followed="no")
    assert filled.status_code == 201, filled.text

    passed = await app_client.post(
        "/api/v1/premarket/checks", headers=csrf(app_client), json={"answers": BEST}
    )
    assert passed.status_code == 200, passed.text
    assert passed.json()["verdict"] == "green"

    state = await today(app_client)
    assert state["state"] == "trading"
    assert state["review"]["pending_day"] is None


async def test_bad_plan_answer_is_refused(app_client, factory) -> None:
    await setup(app_client)
    uid = await user_id(app_client)
    day = dt.date.fromisoformat((await today(app_client))["day"])
    yesterday = day - dt.timedelta(days=1)
    await insert_closed_day(factory, uid, yesterday, "pending")

    res = await post_review(app_client, yesterday, plan_followed="maybe")
    assert res.status_code == 400
    res = await post_review(app_client, yesterday, execution_score=9)
    assert res.status_code == 400


async def test_review_publishes_event(app_client, factory) -> None:
    """Разбор входит в условия стрика, поэтому о нём узнают через шину."""
    await setup(app_client)
    uid = await user_id(app_client)
    day = dt.date.fromisoformat((await today(app_client))["day"])
    yesterday = day - dt.timedelta(days=1)
    await insert_closed_day(factory, uid, yesterday, "pending")
    await post_review(app_client, yesterday)

    async with factory() as s:
        res = await s.execute(
            text(
                "SELECT event_type FROM events.outbox "
                "WHERE payload->>'user_id' = :uid ORDER BY id"
            ),
            {"uid": uid},
        )
        types = [row[0] for row in res]
    assert "daybook.review_completed" in types
