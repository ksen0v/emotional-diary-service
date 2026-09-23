"""Движок через HTTP: два стопа подряд → блокировка → снятие разбором.

Это проверка из плана разработки для шага 9 дословно: «подал две убыточные
сделки — получил блокировку; таймер идёт, снятие требует разбора». Плюс то,
что руками не проверишь: compliance по времени открытия сделки и то, что
правило не срабатывает второй раз, пока условие продолжает выполняться.
"""

import asyncio
import datetime as dt

import httpx
import pytest

from tests.test_identity import csrf, move_day_boundary_away, register
from tests.test_premarket import BEST
from tests.test_trades_flow import push, sync

pytestmark = pytest.mark.usefixtures("clean_users")

TWO_STOPS = {
    "name": "2 стопа подряд",
    "conditions": [{"metric": "loss_streak", "cmp": "ge", "value": 2}],
    "actions": {"alert": True, "lock": {"enabled": True, "minutes": 30}, "buddy": False},
    "unlock": {"timer": True, "review": True, "buddy": False},
}

ANSWERS = {
    "q1": "Второй стоп подряд, полез отбивать.",
    "q2": "Не торговать после двух стопов.",
    "q3": "Закрою терминал и пойду пройдусь.",
}


async def setup(client: httpx.AsyncClient, *, rule: dict | None = None) -> dict:
    await register(client)
    await move_day_boundary_away(client)
    res = await client.post("/api/v1/source/connections/fake", headers=csrf(client))
    assert res.status_code == 200, res.text
    res = await client.post(
        "/api/v1/premarket/checks", headers=csrf(client), json={"answers": BEST}
    )
    assert res.status_code == 200, res.text
    res = await client.post(
        "/api/v1/rules", headers=csrf(client), json=rule or TWO_STOPS
    )
    assert res.status_code == 201, res.text
    return res.json()


async def today(client: httpx.AsyncClient) -> dict:
    res = await client.get("/api/v1/today")
    assert res.status_code == 200, res.text
    return res.json()


async def two_stops(client: httpx.AsyncClient) -> dict:
    """Два значимых стопа подряд — сценарий Б из дизайна.

    Сделки закрываются «только что»: правило проверяет то, что случилось после
    того, как оно появилось, и backdated-сделки оно сознательно пропускает.
    """
    await push(client, profit="-84.20", pct="-0.68", minutes_ago=0, duration_sec=420)
    await push(
        client,
        profit="-91.00",
        pct="-0.72",
        minutes_ago=0,
        duration_sec=300,
        symbol="ETHUSDT",
    )
    return await sync(client)


# --- проверка из плана ---


async def test_two_stops_start_a_lock(app_client: httpx.AsyncClient) -> None:
    await setup(app_client)
    report = await two_stops(app_client)
    assert report["engine"]["fired"] == 1
    assert report["engine"]["locks_started"] == 1

    body = await today(app_client)
    assert body["state"] == "locked"

    lock = body["lock"]
    assert lock is not None
    assert lock["rule_name"] == "2 стопа подряд"
    assert lock["requires"] == {"timer": True, "review": True, "buddy": False}
    # Три состояния, а не два: None — условие выключено, False — не выполнено.
    assert lock["satisfied"] == {"timer": False, "review": False, "buddy": None}
    assert lock["can_lift"] is False
    assert lock["timer_until"] > lock["started_at"]
    # Граница дня — жёсткий предел поверх таймера (ТЗ 6.6).
    assert lock["window_until"] >= lock["timer_until"]
    assert len(lock["review_questions"]) == 3
    # Доверенное лицо появится на шаге 13, и обещать его нечем.
    assert lock["buddy"] is None


async def test_lock_is_not_lifted_until_review_is_filled(
    app_client: httpx.AsyncClient,
) -> None:
    await setup(app_client)
    await two_stops(app_client)
    lock_id = (await today(app_client))["lock"]["id"]

    res = await client_review(app_client, lock_id, {"q1": "мало", "q2": "мало", "q3": "мало"})
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "validation_failed"

    res = await client_review(app_client, lock_id, ANSWERS)
    assert res.status_code == 200, res.text
    body = res.json()
    # Разбор заполнен, но таймер ещё идёт: снятие — конъюнкция включённых условий.
    assert body["satisfied"] == {"timer": False, "review": True, "buddy": None}
    assert body["lifted"] is False
    assert body["lock_state"] == "active"
    assert "таймер" in body["message"].lower()

    assert (await today(app_client))["state"] == "locked"


async def test_review_alone_lifts_when_timer_is_off(
    app_client: httpx.AsyncClient,
) -> None:
    """«Только разбор, без ожидания» — законная сборка правила (ТЗ 6.6)."""
    rule = {
        **TWO_STOPS,
        "unlock": {"timer": False, "review": True, "buddy": False},
    }
    await setup(app_client, rule=rule)
    await two_stops(app_client)

    lock = (await today(app_client))["lock"]
    assert lock["satisfied"] == {"timer": None, "review": False, "buddy": None}
    # Таймера нет — значит и времени снятия нет.
    assert lock["timer_until"] is None

    res = await client_review(app_client, lock["id"], ANSWERS)
    assert res.status_code == 200, res.text
    assert res.json()["lifted"] is True

    body = await today(app_client)
    assert body["lock"] is None
    assert body["state"] == "trading"


async def test_lock_without_conditions_cannot_be_lifted_by_hand(
    app_client: httpx.AsyncClient,
) -> None:
    """Ни одного включённого условия — «сегодня я больше не торгую» (ТЗ 6.6)."""
    rule = {**TWO_STOPS, "unlock": {"timer": False, "review": False, "buddy": False}}
    await setup(app_client, rule=rule)
    await two_stops(app_client)

    lock = (await today(app_client))["lock"]
    assert lock["satisfied"] == {"timer": None, "review": None, "buddy": None}
    assert lock["can_lift"] is False

    res = await client_review(app_client, lock["id"], ANSWERS)
    assert res.status_code == 200, res.text
    # Разбор приняли, но снять нечем: блокировка кончится на границе дня.
    assert res.json()["lifted"] is False


async def client_review(
    client: httpx.AsyncClient, lock_id: str, answers: dict
) -> httpx.Response:
    return await client.post(
        f"/api/v1/locks/{lock_id}/review", headers=csrf(client), json=answers
    )


# --- срабатывание как событие, а не состояние ---


async def test_rule_does_not_fire_again_while_condition_holds(
    app_client: httpx.AsyncClient,
) -> None:
    """Третий стоп подряд не даёт второго инцидента.

    Срабатывание — переход, а не состояние. Иначе «2 убыточных подряд» дало бы
    по блокировке на каждую следующую убыточную сделку.
    """
    await setup(app_client)
    await two_stops(app_client)

    await push(
        app_client,
        profit="-40.00",
        pct="-0.60",
        minutes_ago=0,
        duration_sec=120,
        symbol="SOLUSDT",
    )
    report = await sync(app_client)
    assert report["engine"]["fired"] == 0
    assert report["engine"]["locks_started"] == 0


async def test_repeated_sync_changes_nothing(app_client: httpx.AsyncClient) -> None:
    """Повторная сверка приносит те же сделки — инцидент должен остаться один."""
    await setup(app_client)
    await two_stops(app_client)
    report = await sync(app_client)
    assert report["engine"]["fired"] == 0

    res = await app_client.get("/api/v1/locks/active")
    assert res.status_code == 200
    assert res.json()["lock"] is not None


async def test_rule_does_not_fire_on_trades_older_than_itself(
    app_client: httpx.AsyncClient,
) -> None:
    """Только что собранное правило не срабатывает задним числом на утренних стопах."""
    await register(app_client)
    await move_day_boundary_away(app_client)
    await app_client.post("/api/v1/source/connections/fake", headers=csrf(app_client))
    await app_client.post(
        "/api/v1/premarket/checks", headers=csrf(app_client), json={"answers": BEST}
    )
    # Сделки приехали и были приняты до того, как появилось правило.
    await push(app_client, profit="-84.20", pct="-0.68", minutes_ago=12)
    await push(
        app_client, profit="-91.00", pct="-0.72", minutes_ago=10, symbol="ETHUSDT"
    )
    await sync(app_client)

    res = await app_client.post("/api/v1/rules", headers=csrf(app_client), json=TWO_STOPS)
    assert res.status_code == 201, res.text

    report = await sync(app_client)
    assert report["engine"]["fired"] == 0
    assert (await today(app_client))["lock"] is None


# --- compliance ---


async def test_trade_opened_during_lock_breaches_it(
    app_client: httpx.AsyncClient,
) -> None:
    await setup(app_client)
    await two_stops(app_client)

    # Блокировка началась мгновение назад. Ждём, чтобы у сделки, открытой
    # «секунду назад», время открытия оказалось заведомо внутри окна.
    await asyncio.sleep(1.5)
    await push(
        app_client,
        profit="12.00",
        pct="0.03",
        minutes_ago=0,
        duration_sec=1,
        symbol="XRPUSDT",
    )
    report = await sync(app_client)
    assert report["engine"]["breaches"] == 1

    lock = (await today(app_client))["lock"]
    assert lock is not None
    # Блокировка не снимается в наказание: экран остаётся, сверху полоса.
    assert lock["state"] == "active"
    assert lock["breach"] is not None
    assert lock["breach"]["first"]["symbol"] == "XRPUSDT"


async def test_trade_opened_before_lock_is_not_a_breach(
    app_client: httpx.AsyncClient,
) -> None:
    """Сделка, открытая до блокировки и закрытая внутри неё, нарушением не является.

    Это место, где ошибка тихо превращает сервис в несправедливый
    (Архитектура ч.2 §4.4), поэтому проверка стоит отдельным тестом.
    """
    await setup(app_client)
    await two_stops(app_client)

    await push(
        app_client,
        profit="12.00",
        pct="0.03",
        minutes_ago=0,
        duration_sec=1800,  # открыта полчаса назад, закрыта только что
        symbol="XRPUSDT",
    )
    report = await sync(app_client)
    assert report["engine"]["breaches"] == 0

    lock = (await today(app_client))["lock"]
    assert lock["breach"] is None


async def test_session_cannot_be_closed_while_locked(
    app_client: httpx.AsyncClient,
) -> None:
    """Иначе закрытие сессии стало бы способом снять блокировку."""
    await setup(app_client)
    await two_stops(app_client)

    res = await app_client.post("/api/v1/session/close", headers=csrf(app_client))
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "lock_active"


# --- режим наблюдения ---


async def test_shadow_mode_records_incident_without_lock(
    app_client: httpx.AsyncClient,
) -> None:
    """Правила считаются, инциденты пишутся, блокировки не применяются (ч.2 §5.10)."""
    await setup(app_client)
    res = await app_client.patch(
        "/api/v1/me/settings", headers=csrf(app_client), json={"shadow_mode": True}
    )
    assert res.status_code == 200, res.text

    report = await two_stops(app_client)
    assert report["engine"]["fired"] == 1
    assert report["engine"]["locks_started"] == 0
    assert report["engine"]["shadow"] is True

    body = await today(app_client)
    assert body["lock"] is None
    assert body["state"] == "trading"
    assert body["shadow_mode"] is True


# --- блок «ближе всего к срабатыванию» ---


async def test_near_rules_show_distance_to_the_border(
    app_client: httpx.AsyncClient,
) -> None:
    await setup(app_client)
    body = await today(app_client)
    assert body["near_rules"] == [
        {
            "rule_id": body["near_rules"][0]["rule_id"],
            "name": "2 стопа подряд",
            "metric": "loss_streak",
            "metric_name": "Убыточных сделок подряд",
            "value_text": "0 из 2",
            "ratio": "0",
            "met": False,
            "hot": False,
        }
    ]

    await push(app_client, profit="-84.20", pct="-0.68", minutes_ago=12)
    await sync(app_client)

    row = (await today(app_client))["near_rules"][0]
    assert row["value_text"] == "1 из 2"
    assert row["ratio"] == "0.5"
    # Янтарным — ближайшее правило со второй половины пути.
    assert row["hot"] is True


async def test_counters_come_from_the_engine(app_client: httpx.AsyncClient) -> None:
    """Счётчики на экране и счётчики движка — одна и та же функция, не две."""
    await setup(app_client)
    await two_stops(app_client)

    counters = (await today(app_client))["counters"]
    assert counters["loss_streak"] == 2
    assert counters["all_trades"] == 2
    assert counters["significant_trades"] == 2
    assert counters["equity_pct"] == "-1.40"
    assert counters["peak_pct"] == "0.00"
    assert counters["drawdown_pct"] == "1.40"
    assert counters["loss_sum_pct"] == "1.40"
    # Источник без открытых позиций их не даёт: null, а не ноль.
    assert counters["unrealized_pct"] is None
    assert counters["drawdown_full_pct"] is None


async def test_fired_counter_appears_in_the_rule_card(
    app_client: httpx.AsyncClient,
) -> None:
    """Счётчик срабатываний в карточке правила — настоящий, а не ноль-заглушка."""
    await setup(app_client)
    await two_stops(app_client)

    res = await app_client.get("/api/v1/rules")
    assert res.status_code == 200
    user_rules = [r for r in res.json()["rules"] if r["kind"] == "user"]
    assert user_rules[0]["fired_last_30d"] == 1


async def test_lock_expires_with_the_trading_day(
    app_client: httpx.AsyncClient,
) -> None:
    """Блокировка не переходит на следующий торговый день (ТЗ 6.6)."""
    await setup(app_client)
    await two_stops(app_client)

    lock = (await today(app_client))["lock"]
    window = dt.datetime.fromisoformat(lock["window_until"])
    day_ends = dt.datetime.fromisoformat((await today(app_client))["day_ends_at"])
    assert window == day_ends


async def test_day_curve_agrees_with_engine_counters(
    app_client: httpx.AsyncClient,
) -> None:
    """Кривая дня и счётчики движка обязаны сходиться в последней точке.

    Считают их разные модули — trades рисует кривую, rules ведёт счётчики, —
    и разойтись они могут незаметно: на экране «Сегодня» кривая и плитки стоят
    рядом, и расхождение читалось бы как «сервис что-то путает».
    """
    await setup(app_client)
    await two_stops(app_client)
    await push(
        app_client, profit="60.00", pct="0.40", minutes_ago=0, duration_sec=60,
        symbol="ADAUSDT",
    )
    await sync(app_client)

    counters = (await today(app_client))["counters"]
    res = await app_client.get("/api/v1/trades/day-curve")
    assert res.status_code == 200, res.text
    last = res.json()["points"][-1]

    assert last["equity_pct"] == counters["equity_pct"]
    assert last["peak_pct"] == counters["peak_pct"]
    assert last["drawdown_pct"] == counters["drawdown_pct"]
