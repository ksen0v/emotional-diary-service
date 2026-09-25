"""Жизненный цикл инцидента и блокировки.

Разделение обязанностей из Архитектуры ч.1 §2: движок правил отвечает только
на вопрос «выполнились ли условия», а что с этим делать — решает этот модуль.
Поэтому новое действие правила добавляется здесь и не трогает движок.

Правила, от которых нельзя отступать:

- **Блокировка не переходит на следующий торговый день** (ТЗ 6.6). `window_until`
  стоит на границе дня и перебивает таймер.
- **Снятие — конъюнкция включённых условий.** Не включено ни одного — снять
  вручную нельзя, блокировка кончится сама на границе дня. Это осмысленный
  вариант «сегодня я больше не торгую», и запрещать его не надо.
- **Compliance считается по времени открытия сделки.** Сделка, открытая за
  минуту до блокировки и закрытая внутри неё, нарушением не является.
"""

import datetime as dt
import logging
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts import events as ev
from eds.contracts.rules import Firing, OpenTrade
from eds.modules.incidents import repo
from eds.modules.incidents.models import (
    ACTIVE,
    BREACHED,
    CODE_LOCK_BREACHED,
    CODE_NO_ADMISSION,
    CODE_RETRO_TAG,
    CODE_RULE_FIRED,
    CODE_TITLE,
    CODE_VIOLATION,
    EXPIRED,
    KEPT,
    LIFTED,
    OPEN,
    IncidentRow,
    LockRow,
)
from eds.platform import bus
from eds.platform.errors import UNPROCESSABLE, AppError, not_found

log = logging.getLogger("eds.incidents")

ANSWER_MIN = 10

# Вопросы разбора блокировки — из прототипа Locked.dc.html дословно.
REVIEW_QUESTIONS: tuple[dict[str, str], ...] = (
    {"id": "q1", "text": "Что произошло?", "hint": "Коротко, своими словами"},
    {
        "id": "q2",
        "text": "Какое правило нарушено?",
        "hint": "Своё правило, а не правило сервиса",
    },
    {"id": "q3", "text": "Что делаешь дальше?", "hint": "Одно конкретное действие"},
)


# --- срабатывание ---


async def open_from_firing(
    s: AsyncSession,
    user_id: uuid.UUID,
    firing: Firing,
    *,
    window_until: dt.datetime,
    shadow: bool,
    now: dt.datetime,
    code: str = CODE_RULE_FIRED,
    extra: dict[str, Any] | None = None,
) -> tuple[IncidentRow | None, LockRow | None]:
    """Записать инцидент и, если правило того требует, включить блокировку.

    `code` отличает срабатывание правила трейдера от системного триггера:
    у SR-1 это `violation`. Механика при этом одна и та же — у системного
    триггера другое условие, а не другой жизненный цикл, и дублировать ради
    этого весь путь значило бы завести второе место, где чинить блокировки.

    Блокировка не включается в трёх случаях, и каждый из них записан
    в инциденте, а не подразумевается:

    - действие «блокировка» у правила выключено — правило только уведомляет;
    - включён режим наблюдения — инцидент пишется, блокировка не применяется
      (Архитектура ч.2 §5.10);
    - блокировка уже идёт — второй экран блокировки показать некуда, и в базе
      стоит ограничение «одна активная на пользователя». Инцидент при этом
      записывается и разделяет исход с идущей блокировкой.
    """
    lock_wanted = bool((firing.actions.get("lock") or {}).get("enabled"))
    running = await repo.active_lock(s, user_id)

    details: dict[str, Any] = {
        "trade_id": str(firing.trade_id) if firing.trade_id else None,
        "trigger_ref": firing.trigger_ref,
        "rule_name": firing.rule_name,
        "rule_text": firing.rule_text,
        "rule_version": firing.rule_version,
        "snapshot": firing.snapshot,
        "had_lock": False,
        "alert": bool(firing.actions.get("alert")),
        **(extra or {}),
    }
    if lock_wanted and shadow:
        details["not_locked"] = "shadow_mode"
    elif lock_wanted and running is not None:
        details["not_locked"] = "lock_active"
        details["covered_by_lock"] = str(running.id)
    elif not lock_wanted:
        details["not_locked"] = "no_lock_action"

    incident = await repo.insert_incident(
        s,
        user_id,
        day=firing.day,
        code=code,
        rule_id=firing.rule_id,
        details=details,
        shadow=shadow,
    )
    if incident is None:
        # Такой инцидент уже записан: то же правило, тот же день, та же сделка.
        return None, None

    await bus.publish(
        s,
        ev.INCIDENTS_OPENED,
        {
            "user_id": str(user_id),
            "incident_id": str(incident.id),
            "day": firing.day.isoformat(),
            "rule_id": str(firing.rule_id),
            "rule_name": firing.rule_name,
            "shadow": shadow,
        },
        dedup_key=f"incident:{incident.id}",
    )

    if firing.actions.get("alert"):
        # Telegram — шаг 13. До него алерт идёт в лог: это честнее, чем
        # очередь, из которой никто не читает.
        log.info(
            "алерт (шаг 13 отправит в Telegram): %s — %s",
            firing.rule_name,
            firing.rule_text,
        )

    if not lock_wanted or shadow or running is not None:
        # Окна соблюдения нет, поэтому инцидент закрывается сразу: висеть
        # открытым он не должен, иначе «открытых инцидентов» накопится список,
        # который ничего не значит.
        if running is None:
            await _close_incident(s, incident, KEPT, now)
        return incident, None

    lock = await _start_lock(
        s, user_id, incident, firing, window_until=window_until, now=now
    )
    return incident, lock


async def _start_lock(
    s: AsyncSession,
    user_id: uuid.UUID,
    incident: IncidentRow,
    firing: Firing,
    *,
    window_until: dt.datetime,
    now: dt.datetime,
) -> LockRow:
    minutes = (firing.actions.get("lock") or {}).get("minutes")
    requires = {key: bool(firing.unlock.get(key)) for key in ("timer", "review", "buddy")}

    timer_until: dt.datetime | None = None
    if requires["timer"] and minutes is not None:
        # Граница дня перебивает таймер: блокировка на 60 минут за двадцать
        # минут до конца дня кончится вместе с днём, а не завтра утром.
        timer_until = min(now + dt.timedelta(minutes=int(minutes)), window_until)

    lock = await repo.insert_lock(
        s,
        LockRow(
            id=uuid.uuid4(),
            user_id=user_id,
            incident_id=incident.id,
            day=firing.day,
            rule_id=firing.rule_id,
            rule_name=firing.rule_name,
            rule_text=firing.rule_text,
            rule_version=firing.rule_version,
            started_at=now,
            timer_until=timer_until,
            window_until=window_until,
            requires=requires,
            state=ACTIVE,
            lifted_at=None,
            lift_reason=None,
        ),
    )
    incident.details = {**incident.details, "had_lock": True, "lock_id": str(lock.id)}
    await s.flush()

    await bus.publish(
        s,
        ev.INCIDENTS_LOCK_STARTED,
        {
            "user_id": str(user_id),
            "lock_id": str(lock.id),
            "incident_id": str(incident.id),
            "day": firing.day.isoformat(),
            "rule_name": firing.rule_name,
            "started_at": now.isoformat(),
            "timer_until": timer_until.isoformat() if timer_until else None,
            "window_until": window_until.isoformat(),
            "requires": requires,
        },
        dedup_key=f"lock-started:{lock.id}",
    )
    return lock


async def open_fact(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    code: str,
    day: dt.date,
    rule_id: uuid.UUID | None,
    rule_name: str,
    rule_text: str,
    details: dict[str, Any],
    shadow: bool,
    now: dt.datetime,
    alert: bool = True,
    outcome: str = BREACHED,
) -> IncidentRow | None:
    """Инцидент-факт: событие уже случилось, соблюдать нечего.

    Так записываются SR-2 и SR-3. Сделка во время блокировки и торговля без
    допуска — это не окно, которое можно выдержать, а уже состоявшееся
    нарушение, поэтому исход известен в момент записи и инцидент закрывается
    сразу. Держать его открытым значило бы обещать, что он ещё может
    кончиться иначе.

    Режим наблюдения на запись не влияет: он выключает применение блокировок
    и отправку уведомлений, а не фиксацию факта (Архитектура ч.2 §5.10).
    Стрик в режиме наблюдения тоже считается по-настоящему.

    Исход по умолчанию «нарушено», потому что так записываются SR-2 и SR-3:
    у них сам факт и есть нарушение. У ретропроверки позднего тега (SR-1,
    ТЗ 4.4) поводом записи является тег, а исход выходит любым из двух —
    «соблюдено», если после той сделки трейдер не торговал, — поэтому исход
    стал параметром. Открытым инцидент-факт не бывает ни в одном из случаев:
    окна, которое можно выдержать, у него нет.

    None — такой инцидент уже записан. Ключ повтора стоит в базе, поэтому
    повторный проход сверки ничего не задвоит.
    """
    incident = await repo.insert_incident(
        s,
        user_id,
        day=day,
        code=code,
        rule_id=rule_id,
        details={**details, "rule_name": rule_name, "rule_text": rule_text},
        shadow=shadow,
    )
    if incident is None:
        return None

    await _close_incident(s, incident, outcome, now)
    await bus.publish(
        s,
        ev.INCIDENTS_OPENED,
        {
            "user_id": str(user_id),
            "incident_id": str(incident.id),
            "day": day.isoformat(),
            "code": code,
            "rule_id": str(rule_id) if rule_id else None,
            "rule_name": rule_name,
            "shadow": shadow,
        },
        dedup_key=f"incident:{incident.id}",
    )
    if alert:
        # Telegram и сигнал доверенному лицу — шаг 13. До него алерт идёт
        # в лог: это честнее, чем очередь, из которой никто не читает.
        log.info(
            "алерт (шаг 13 отправит в Telegram): %s — %s",
            CODE_TITLE.get(code, code),
            rule_text,
        )
    return incident


# --- compliance и снятие ---


async def record_breach(
    s: AsyncSession,
    user_id: uuid.UUID,
    lock: LockRow,
    trades: list[OpenTrade],
    now: dt.datetime,
) -> bool:
    """Сделка открыта во время блокировки → инцидент нарушен.

    Сама блокировка при этом не снимается: экран остаётся, а сверху появляется
    красная полоса (Дизайн Э-13). Снимать блокировку в наказание было бы
    подарком тому, кто её нарушил.
    """
    if not trades:
        return False
    incident = await repo.by_id(s, user_id, lock.incident_id)
    if incident is None or incident.outcome != "open":
        return False

    incident.details = {
        **incident.details,
        "breach_trades": [
            {
                "trade_id": str(t.trade_id),
                "symbol": t.symbol,
                "open_time": t.open_time.isoformat(),
            }
            for t in trades
        ],
    }
    await _close_incident(s, incident, BREACHED, now)

    first = trades[0]
    await bus.publish(
        s,
        ev.INCIDENTS_LOCK_BREACHED,
        {
            "user_id": str(user_id),
            "lock_id": str(lock.id),
            "incident_id": str(incident.id),
            "day": lock.day.isoformat(),
            "trade_id": str(first.trade_id),
            "symbol": first.symbol,
            "open_time": first.open_time.isoformat(),
        },
        dedup_key=f"lock-breached:{lock.id}",
    )
    log.info(
        "нарушение блокировки %s: сделка %s открыта в %s",
        lock.id,
        first.symbol,
        first.open_time,
    )
    return True


def satisfied_of(
    lock: LockRow, review_filled: bool, now: dt.datetime
) -> dict[str, bool | None]:
    """Что из условий снятия выполнено.

    Три состояния, а не два: None — условие выключено, False — включено и не
    выполнено. Экран блокировки показывает чек-лист, и «выключено» и «не
    выполнено» там выглядят по-разному (Архитектура ч.2 §3.5).
    """
    requires = lock.requires or {}
    return {
        "timer": (
            None
            if not requires.get("timer")
            else bool(lock.timer_until is not None and now >= lock.timer_until)
        ),
        "review": None if not requires.get("review") else review_filled,
        # Подтверждение доверенного лица включить нельзя до шага 13: без
        # подтверждённого контакта такое правило не сохраняется (ТЗ 6.8).
        "buddy": None if not requires.get("buddy") else False,
    }


def can_lift(satisfied: dict[str, bool | None]) -> bool:
    values = [v for v in satisfied.values() if v is not None]
    # Ни одного включённого условия — снять вручную нельзя (ТЗ 6.6).
    return bool(values) and all(values)


async def settle(
    s: AsyncSession, user_id: uuid.UUID, lock: LockRow, now: dt.datetime
) -> LockRow:
    """Довести блокировку до конца, если пора: снять или закрыть границей дня."""
    if lock.state != ACTIVE:
        return lock

    review = await repo.review_of(s, lock.id)
    satisfied = satisfied_of(lock, review is not None, now)

    if now >= lock.window_until:
        return await _finish(s, user_id, lock, EXPIRED, "day_boundary", now)
    if can_lift(satisfied):
        return await _finish(s, user_id, lock, LIFTED, "conditions_met", now)
    return lock


async def _finish(
    s: AsyncSession,
    user_id: uuid.UUID,
    lock: LockRow,
    state: str,
    reason: str,
    now: dt.datetime,
) -> LockRow:
    incident = await repo.by_id(s, user_id, lock.incident_id)
    breached = incident is not None and incident.outcome == BREACHED

    lock.state = BREACHED if breached else state
    lock.lifted_at = now
    lock.lift_reason = "breached" if breached else reason
    await s.flush()

    if incident is not None and incident.outcome == "open":
        await _close_incident(s, incident, KEPT, now)
    await _close_covered(s, user_id, lock, incident, now)

    await bus.publish(
        s,
        ev.INCIDENTS_LOCK_LIFTED,
        {
            "user_id": str(user_id),
            "lock_id": str(lock.id),
            "day": lock.day.isoformat(),
            "state": lock.state,
            "reason": lock.lift_reason,
            "lifted_at": now.isoformat(),
        },
        dedup_key=f"lock-lifted:{lock.id}",
    )
    return lock


async def _close_covered(
    s: AsyncSession,
    user_id: uuid.UUID,
    lock: LockRow,
    incident: IncidentRow | None,
    now: dt.datetime,
) -> None:
    """Инциденты, которые ждали чужую блокировку, получают её исход.

    Правило сработало, пока шла другая блокировка: своей у него нет, но окно
    соблюдения оно делило с идущей, поэтому и исход у них общий.
    """
    outcome = incident.outcome if incident is not None else KEPT
    for row in await repo.incidents_of_day(s, user_id, lock.day):
        if row.outcome != "open":
            continue
        if (row.details or {}).get("covered_by_lock") != str(lock.id):
            continue
        await _close_incident(s, row, outcome, now)


async def _close_incident(
    s: AsyncSession, incident: IncidentRow, outcome: str, now: dt.datetime
) -> None:
    incident.outcome = outcome
    incident.closed_at = now
    await s.flush()


async def submit_review(
    s: AsyncSession,
    user_id: uuid.UUID,
    lock_id: uuid.UUID,
    answers: dict[str, str],
    now: dt.datetime,
) -> tuple[LockRow, dict[str, bool | None]]:
    """Заполнить разбор блокировки.

    Разбор доступен во время таймера, а не после него: пусть трейдер пишет,
    пока горячо, — к концу таймера останется только нажать кнопку (Дизайн Э-13).
    """
    lock = await repo.lock_by_id(s, user_id, lock_id)
    if lock is None:
        raise not_found("Блокировка не найдена.")
    if lock.state != ACTIVE:
        raise AppError(
            "lock_not_active", "Эта блокировка уже закрыта.", 409, {"state": lock.state}
        )
    if await repo.review_of(s, lock.id) is not None:
        raise AppError("already_done", "Разбор этой блокировки уже заполнен.", 409)

    cleaned: dict[str, str] = {}
    for question in REVIEW_QUESTIONS:
        text = (answers.get(question["id"]) or "").strip()
        if len(text) < ANSWER_MIN:
            # Грубая проверка: от «ааааааа» она не спасает и не должна.
            # Её задача — минимальное трение, а не превращение разбора в тест.
            raise AppError(
                "validation_failed",
                f"«{question['text']}»: ответ короче {ANSWER_MIN} символов.",
                UNPROCESSABLE,
                {"question": question["id"]},
            )
        cleaned[question["id"]] = text

    await repo.save_review(
        s, lock.id, q1=cleaned["q1"], q2=cleaned["q2"], q3=cleaned["q3"]
    )
    lock = await settle(s, user_id, lock, now)
    return lock, satisfied_of(lock, True, now)


# --- чтение ---


def unlock_words(requires: dict[str, Any] | None) -> str:
    """Условия снятия словами: «таймер и разбор».

    Собирает сервер, а не фронт: та же строка стоит под таймером и в подвале
    экрана, и собранная в двух местах она однажды разъедётся (ч.2 §1.3).
    """
    names = {"timer": "таймер", "review": "разбор", "buddy": "подтверждение друга"}
    parts = [
        names[key] for key in ("timer", "review", "buddy") if (requires or {}).get(key)
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " и " + parts[-1]


def lock_out(
    lock: LockRow | None,
    review_filled: bool,
    now: dt.datetime,
    breach: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Блокировка для экрана (Архитектура ч.2 §3.5 и §3.7)."""
    if lock is None:
        return None
    satisfied = satisfied_of(lock, review_filled, now)
    words = unlock_words(lock.requires)
    return {
        "id": str(lock.id),
        "incident_id": str(lock.incident_id),
        "rule_name": lock.rule_name,
        "rule_text": lock.rule_text,
        "started_at": lock.started_at,
        "timer_until": lock.timer_until,
        "window_until": lock.window_until,
        "server_time": now,
        "requires": lock.requires,
        "satisfied": satisfied,
        "can_lift": can_lift(satisfied),
        "state": lock.state,
        "review_questions": [dict(q) for q in REVIEW_QUESTIONS],
        "review_filled": review_filled,
        # Две готовые строки для экрана: под таймером и в подвале.
        "unlock_short": f"снятие: {words}" if words else "снимется на границе дня",
        "unlock_text": (
            f"Условия снятия: {words}"
            if words
            else "Условий снятия нет — блокировка кончится на границе дня"
        ),
        # Доверенное лицо — шаг 13. null, а не пустой объект: пустой объект
        # читался бы как «контакт есть, просто без имени».
        "buddy": None,
        "breach": breach,
    }


def breach_of(incident: IncidentRow | None) -> dict[str, Any] | None:
    """Данные красной полосы: что именно нарушило блокировку."""
    if incident is None or incident.outcome != BREACHED:
        return None
    trades = (incident.details or {}).get("breach_trades") or []
    if not trades:
        return None
    return {"trades": trades, "first": trades[0], "streak_text": None}


def streak_burned_text(current: int) -> str:
    """Вторая фраза красной полосы: что стало со стриком (SR-2, ТЗ 6.5).

    В прототипе она звучит как «Стрик 14 дней сгорел», и это почти правда —
    но не вся: отметка дню ставится, когда день закончится, поэтому число
    в шапке до границы дня не изменится. Умолчать об этом нельзя: трейдер
    увидит прежние 14 и решит, что обошлось, а на следующий день получит
    единицу без объяснения. Поэтому фраза говорит и про сгоревший стрик,
    и про то, когда это станет видно.

    Третьей фразы прототипа — «Максиму отправлен сигнал» — здесь нет:
    доверенное лицо появится на шаге 13, и до тех пор про отправленный
    сигнал сервис врать не может.
    """
    if current <= 0:
        return "Этот день в стрик не зачтётся."
    days = _plural(current, "день", "дня", "дней")
    return (
        f"Стрик {current} {days} сгорел: этот день не зачтётся. "
        "Счётчик в шапке обновится на границе дня."
    )


def retro_violation_text(day: dt.date, before: int, after: int) -> str:
    """Уведомление «поздний тег закрыл прошлый день» (событие `retro_violation`).

    Дефолт взят из прототипа `NotifyTexts.dc.html` дословно:
    «Тег зафиксировал нарушение за {day}. Стрик пересчитан: {before} → {after}.»

    В ТЗ 4.4 пример этого уведомления длиннее — он называет и сделку, и день,
    и слово «день» после числа. Взят прототип: он показывает тот текст,
    который лежит в настройках как редактируемый дефолт, а пример в ТЗ
    объясняет смысл, а не задаёт строку. Расхождением это не считаю и
    в раздел 5 не пишу — но называю здесь, чтобы на шаге 13, когда дефолты
    переедут в `notify.templates`, выбор не пришлось делать заново.

    Редактора текстов тут нет: он появится на шаге 13 вместе с Telegram,
    и до тех пор строка уходит в журнал сервиса.
    """
    return (
        f"Тег зафиксировал нарушение за {date_text(day)}. "
        f"Стрик пересчитан: {before} → {after}."
    )


# Заголовок и подпись строки в ленте инцидентов собирает сервер. Причина та
# же, по которой сервер собирает `human_text` правила (ч.2 §1.3 и §3.6): эта
# же строка уйдёт в уведомление и в блок «Инциденты сегодня», и собранная
# в трёх местах она опишет одно событие тремя способами.

# Два падежа, потому что оба нужны: «18 сентября» в строке ленты и
# «За сентябрь» в сводке над ней. Выводить один из другого дешевле не выходит.
MONTHS_NOM = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

OUTCOME_TEXT = {OPEN: "идёт", KEPT: "соблюдено", BREACHED: "нарушено"}


def _local(moment: dt.datetime | None, tz: str) -> dt.datetime | None:
    if moment is None:
        return None
    return moment.astimezone(ZoneInfo(tz))


def _hhmm(moment: dt.datetime | None, tz: str) -> str:
    local = _local(moment, tz)
    return "" if local is None else f"{local:%H:%M}"


def _daypart(moment: dt.datetime) -> str:
    """«вечером», «утром» — как в прототипе Incidents.dc.html.

    Часть суток, а не время с точностью до минуты: в строке про поздний тег
    важно не «в 22:14», а то, что тег пришёл, когда день уже кончился.
    Точное время стоит слева, в колонке строки, и повторять его в подписи
    значило бы занять место, ничего не сказав.
    """
    hour = moment.hour
    if 5 <= hour < 12:
        return "утром"
    if 12 <= hour < 17:
        return "днём"
    if 17 <= hour < 23:
        return "вечером"
    return "ночью"


def _ordinal(day: dt.date) -> str:
    """«9-го» — день без месяца.

    Месяц не называется сознательно: строка уже сказала, какого числа была
    сделка, и второй месяц подряд читался бы как другой месяц.
    """
    return f"{day.day}-го"


def date_text(day: dt.date) -> str:
    """«18 сентября» — как в прототипе. Год не показываем: лента идёт за месяц."""
    return f"{day.day} {MONTHS[day.month - 1]}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    rest = abs(n) % 100
    if 10 < rest < 20:
        return many
    last = rest % 10
    if last == 1:
        return one
    if 1 < last < 5:
        return few
    return many


def _lock_span(lock: LockRow | None) -> str:
    """Была ли блокировка и насколько."""
    if lock is None:
        return ""
    if lock.timer_until is None:
        return "блокировка до конца торгового дня"
    minutes = max(1, round((lock.timer_until - lock.started_at).total_seconds() / 60))
    return f"блокировка {minutes} мин"


def _lock_words(lock: LockRow | None) -> list[str]:
    """Чем обернулось срабатывание: блокировка и её условия снятия."""
    span = _lock_span(lock)
    if not span or lock is None:
        return []
    words = unlock_words(lock.requires)
    return [span] + ([f"снятие: {words}"] if words else ["снимется на границе дня"])


def _outcome_words(row: IncidentRow, lock: LockRow | None, tz: str) -> list[str]:
    """Чем всё кончилось. Пустая строка лучше выдуманной: инцидент, который
    ещё идёт, так и написан — «идёт», а не «соблюдено заранее»."""
    if row.code == CODE_RETRO_TAG:
        # Чем кончилось, у позднего тега сказано в самой строке: исход тут
        # не «чем кончилась блокировка», а «что нашла ретропроверка», и
        # второй раз тем же словом он бы только запутал.
        return []

    details = row.details or {}
    breach = (details.get("breach_trades") or [None])[0]
    if row.outcome == BREACHED and breach:
        at = dt.datetime.fromisoformat(breach["open_time"])
        return [f"нарушена сделкой {_hhmm(at, tz)}"]
    if row.outcome == KEPT and lock is not None and lock.lifted_at is not None:
        when = _hhmm(lock.lifted_at, tz)
        if lock.lift_reason == "day_boundary":
            return ["кончилась на границе дня, новых сделок в окне не было"]
        return [f"снята в {when}, новых сделок в окне не было"]
    if row.outcome == KEPT and lock is None:
        reason = details.get("not_locked")
        if reason == "shadow_mode":
            return ["режим наблюдения: блокировка не применялась"]
        if reason == "no_lock_action":
            return ["без блокировки: у правила только алерт"]
    if details.get("covered_by_lock"):
        return ["во время этого срабатывания уже шла другая блокировка"]
    return []


def _at(details: dict[str, Any], tz: str) -> str:
    """«в 14:31» — время сделки в таймзоне трейдера.

    Именно через таймзону, а не срезом ISO-строки: в базе время в UTC, и срез
    дал бы в одной строке московское время рядом с лондонским.
    """
    raw = details.get("trade_open_time")
    if not isinstance(raw, str):
        return ""
    return f" в {_hhmm(dt.datetime.fromisoformat(raw), tz)}"


def _retro_words(row: IncidentRow, tz: str) -> list[str]:
    """Строка позднего тега — обе ветки из прототипа Incidents.dc.html.

    В прототипе их две, и они не симметричны, потому что говорят разное:

        тег на сделке 12:55 появился вечером, окно дня закрыто ·
        ретропроверка: сделок после 12:55 не было · блокировка не включалась

        тег на сделке 8 сентября 15:12 появился утром 9-го ·
        ретропроверка: после 15:12 были сделки → нарушение записано
        в 8 сентября, стрик пересчитан

    Разбор различий, потому что каждое здесь воспроизведено намеренно:

    - **Дата у сделки появляется только тогда, когда тег пришёл в другой
      день.** Тег того же вечера относится к сделке, которая и так видна
      в дне строки; тег следующего утра — нет, и без даты строка соврала бы
      о том, за какой день записано нарушение.
    - **«Окно дня закрыто» стоит там, где это не очевидно** — при теге в тот
      же день. Когда тег пришёл на следующее утро, закрытость окна не
      требует объяснений, и фраза стала бы шумом.
    - **Хвост зависит от исхода.** «Блокировка не включалась» — это ответ на
      вопрос «а меня накажут сейчас?», и он нужен там, где нарушения не
      нашлось. Там, где нашлось, вместо него стоит то, что произошло взамен:
      нарушение записано в тот день и стрик пересчитан.

    Отступление от прототипа, названное вслух: время сделки в первой части —
    время **открытия** (так сделка подписана в ленте сделок и в живой строке
    SR-1), а в ретропроверке — время **закрытия**, потому что именно от него
    считается окно (ТЗ 4.4). У скальперской сделки это одна и та же минута,
    и строка совпадает с прототипом; когда они разойдутся, в строке будет
    два времени — это честнее, чем одно на два разных смысла.
    """
    details = row.details or {}

    opened = _local(_iso(details.get("trade_open_time")), tz)
    closed = _local(_iso(details.get("trade_close_time")), tz)
    tagged = _local(_iso(details.get("tagged_at")), tz)
    # Время закрытия — граница окна. Нет его (сделка пришла без close_time) —
    # окном становится открытие: раньше него сделок «после» всё равно нет.
    edge = closed or opened

    mark = f"{opened:%H:%M}" if opened else ""
    same_day = bool(opened and tagged and opened.date() == tagged.date())
    if opened and not same_day:
        mark = f"{date_text(opened.date())} {opened:%H:%M}"

    when = ""
    if tagged:
        when = _daypart(tagged)
        if not same_day:
            when = f"{when} {_ordinal(tagged.date())}"

    head = f"тег на сделке {mark}".rstrip()
    if when:
        head = f"{head} появился {when}"
    if same_day:
        head = f"{head}, окно дня закрыто"

    after = f"{edge:%H:%M}" if edge else "закрытия сделки"
    if row.outcome == BREACHED:
        return [
            head,
            f"ретропроверка: после {after} были сделки → нарушение записано "
            f"в {date_text(row.day)}, стрик пересчитан",
        ]
    return [
        head,
        f"ретропроверка: сделок после {after} не было",
        "блокировка не включалась",
    ]


def _iso(raw: Any) -> dt.datetime | None:
    return dt.datetime.fromisoformat(raw) if isinstance(raw, str) else None


def _what_happened(row: IncidentRow, tz: str) -> list[str]:
    """Что именно случилось — своё для каждого кода (ТЗ 6.5)."""
    details = row.details or {}
    snapshot = details.get("snapshot") or {}

    if row.code == CODE_VIOLATION:
        symbol = details.get("symbol") or "сделка"
        return [f"{symbol} отмечена как нарушение{_at(details, tz)}"]

    if row.code == CODE_LOCK_BREACHED:
        symbol = details.get("symbol") or "сделка"
        rule = details.get("breached_rule_name")
        during = f", пока действовала блокировка «{rule}»" if rule else ""
        return [f"{symbol} открыта{_at(details, tz)}{during}", "стрик сброшен"]

    if row.code == CODE_RETRO_TAG:
        return _retro_words(row, tz)

    if row.code == CODE_NO_ADMISSION:
        count = int(snapshot.get("trades") or 0)
        score = snapshot.get("score")
        if score is None:
            how = "чек не пройден"
        else:
            how = f"балл допуска {score} из {snapshot.get('max_score', 25)}"
        return [
            f"{count} {_plural(count, 'сделка', 'сделки', 'сделок')} при том, что {how}",
            "стрик сброшен",
        ]

    return []


def _source_word(row: IncidentRow) -> str:
    code = (row.details or {}).get("system_code")
    if code:
        return code
    return "Пользовательское правило"


def incident_out(
    row: IncidentRow, lock: LockRow | None, tz: str = "UTC"
) -> dict[str, Any]:
    """Строка ленты инцидентов (Архитектура ч.2 §3.7 + прототип Incidents)."""
    details = row.details or {}
    title = CODE_TITLE.get(row.code) or details.get("rule_name") or "Срабатывание"

    happened = _what_happened(row, tz)
    outcome = _outcome_words(row, lock, tz)

    parts = [_source_word(row)] + happened + _lock_words(lock) + outcome
    if row.shadow:
        parts.append("режим наблюдения")

    # Короткая форма для блока «Инциденты сегодня»: в прототипе Main.dc.html
    # строка занимает одну строку рядом с заголовком, и условия снятия там
    # не помещаются — а главное, там на них не смотрят.
    short = [p for p in ([_lock_span(lock)] + outcome) if p] or happened

    return {
        "summary": ", ".join(short),
        "id": str(row.id),
        "day": row.day.isoformat(),
        "date_text": date_text(row.day),
        "time_text": _hhmm(row.opened_at, tz),
        "code": row.code,
        "title": title,
        "detail": " · ".join(p for p in parts if p),
        "outcome": row.outcome,
        "outcome_text": OUTCOME_TEXT.get(row.outcome, row.outcome),
        "shadow": row.shadow,
        "opened_at": row.opened_at,
        "closed_at": row.closed_at,
        "rule": {
            "id": str(row.rule_id) if row.rule_id else None,
            "name": details.get("rule_name"),
            "text_at_firing": details.get("rule_text"),
        },
        "lock": (
            None
            if lock is None
            else {
                "id": str(lock.id),
                "started_at": lock.started_at,
                "timer_until": lock.timer_until,
                "window_until": lock.window_until,
                "state": lock.state,
                "lifted_at": lock.lifted_at,
                "lift_reason": lock.lift_reason,
            }
        ),
        "details": details,
    }


async def day_feed(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date, tz: str = "UTC"
) -> list[dict[str, Any]]:
    """Инциденты одного дня — блок «Инциденты сегодня» на «Сегодня».

    Тот же сборщик строки, что и у ленты: блок на главном экране и раздел
    «Инциденты» обязаны описывать одно событие одинаково, иначе трейдер
    решит, что это два разных.
    """
    rows = await repo.incidents_of_day(s, user_id, day)
    if not rows:
        return []
    locks = await repo.locks_of(s, user_id, [row.id for row in rows])
    return [incident_out(row, locks.get(row.id), tz) for row in rows]
