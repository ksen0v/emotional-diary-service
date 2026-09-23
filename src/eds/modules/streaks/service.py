"""Применение отметок и состояние серии."""

import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts import events as ev
from eds.contracts.streaks import (
    REASON_BREACH,
    REASON_FROZEN,
    REASON_VIOLATION,
    DayFacts,
    DayMark,
)
from eds.modules.streaks import repo, rules
from eds.modules.streaks.models import StateRow
from eds.platform import bus
from eds.platform.errors import UNPROCESSABLE, AppError

FREEZES_PER_MONTH = 2


async def apply_facts(
    s: AsyncSession, user_id: uuid.UUID, facts: list[DayFacts]
) -> int:
    """Пересчитать отметки за перечисленные дни. Возвращает число изменений.

    Заморозку не трогаем: её поставил трейдер руками, и она не должна
    исчезать от того, что в этот день что-то пересчиталось.

    Пишем только изменившееся: пересчёт идёт по шестидесяти дням при каждом
    открытии главной страницы, и шестьдесят UPDATE на каждое открытие — это
    нагрузка ни за что.
    """
    if not facts:
        return 0

    existing = await repo.marked_days(
        s, user_id, min(f.day for f in facts), max(f.day for f in facts)
    )
    changed = 0
    for item in facts:
        was = existing.get(item.day)
        if was is not None and was.reason == REASON_FROZEN:
            continue
        mark = rules.mark_of(item)
        if mark is None:
            if was is not None:
                await repo.delete_mark(s, user_id, item.day)
                changed += 1
            continue
        if was is not None and was.counted == mark.counted and was.reason == mark.reason:
            continue
        await repo.upsert_mark(s, user_id, mark)
        changed += 1

    if changed:
        await s.flush()
    return changed


async def recompute(
    s: AsyncSession, user_id: uuid.UUID, *, publish: bool = True
) -> StateRow:
    """Пересчитать текущую и лучшую серию по отметкам."""
    state = await repo.ensure_state(s, user_id)
    marks = await repo.marks_of(s, user_id)
    was_current, was_best = state.current, state.best

    state.current = rules.current_streak(marks)
    state.best = max(rules.best_streak(marks), state.best)
    state.last_day = marks[-1].day if marks else None
    state.updated_at = dt.datetime.now(dt.UTC)
    await s.flush()

    if publish and (state.current != was_current or state.best != was_best):
        await bus.publish(
            s,
            ev.STREAKS_CHANGED,
            {
                "user_id": str(user_id),
                "current": state.current,
                "previous": was_current,
                "best": state.best,
            },
            dedup_key=f"streak:{user_id}:{state.last_day}:{state.current}",
        )
    return state


def month_of(day: dt.date) -> dt.date:
    return day.replace(day=1)


def freezes_left(state: StateRow, today: dt.date) -> int:
    """Сколько заморозок осталось в этом месяце.

    Счётчик привязан к месяцу, а не к скользящему окну: «две в месяц» должно
    читаться по календарю, иначе трейдер не сможет посчитать их в уме.
    """
    if state.freezes_month != month_of(today):
        return FREEZES_PER_MONTH
    return max(0, FREEZES_PER_MONTH - state.freezes_used)


async def freeze(
    s: AsyncSession,
    user_id: uuid.UUID,
    day: dt.date,
    today: dt.date,
    *,
    has_violations: bool,
) -> StateRow:
    """Заморозить день: он не рвёт серию и не удлиняет её (ТЗ 7.2).

    Прошлое заморозить нельзя — иначе заморозка превращается в способ
    переписать историю задним числом, а история неизменяема (ТЗ 9.2).
    """
    if day < today:
        raise AppError(
            "day_in_past",
            "Заморозить прошедший день нельзя.",
            UNPROCESSABLE,
        )
    if has_violations:
        raise AppError(
            "day_has_violations",
            "В этот день уже есть нарушения — заморозка к нему не применяется.",
            UNPROCESSABLE,
        )

    state = await repo.ensure_state(s, user_id)
    # Повторная заморозка того же дня ничего не делает и ничего не стоит:
    # иначе второй клик по кнопке сжигал бы вторую заморозку месяца.
    already = await repo.marked_days(s, user_id, day, day)
    mark_now = already.get(day)
    if mark_now is not None and mark_now.reason == REASON_FROZEN:
        return state

    if freezes_left(state, today) <= 0:
        raise AppError(
            "no_freezes_left",
            f"Заморозки на этот месяц кончились: их {FREEZES_PER_MONTH}.",
            409,
        )

    if state.freezes_month != month_of(today):
        state.freezes_month = month_of(today)
        state.freezes_used = 0
    state.freezes_used += 1
    await repo.upsert_mark(
        s, user_id, DayMark(day=day, counted=False, reason=REASON_FROZEN)
    )
    await s.flush()
    return await recompute(s, user_id)


async def clean_month(
    s: AsyncSession, user_id: uuid.UUID, today: dt.date
) -> dict:
    """Дней без нарушений в этом месяце (ТЗ 7.2).

    Показывается рядом с серией нарочно: серия обрывается одним днём, а этот
    счётчик — нет, и после обрыва остаётся видно, что месяц не потерян.

    «Без нарушений» — это отсутствие нарушения и нарушенной блокировки,
    а не зачёт: день без разбора зачёт не получает, но нарушений в нём нет,
    и называть его нарушенным было бы неправдой.
    """
    start = month_of(today)
    marks = await repo.marked_days(s, user_id, start, today)
    dirty = sum(
        1
        for mark in marks.values()
        if mark.reason in (REASON_VIOLATION, REASON_BREACH)
    )
    elapsed = (today - start).days + 1
    return {"days": elapsed, "clean": elapsed - dirty, "month": start.isoformat()[:7]}


async def state_out(
    s: AsyncSession, user_id: uuid.UUID, today: dt.date, *, days: int = 30
) -> dict:
    state = await repo.ensure_state(s, user_id)
    since = today - dt.timedelta(days=days - 1)
    marks = await repo.marked_days(s, user_id, since, today)
    return {
        "current": state.current,
        "best": state.best,
        "last_day": state.last_day.isoformat() if state.last_day else None,
        "freezes": {
            "used": state.freezes_used if state.freezes_month == month_of(today) else 0,
            "left": freezes_left(state, today),
            "per_month": FREEZES_PER_MONTH,
            "month": month_of(today).isoformat()[:7],
        },
        # Полоска за 30 дней: причина берётся оттуда же, где считалась, —
        # расхождение между показанной и настоящей причиной невозможно.
        "days": [
            {
                "day": day.isoformat(),
                "counted": mark.counted,
                "reason": mark.reason,
                "text": rules.REASON_TEXT.get(mark.reason, mark.reason),
            }
            for day, mark in sorted(marks.items(), reverse=True)
        ],
        "month": await clean_month(s, user_id, today),
        "conditions": list(rules.CONDITIONS),
    }
