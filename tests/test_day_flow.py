"""Торговый день через HTTP: чек, три исхода, состояния, закрытие сессии.

Проверяется то, на чём стоит весь сервис: сессии не существует без чека,
а чек проходится один раз в день. Если это обходится, остальное не имеет смысла.
"""

import datetime as dt

import httpx
import pytest
from sqlalchemy import text

from tests.test_identity import csrf, move_day_boundary_away, register
from tests.test_premarket import BEST, MIDDLE, WORST
from tests.test_trades_flow import push, sync

pytestmark = pytest.mark.usefixtures("clean_users")


async def setup(client: httpx.AsyncClient, *, with_source: bool = True) -> None:
    await register(client)
    await move_day_boundary_away(client)
    if with_source:
        res = await client.post("/api/v1/source/connections/fake", headers=csrf(client))
        assert res.status_code == 200, res.text


async def today(client: httpx.AsyncClient) -> dict:
    res = await client.get("/api/v1/today")
    assert res.status_code == 200, res.text
    return res.json()


async def check(client: httpx.AsyncClient, answers: dict) -> httpx.Response:
    return await client.post(
        "/api/v1/premarket/checks", headers=csrf(client), json={"answers": answers}
    )


async def user_id(client: httpx.AsyncClient) -> str:
    res = await client.get("/api/v1/me")
    return res.json()["user"]["id"]


async def test_without_source_nothing_to_count(app_client) -> None:
    await setup(app_client, with_source=False)
    body = await today(app_client)
    assert body["state"] == "no_source"
    assert body["source"] is None
    assert body["admission"] is None


async def test_session_does_not_exist_until_check_passed(app_client) -> None:
    await setup(app_client)
    body = await today(app_client)
    assert body["state"] == "no_check"
    assert body["session"]["opened_at"] is None
    assert body["thresholds"] == {"pass_score": 19, "min_score": 13}
    # Границы дня сервер считает сам: фронт таймеры не выводит.
    assert body["day_ends_at"] > body["server_time"]


async def test_green_opens_session(app_client) -> None:
    await setup(app_client)
    res = await check(app_client, BEST)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["verdict"] == "green"
    assert body["score"] == 25
    assert body["max_score"] == 25
    assert body["session_opened_at"] is not None
    assert body["restrictions"] is None
    assert body["weak"] == []

    state = await today(app_client)
    assert state["state"] == "trading"
    assert state["admission"]["verdict"] == "green"
    assert state["admission"]["score"] == 25
    assert state["admission"]["checked_at"] is not None
    assert state["session"]["opened_at"] is not None


async def test_red_opens_session_with_a_reminder_not_a_limit(app_client) -> None:
    await setup(app_client)
    body = (await check(app_client, MIDDLE)).json()
    assert body["verdict"] == "red"
    assert body["score"] == 15
    assert body["session_opened_at"] is not None
    assert "половина" in body["restrictions"]["text"]

    state = await today(app_client)
    assert state["state"] == "trading"
    assert state["admission"]["verdict"] == "red"


async def test_denied_keeps_session_closed(app_client) -> None:
    await setup(app_client)
    body = (await check(app_client, WORST)).json()
    assert body["verdict"] == "denied"
    assert body["score"] == 5
    assert body["session_opened_at"] is None
    assert "порог допуска" in body["message"]

    state = await today(app_client)
    assert state["state"] == "check_failed"
    assert state["session"]["opened_at"] is None


async def test_check_cannot_be_retaken(app_client) -> None:
    """Перепройти нельзя — в этом и смысл допуска (ТЗ 5.2)."""
    await setup(app_client)
    assert (await check(app_client, WORST)).json()["verdict"] == "denied"

    again = await check(app_client, BEST)
    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "already_done"
    assert error["details"]["verdict"] == "denied"

    catalog = await app_client.get("/api/v1/premarket/questions")
    assert catalog.json()["already_done"] is True


async def test_unclosed_review_blocks_the_check(app_client, factory) -> None:
    """Не разобрал вчера — нет допуска сегодня (ТЗ 5.4).

    Разбор появится на шаге 6, поэтому состояние выставляется напрямую:
    запрет должен работать уже сейчас, иначе на шаге 6 он окажется ненаписанным.
    """
    await setup(app_client)
    uid = await user_id(app_client)
    body = await today(app_client)

    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO daybook.trading_days (user_id, day, review_state) "
                "VALUES (:uid, :day, 'pending') "
                "ON CONFLICT (user_id, day) DO UPDATE SET review_state = 'pending'"
            ),
            {"uid": uid, "day": dt.date.fromisoformat(body["day"])},
        )
        await s.commit()

    res = await check(app_client, BEST)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "review_pending"


async def test_questions_come_from_the_server(app_client) -> None:
    await setup(app_client)
    res = await app_client.get("/api/v1/premarket/questions")
    body = res.json()
    assert [q["id"] for q in body["questions"]] == [
        "sleep",
        "emotion",
        "yesterday",
        "revenge",
        "plan",
    ]
    revenge = next(q for q in body["questions"] if q["id"] == "revenge")
    # Переворот виден из подписей, а не из сноски (решение дизайна Э-05).
    assert revenge["inverted"] is True
    assert revenge["labels"]["1"] and revenge["labels"]["5"]
    assert body["max_score"] == 25


async def test_session_closes_by_hand_once(app_client) -> None:
    await setup(app_client)
    await check(app_client, BEST)

    res = await app_client.post("/api/v1/session/close", headers=csrf(app_client))
    assert res.status_code == 200, res.text
    assert res.json()["closed_at"]

    state = await today(app_client)
    assert state["state"] == "session_closed"
    assert state["session"]["closed_at"] is not None

    again = await app_client.post("/api/v1/session/close", headers=csrf(app_client))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_closed"


async def test_closing_without_session_is_refused(app_client) -> None:
    await setup(app_client)
    res = await app_client.post("/api/v1/session/close", headers=csrf(app_client))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "no_session"


async def test_finished_day_closes_itself_on_read(app_client, factory) -> None:
    """День, который кончился, не должен оставаться открытым.

    Процесс границы дня появится вместе с уведомлениями; до тех пор закрытие
    делается на чтении — иначе вчерашняя сессия «шла» бы вечно.
    """
    await setup(app_client)
    uid = await user_id(app_client)
    body = await today(app_client)
    yesterday = dt.date.fromisoformat(body["day"]) - dt.timedelta(days=1)

    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO daybook.trading_days "
                "(user_id, day, admission, check_score, session_opened_at, review_state) "
                "VALUES (:uid, :day, 'green', 21, :opened, 'none')"
            ),
            {
                "uid": uid,
                "day": yesterday,
                "opened": dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
            },
        )
        await s.commit()

    await today(app_client)

    async with factory() as s:
        res = await s.execute(
            text(
                "SELECT session_closed_at FROM daybook.trading_days "
                "WHERE user_id = :uid AND day = :day"
            ),
            {"uid": uid, "day": yesterday},
        )
        closed_at = res.scalar_one()
    assert closed_at is not None
    # Закрылась на границе дня, а не в момент чтения.
    assert closed_at.date() in (yesterday, yesterday + dt.timedelta(days=1))


async def test_day_counters_come_from_real_trades(app_client) -> None:
    await setup(app_client)
    await check(app_client, BEST)

    await push(app_client, profit="-84.20", pct="-0.68", minutes_ago=20)
    await push(app_client, profit="-91.00", pct="-0.72", minutes_ago=10)
    await push(app_client, profit="-1.20", pct="-0.04", minutes_ago=5)  # пыль
    await sync(app_client)

    counters = (await today(app_client))["counters"]
    assert counters["all_trades"] == 3
    assert counters["significant_trades"] == 2
    # Пыль прозрачна для серии: два значимых стопа, а не три (ТЗ 6.2).
    assert counters["loss_streak"] == 2
    assert float(counters["equity_pct"]) == -1.44
    assert float(counters["drawdown_pct"]) == 1.44
    assert float(counters["loss_sum_pct"]) == 1.44
    assert float(counters["profit_usd"]) == -176.40
    assert counters["unrealized_pct"] is None


async def test_profit_day_has_no_loss_sum(app_client) -> None:
    await setup(app_client)
    await check(app_client, BEST)
    await push(app_client, profit="+126.40", pct="+1.02", minutes_ago=10)
    await sync(app_client)

    counters = (await today(app_client))["counters"]
    assert counters["loss_streak"] == 0
    # Убыток суммарно — ноль, а не отрицательное число: показатель назван убытком.
    assert float(counters["loss_sum_pct"]) == 0
    assert float(counters["drawdown_pct"]) == 0


async def test_unmarked_trades_get_into_attention(app_client) -> None:
    await setup(app_client)
    await check(app_client, BEST)
    await push(app_client, profit="-84.20", pct="-0.68", minutes_ago=10, tags=[])
    await sync(app_client)

    body = await today(app_client)
    codes = {item["code"] for item in body["attention"]}
    assert "unmarked_trades" in codes


async def test_admission_publishes_events(app_client, factory) -> None:
    """Допуск и открытие сессии — события для правил, стриков и уведомлений."""
    await setup(app_client)
    uid = await user_id(app_client)
    await check(app_client, BEST)

    async with factory() as s:
        res = await s.execute(
            text(
                "SELECT event_type FROM events.outbox "
                "WHERE payload->>'user_id' = :uid ORDER BY id"
            ),
            {"uid": uid},
        )
        types = [row[0] for row in res]
    assert "daybook.admission_decided" in types
    assert "daybook.session_opened" in types


async def test_denied_does_not_publish_session_opened(app_client, factory) -> None:
    await setup(app_client)
    uid = await user_id(app_client)
    await check(app_client, WORST)

    async with factory() as s:
        res = await s.execute(
            text(
                "SELECT event_type FROM events.outbox "
                "WHERE payload->>'user_id' = :uid ORDER BY id"
            ),
            {"uid": uid},
        )
        types = [row[0] for row in res]
    assert "daybook.admission_decided" in types
    assert "daybook.session_opened" not in types
