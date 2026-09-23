"""Системные триггеры SR-1…SR-4 через HTTP и лента инцидентов.

Это проверка из плана разработки для шага 10 дословно: «открыл сделку во время
блокировки — инцидент стал нарушенным, стрик сгорел». Плюс то, что руками
проверяется дольше всего и потому проверяется тестом: поздний тег, который
обязан ничего не делать до шага 11, и повторный проход сверки, который не
должен задваивать инциденты.

Каждый тест назван тем, что он защищает, а не тем, что он дёргает: через три
месяца по имени должно быть видно, какое решение сломалось.
"""

import asyncio
import datetime as dt

import httpx
import pytest

from eds.app.consumers import all_consumers
from tests.test_identity import csrf, move_day_boundary_away, register
from tests.test_premarket import BEST, WORST
from tests.test_trades_flow import VIOLATION_TAG, drain, push, sync

pytestmark = pytest.mark.usefixtures("clean_users")


async def connect(client: httpx.AsyncClient) -> None:
    res = await client.post("/api/v1/source/connections/fake", headers=csrf(client))
    assert res.status_code == 200, res.text


async def check(client: httpx.AsyncClient, answers: dict) -> httpx.Response:
    return await client.post(
        "/api/v1/premarket/checks", headers=csrf(client), json={"answers": answers}
    )


async def setup(client: httpx.AsyncClient, *, admission: dict | None = BEST) -> None:
    await register(client)
    await move_day_boundary_away(client)
    await connect(client)
    if admission is not None:
        res = await check(client, admission)
        assert res.status_code == 200, res.text


async def today(client: httpx.AsyncClient) -> dict:
    res = await client.get("/api/v1/today")
    assert res.status_code == 200, res.text
    return res.json()


async def incidents(client: httpx.AsyncClient, **params) -> dict:
    res = await client.get("/api/v1/incidents", params=params)
    assert res.status_code == 200, res.text
    return res.json()


async def mark_tag_as_violation(client: httpx.AsyncClient) -> None:
    """Отметить тег нарушением и догнать шину, как это делает приложение."""
    tags = (await client.get("/api/v1/source/tags")).json()["tags"]
    target = next(t for t in tags if t["name"] == VIOLATION_TAG)
    res = await client.put(
        "/api/v1/source/tags/violations",
        headers=csrf(client),
        json={"violation_tag_ids": [target["external_id"]]},
    )
    assert res.status_code == 200, res.text
    for consumer in all_consumers():
        await drain(consumer)


# --- SR-1: тег нарушения ---


async def test_violation_tag_locks_trading_until_end_of_day(
    app_client: httpx.AsyncClient,
) -> None:
    """Тег в тот же день → блокировка до границы дня (ТЗ 4.4).

    Именно до границы, а не на N минут: блокировка по тегу принадлежит
    торговому дню сделки, и таймера у неё нет.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=0)
    report = await sync(app_client)
    # До того как тег объявлен нарушением, сделка чистая и SR-1 молчит.
    assert report["engine"]["system_fired"] == 0

    await mark_tag_as_violation(app_client)

    body = await today(app_client)
    assert body["state"] == "locked"
    lock = body["lock"]
    assert lock is not None
    # Таймера нет: длительность у SR-1 не задана, окно закрывает граница дня.
    assert lock["timer_until"] is None
    assert lock["window_until"] == body["day_ends_at"]
    assert lock["satisfied"]["review"] is False

    feed = await incidents(app_client)
    assert feed["totals"]["count"] == 1
    item = feed["items"][0]
    assert item["code"] == "violation"
    assert item["title"] == "Несистемная сделка"
    assert "SR-1" in item["detail"]
    assert "до конца торгового дня" in item["detail"]


async def test_violation_tag_fires_once_per_trade(
    app_client: httpx.AsyncClient,
) -> None:
    """Повторная сверка не задваивает инцидент.

    Одно и то же событие приходит и из потока, и из сверки; без ключа повтора
    у трейдера к вечеру был бы десяток одинаковых строк в истории.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=0)
    await sync(app_client)
    await mark_tag_as_violation(app_client)

    await sync(app_client)
    await today(app_client)
    feed = await incidents(app_client)
    assert feed["totals"]["count"] == 1


async def test_late_tag_does_nothing_until_step_11(
    app_client: httpx.AsyncClient,
) -> None:
    """Тег на сделке прошлого дня не включает блокировку на сегодня.

    ТЗ 4.4: окно блокировки истекло вместе с тем днём. Разбор того дня —
    ретропроверка — это шаг 11, и пока её нет, честный ответ на поздний тег
    это НИЧЕГО: ни блокировки, ни инцидента без исхода.

    Проверяется тестом, а не руками, потому что руками для этого нужно
    дождаться следующего дня.
    """
    await setup(app_client)
    # Сделка вчерашняя: 26 часов назад заведомо за границей сегодняшнего дня.
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=26 * 60)
    await sync(app_client)
    await mark_tag_as_violation(app_client)

    body = await today(app_client)
    assert body["lock"] is None
    assert body["state"] != "locked"
    assert body["incidents"] == []

    # Инцидента по тегу нет ни за сегодня, ни за тот день: его запишет
    # ретропроверка на шаге 11 — вместе с исходом, который считает она же.
    # (Инцидент SR-3 за тот день при этом появиться может и должен: сделки
    # без допуска были, и к позднему тегу это отношения не имеет.)
    feed = await incidents(app_client, period="all")
    assert [i["code"] for i in feed["items"] if i["code"] == "violation"] == []


# --- SR-2: сделка во время блокировки ---


async def test_trade_during_lock_burns_the_streak(
    app_client: httpx.AsyncClient,
) -> None:
    """Проверка из плана: сделка «во время» → инцидент нарушен, стрик сгорел.

    Две записи об одном событии, и обе нужны: нарушенная блокировка отвечает
    «чем кончился тот инцидент», SR-2 — «что трейдер сделал».
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=0)
    await sync(app_client)
    await mark_tag_as_violation(app_client)
    assert (await today(app_client))["lock"] is not None

    # Блокировка началась мгновение назад. Ждём, чтобы у сделки, открытой
    # «секунду назад», время открытия оказалось заведомо внутри окна.
    await asyncio.sleep(1.5)
    await push(app_client, symbol="ETHUSDT", minutes_ago=0, duration_sec=1)
    await sync(app_client)

    feed = await incidents(app_client)
    codes = {item["code"]: item for item in feed["items"]}
    assert "lock_breached" in codes, feed["items"]
    breach = codes["lock_breached"]
    assert breach["outcome"] == "breached"
    assert breach["title"] == "Сделка во время блокировки"
    assert "стрик сброшен" in breach["detail"]
    # Исходный инцидент по тегу тоже помечен нарушенным.
    assert codes["violation"]["outcome"] == "breached"

    # Блокировка не снимается в наказание: экран остаётся, сверху полоса.
    body = await today(app_client)
    assert body["state"] == "locked"
    assert body["lock"]["breach"] is not None
    assert body["lock"]["breach"]["first"]["symbol"] == "ETHUSDT"


async def test_red_banner_says_what_happened_to_the_streak(
    app_client: httpx.AsyncClient,
) -> None:
    """Вторая фраза красной полосы (прототип Locked.dc.html).

    В прототипе фраз три: сделка, сгоревший стрик, сигнал Максиму. Третьей
    здесь нет и быть не может до шага 13 — про отправленный сигнал сервис
    врать не должен. А вторая обязана сказать и то, что стрик сгорел, и то,
    что счётчик в шапке до границы дня не изменится: иначе трейдер увидит
    прежнее число и решит, что обошлось.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=0)
    await sync(app_client)
    await mark_tag_as_violation(app_client)
    await asyncio.sleep(1.5)
    await push(app_client, symbol="ETHUSDT", minutes_ago=0, duration_sec=1)
    await sync(app_client)

    breach = (await today(app_client))["lock"]["breach"]
    assert breach is not None
    assert breach["streak_text"]
    assert "стрик" in breach["streak_text"].lower()
    # Про доверенное лицо не сказано ни слова.
    assert "сигнал" not in breach["streak_text"].lower()


async def test_breach_marks_the_day_as_not_counted(
    app_client: httpx.AsyncClient,
) -> None:
    """День с нарушенной блокировкой не зачитывается (ТЗ 7.1, пункт 4).

    Стрик считается по завершённым дням, поэтому смотрим на отметку дня,
    а не на число в шапке: сегодняшний день в серию ещё не входит.
    """
    from eds.app import streaks as app_streaks
    from eds.contracts.trading_time import trading_day
    from eds.modules.identity import repo as identity_repo
    from eds.platform import auth, db
    from tests.test_identity import EMAIL

    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=0)
    await sync(app_client)
    await mark_tag_as_violation(app_client)
    await asyncio.sleep(1.5)
    await push(app_client, symbol="ETHUSDT", minutes_ago=0, duration_sec=1)
    await sync(app_client)

    async with db.session_factory()() as s:
        user = await identity_repo.user_by_email(s, EMAIL)
        assert user is not None
        prefs = await auth.prefs_of(s, user.id)
        day = trading_day(dt.datetime.now(dt.UTC), prefs.timezone, prefs.day_cutoff)
        # Пересчитываем «как будто день закончился»: отметка ставится
        # завершённым дням, а сегодняшний ещё идёт.
        facts = await app_streaks.facts_for(s, user.id, [day])

    assert facts[0].lock_breaches == 1


# --- SR-3: торговля без допуска ---


async def test_trading_without_admission_opens_an_incident(
    app_client: httpx.AsyncClient,
) -> None:
    """Чек провален, а сделки есть → «торговля без допуска» (ТЗ 5.2).

    Блокировки у SR-3 нет: день уже закрыт экраном «допуска на сегодня нет»,
    и вторая закрытая дверь поверх первой ничего не добавляет.
    """
    await setup(app_client, admission=None)
    res = await check(app_client, WORST)
    assert res.status_code == 200, res.text
    assert res.json()["verdict"] == "denied"

    await push(app_client, minutes_ago=0)
    await sync(app_client)

    body = await today(app_client)
    assert body["state"] == "check_failed"
    assert body["lock"] is None

    feed = await incidents(app_client)
    assert feed["totals"]["count"] == 1
    item = feed["items"][0]
    assert item["code"] == "no_admission"
    assert item["outcome"] == "breached"
    assert "SR-3" in item["detail"]
    assert "стрик сброшен" in item["detail"]


async def test_trading_without_any_check_is_the_same_incident(
    app_client: httpx.AsyncClient,
) -> None:
    """Чека нет вовсе, а сделки есть — тот же инцидент (ТЗ 5.2, «то же самое»).

    Это второй вход в SR-3, и перепутать его с первым легко: там допуск
    отказан, здесь допуска не спрашивали.
    """
    await setup(app_client, admission=None)
    await push(app_client, minutes_ago=0)
    await sync(app_client)

    feed = await incidents(app_client)
    assert feed["totals"]["count"] == 1
    item = feed["items"][0]
    assert item["code"] == "no_admission"
    assert "чек не пройден" in item["detail"]


async def test_no_admission_incident_is_written_once_a_day(
    app_client: httpx.AsyncClient,
) -> None:
    """Каждая следующая сделка не добавляет нового инцидента за тот же день."""
    await setup(app_client, admission=None)
    await push(app_client, minutes_ago=0)
    await sync(app_client)
    await push(app_client, symbol="ETHUSDT", minutes_ago=0)
    await sync(app_client)

    assert (await incidents(app_client))["totals"]["count"] == 1


# --- SR-4: неразмеченная сделка ---


async def test_unmarked_trade_is_reminded_after_the_threshold(
    app_client: httpx.AsyncClient,
) -> None:
    """Сделка без разметки дольше N минут → строка на «Сегодня» (ТЗ 6.5).

    Напоминание, а не инцидент: забытая разметка закрывается одним кликом,
    и запись её в историю засорила бы ленту тем, что не является нарушением.
    """
    await setup(app_client)
    # Порог SR-4 по умолчанию 15 минут: сделка, закрытая 20 минут назад,
    # его прошла, а свежая — нет.
    await push(app_client, minutes_ago=20, duration_sec=60)
    await push(app_client, symbol="ETHUSDT", minutes_ago=0, duration_sec=60)
    await sync(app_client)

    body = await today(app_client)
    codes = {item["code"]: item for item in body["attention"]}
    assert "unmarked_overdue" in codes, body["attention"]
    assert codes["unmarked_overdue"]["count"] == 1
    # Обе сделки не размечены, но просрочена только одна — это разные новости.
    assert codes["unmarked_trades"]["count"] == 2
    # И ни одного инцидента из-за этого.
    assert (await incidents(app_client))["totals"]["count"] == 0


# --- лента и сводка ---


async def test_feed_summary_counts_the_whole_filter(
    app_client: httpx.AsyncClient,
) -> None:
    """Сводка над лентой считается по выборке, а не по странице.

    Иначе полоса «За сентябрь · Соблюдено · Нарушено» менялась бы при
    прокрутке, и доверие к числам кончилось бы на втором экране.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=0)
    await sync(app_client)
    await mark_tag_as_violation(app_client)
    await asyncio.sleep(1.5)
    await push(app_client, symbol="ETHUSDT", minutes_ago=0, duration_sec=1)
    await sync(app_client)

    feed = await incidents(app_client, limit=1)
    assert len(feed["items"]) == 1
    assert feed["has_more"] is True
    assert feed["totals"]["count"] == 2
    assert feed["totals"]["breached"] == 2
    # Коэффициент дисциплины: доля соблюдённых среди закончившихся.
    assert feed["totals"]["discipline_pct"] == 0.0
    assert feed["period"]["label"].startswith("За ")

    only_breached = await incidents(app_client, outcome="breached")
    assert only_breached["totals"]["count"] == 2


async def test_today_block_and_feed_describe_the_same_event_alike(
    app_client: httpx.AsyncClient,
) -> None:
    """Блок «Инциденты сегодня» и раздел «Инциденты» — один сборщик строки.

    Разные формулировки одного события читаются как два разных события,
    и разбираться в этом трейдер будет в худший момент дня.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=0)
    await sync(app_client)
    await mark_tag_as_violation(app_client)

    block = (await today(app_client))["incidents"]
    feed = (await incidents(app_client))["items"]
    assert len(block) == 1
    assert block[0]["id"] == feed[0]["id"]
    assert block[0]["title"] == feed[0]["title"]
    assert block[0]["detail"] == feed[0]["detail"]
    assert block[0]["time_text"] == feed[0]["time_text"]


async def test_history_cannot_be_edited_or_deleted(
    app_client: httpx.AsyncClient,
) -> None:
    """У ленты нет ни правки, ни удаления (ТЗ 9.2).

    Проверяется на уровне маршрутов: метода просто не существует, и это
    сильнее, чем проверка внутри обработчика.
    """
    await setup(app_client)
    feed = await incidents(app_client)
    assert feed["items"] == []

    res = await app_client.delete("/api/v1/incidents/whatever", headers=csrf(app_client))
    assert res.status_code in (404, 405)
