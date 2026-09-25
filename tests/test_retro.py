"""Поздний тег: ретропроверка закрытого дня и ретропересчёт (шаг 11, ТЗ 4.4).

Проверка из плана разработки дословно: «поставил тег на сделку прошлого дня —
инцидент записан в тот день, стрик пересчитан, сегодняшний день не тронут».

Руками это проверяется медленно — надо дождаться следующего дня, — поэтому
здесь лежит вся таблица случаев: обе ветки исхода, повторный проход,
живой тег, который переразбирать нельзя, и пересчёт серии из середины
истории. Архитектура ч.2 §4.4 называет это место риском №1: «здесь легко
получить стрик, который скачет между перезагрузками».

Каждый тест назван тем решением, которое он защищает.
"""

import datetime as dt

import httpx
import pytest

from eds.app import retro
from eds.app import streaks as app_streaks
from eds.contracts.streaks import DayMark
from eds.contracts.trading_time import day_ends_at, trading_day
from eds.modules.identity import repo as identity_repo
from eds.modules.incidents import service as incidents_service
from eds.modules.streaks import rules as streak_rules
from eds.platform import auth, db
from tests.test_identity import EMAIL
from tests.test_system_rules import incidents, mark_tag_as_violation, setup, today
from tests.test_trades_flow import VIOLATION_TAG, push, sync

pytestmark = pytest.mark.usefixtures("clean_users")

# Сделки «прошлого дня». Граница дня в тестах отодвинута на шесть часов
# вперёд (`move_day_boundary_away`), поэтому текущий торговый день начался
# примерно восемнадцать часов назад: всё, что старше, заведомо в прошлом дне,
# а 26 и 25 часов назад — заведомо в одном и том же прошлом дне.
YESTERDAY_TAGGED = 26 * 60
YESTERDAY_AFTER = 25 * 60
YESTERDAY_BEFORE = 30 * 60


def retro_rows(feed: dict) -> list[dict]:
    return [item for item in feed["items"] if item["code"] == "retro_tag"]


async def user_context():
    """Пользователь, его настройки и сегодняшний торговый день."""
    async with db.session_factory()() as s:
        user = await identity_repo.user_by_email(s, EMAIL)
        assert user is not None
        prefs = await auth.prefs_of(s, user.id)
    now = dt.datetime.now(dt.UTC)
    return user.id, prefs, trading_day(now, prefs.timezone, prefs.day_cutoff)


# --- ветка «нарушено»: после тега трейдер продолжил торговать ---


async def test_late_tag_writes_the_incident_into_that_past_day(
    app_client: httpx.AsyncClient,
) -> None:
    """Проверка из плана: инцидент записан в тот день, сегодня не тронуто.

    Ретропроверка нашла сделку, открытую после размеченной, — значит окно того
    дня соблюдено не было (ТЗ 4.4). Инцидент ложится в **тот** день, а не
    в сегодняшний: история должна объяснять, когда трейдер нарушил, а не
    когда сервис об этом узнал.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=YESTERDAY_TAGGED)
    await push(app_client, symbol="ETHUSDT", minutes_ago=YESTERDAY_AFTER)
    await sync(app_client)
    _user_id, prefs, day = await user_context()
    yesterday = day - dt.timedelta(days=1)

    await mark_tag_as_violation(app_client)

    rows = retro_rows(await incidents(app_client, period="all"))
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["day"] == yesterday.isoformat()
    assert row["outcome"] == "breached"
    assert row["title"] == "Тег поставлен после конца дня"
    assert "SR-1" in row["detail"]
    assert "ретропроверка" in row["detail"]
    assert "нарушение записано в" in row["detail"]
    assert "стрик пересчитан" in row["detail"]
    # Блокировки нет — ни в инциденте, ни рядом с ним.
    assert row["lock"] is None
    assert row["details"]["not_locked"] == "day_window_closed"
    assert prefs.timezone  # настройки прочитаны, время в строке местное


async def test_late_tag_does_not_touch_today(app_client: httpx.AsyncClient) -> None:
    """Сегодняшний день поздний тег не трогает вообще (ТЗ 4.4).

    Это половина шага, и именно её проще всего сломать: соблазн «ну хоть
    предупредить сегодня» приводит к блокировке за то, что случилось вчера.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=YESTERDAY_TAGGED)
    await push(app_client, symbol="ETHUSDT", minutes_ago=YESTERDAY_AFTER)
    await sync(app_client)
    await mark_tag_as_violation(app_client)

    body = await today(app_client)
    assert body["lock"] is None
    assert body["state"] != "locked"
    # Ни ретро-строки, ни любой другой: сегодня ничего не произошло.
    assert [i for i in body["incidents"] if i["code"] == "retro_tag"] == []


# --- ветка «соблюдено»: после тега трейдер остановился сам ---


async def test_late_tag_without_trades_after_is_kept(
    app_client: httpx.AsyncClient,
) -> None:
    """Сделок после размеченной не было → «соблюдено» (ТЗ 4.4).

    И сделка, открытая ДО размеченной, исход не меняет: окно ретропроверки —
    остаток дня после закрытия, а не весь день. Иначе поздний тег наказывал
    бы за торговлю, которая была раньше повода.
    """
    await setup(app_client)
    await push(app_client, symbol="SOLUSDT", minutes_ago=YESTERDAY_BEFORE)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=YESTERDAY_TAGGED)
    await sync(app_client)

    await mark_tag_as_violation(app_client)

    rows = retro_rows(await incidents(app_client, period="all"))
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["outcome"] == "kept"
    assert "сделок после" in row["detail"]
    assert "не было" in row["detail"]
    assert "блокировка не включалась" in row["detail"]
    assert row["details"]["trades_after"] == []


async def test_kept_does_not_mean_the_day_was_counted(
    app_client: httpx.AsyncClient,
) -> None:
    """«Соблюдено» — про окно, а не про стрик. День всё равно не зачтён.

    Читается как противоречие, поэтому зафиксировано тестом. ТЗ 4.4 говорит
    «стрик того дня сгорает» про нарушенную ветку, но день с нарушением не
    зачитывается по ТЗ 7.1 в любом случае, и тег — это нарушение. Исход
    инцидента отвечает на другой вопрос: торговал ли трейдер после той сделки.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=YESTERDAY_TAGGED)
    await sync(app_client)
    await mark_tag_as_violation(app_client)

    _user_id, _prefs, day = await user_context()
    yesterday = (day - dt.timedelta(days=1)).isoformat()

    rows = retro_rows(await incidents(app_client, period="all"))
    assert rows[0]["outcome"] == "kept"

    res = await app_client.get("/api/v1/streak")
    assert res.status_code == 200, res.text
    marks = {d["day"]: d for d in res.json()["days"]}
    assert marks[yesterday]["counted"] is False


# --- повторный проход и живой тег ---


async def test_retro_check_does_not_repeat(app_client: httpx.AsyncClient) -> None:
    """Повторная сверка не задваивает ретро-инцидент.

    Тот же ключ повтора, что у живого тега, но свой повод (`retro:<сделка>`):
    одно и то же событие приходит и из потока, и из сверки.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=YESTERDAY_TAGGED)
    await push(app_client, symbol="ETHUSDT", minutes_ago=YESTERDAY_AFTER)
    await sync(app_client)
    await mark_tag_as_violation(app_client)

    await sync(app_client)
    await today(app_client)
    await sync(app_client)

    assert len(retro_rows(await incidents(app_client, period="all"))) == 1


async def test_live_tag_is_not_rechecked_after_the_day_closes(
    app_client: httpx.AsyncClient,
) -> None:
    """Тег, разобранный живым, ретропроверка второй раз не разбирает.

    Сверка после переподключения приносит вчерашний день целиком, и без этой
    защиты каждая такая сверка дописывала бы вторую строку к событию, которое
    уже записано вместе со своей блокировкой. В ленте это выглядело бы как
    два нарушения там, где было одно.

    Проверяется прямым вызовом с временем «после конца дня»: дождаться
    границы дня в тесте нельзя.
    """
    from eds.app import system_rules

    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=0)
    await sync(app_client)
    await mark_tag_as_violation(app_client)
    # Живой тег: блокировка есть.
    assert (await today(app_client))["lock"] is not None

    user_id, prefs, day = await user_context()
    later = day_ends_at(day, prefs.timezone, prefs.day_cutoff) + dt.timedelta(hours=1)
    async with db.session_factory()() as s:
        result = await system_rules.sr1_violations(
            s, user_id, prefs, day, now=later, today=day + dt.timedelta(days=1)
        )
        await s.commit()

    assert result.locks == []
    assert result.retro == []
    assert retro_rows(await incidents(app_client, period="all")) == []


# --- пересчёт серии ---


async def test_streak_recount_rewrites_the_series_after_the_changed_day(
    app_client: httpx.AsyncClient,
) -> None:
    """Серия пересчитывается от изменённого дня, а не поправляется с конца.

    Проверка на чистых правилах, потому что именно здесь живёт риск №1
    (Архитектура ч.2 §4.4): десять зачтённых дней, у пятого появилось
    нарушение — текущая серия обязана стать числом дней ПОСЛЕ него, а не
    уменьшиться на единицу.
    """
    base = dt.date(2026, 9, 1)
    marks = [DayMark(base + dt.timedelta(days=i), counted=True, reason="ok") for i in range(10)]
    assert streak_rules.current_streak(marks) == 10

    marks[4] = DayMark(marks[4].day, counted=False, reason="violation")
    assert streak_rules.current_streak(marks) == 5
    # Лучшая серия остаётся историей: она не уменьшается от того, что
    # текущая оборвалась.
    assert streak_rules.best_streak(marks) == 5


async def test_recount_window_starts_at_the_changed_day(
    app_client: httpx.AsyncClient,
) -> None:
    """Окно пересчёта начинается с изменённого дня и не включает сегодня.

    Обычное окно пересчёта — шестьдесят дней назад от сегодня. Поздний тег
    может прийти на день старше, и тогда пересчёт по умолчанию прошёл бы
    мимо него; сегодняшний день, наоборот, в серии не участвует.
    """
    await setup(app_client)
    await push(app_client, tags=[VIOLATION_TAG], minutes_ago=YESTERDAY_TAGGED)
    await sync(app_client)
    user_id, prefs, day = await user_context()
    far = day - dt.timedelta(days=200)

    async with db.session_factory()() as s:
        before, after = await retro.recount(s, user_id, prefs, far, today=day)
        await s.commit()
    assert isinstance(before, int) and isinstance(after, int)

    # Тот же путь пересчёта, что у ретро, но окном по умолчанию: отметки
    # вчерашнего дня обязаны совпасть — иначе «было → стало» в уведомлении
    # означало бы не то, что показывает полоска.
    async with db.session_factory()() as s:
        state = await app_streaks.refresh(s, user_id, prefs, today=day)
        await s.commit()
    assert state.current == after


# --- текст уведомления ---


def test_retro_notification_text_matches_the_prototype() -> None:
    """Дефолт события `retro_violation` — из прототипа NotifyTexts.dc.html.

    Текст собирает сервер: та же строка уйдёт в Telegram на шаге 13, и
    собранная во второй раз на клиенте она разъедётся.
    """
    text = incidents_service.retro_violation_text(dt.date(2026, 9, 8), 14, 1)
    assert text == "Тег зафиксировал нарушение за 8 сентября. Стрик пересчитан: 14 → 1."
