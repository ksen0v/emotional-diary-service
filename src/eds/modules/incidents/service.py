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

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts import events as ev
from eds.contracts.rules import Firing, OpenTrade
from eds.modules.incidents import repo
from eds.modules.incidents.models import (
    ACTIVE,
    BREACHED,
    CODE_RULE_FIRED,
    EXPIRED,
    KEPT,
    LIFTED,
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
) -> tuple[IncidentRow | None, LockRow | None]:
    """Записать инцидент и, если правило того требует, включить блокировку.

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
        code=CODE_RULE_FIRED,
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
    return {"trades": trades, "first": trades[0]}


def incident_out(row: IncidentRow, lock: LockRow | None) -> dict[str, Any]:
    details = row.details or {}
    return {
        "id": str(row.id),
        "day": row.day.isoformat(),
        "code": row.code,
        "outcome": row.outcome,
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
