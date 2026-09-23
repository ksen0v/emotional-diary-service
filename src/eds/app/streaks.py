"""Стрик: сбор фактов по дням и пересчёт серии.

Живёт в оркестрации, потому что зачёт дня зависит от четырёх модулей сразу:
сделки и нарушения из trades, допуск, сессия и разбор из daybook, запись
дневника оттуда же, а правило зачёта — в streaks. Модуль streaks при этом
не знает ни одной чужой таблицы: он получает факты и возвращает отметку.
"""

import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.streaks import DayFacts
from eds.modules.daybook import periods
from eds.modules.daybook import repo as daybook_repo
from eds.modules.streaks import repo as streaks_repo
from eds.modules.streaks import service as streaks
from eds.modules.streaks.models import StateRow
from eds.modules.trades import repo as trades_repo
from eds.platform import auth

# Окно ленивого пересчёта. Шире месяца, потому что полоска показывает 30 дней,
# и отметка за её край должна быть посчитана, а не пустовать.
REFRESH_DAYS = 60


async def refresh(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    *,
    today: dt.date,
    days: list[dt.date] | None = None,
) -> StateRow:
    """Пересчитать отметки и серию.

    Сегодняшний день не оценивается: он ещё идёт, и нарушение может случиться
    через минуту. Стрик всегда считается по завершённым дням.
    """
    window = days or [
        today - dt.timedelta(days=offset) for offset in range(1, REFRESH_DAYS + 1)
    ]
    window = sorted(day for day in window if day < today)
    if not window:
        return await streaks.recompute(s, user_id)

    facts = await facts_for(s, user_id, window)
    await streaks.apply_facts(s, user_id, facts)
    return await streaks.recompute(s, user_id)


async def facts_for(
    s: AsyncSession, user_id: uuid.UUID, days: list[dt.date]
) -> list[DayFacts]:
    since, until = days[0], days[-1]
    summaries = await trades_repo.day_summaries(s, user_id, since, until)
    day_rows = {
        row.day: row for row in await daybook_repo.days_in_range(s, user_id, since, until)
    }
    entries = {
        row.period_start: row
        for row in await daybook_repo.entries_in_range(
            s, user_id, periods.DAY, since, until
        )
    }
    tags = await daybook_repo.tags_of(s, [row.id for row in entries.values()])

    out: list[DayFacts] = []
    for day in days:
        summary = summaries.get(day)
        row = day_rows.get(day)
        entry = entries.get(day)
        out.append(
            DayFacts(
                day=day,
                trades=summary["trades"] if summary else 0,
                violations=summary["violations"] if summary else 0,
                # Нарушенные блокировки появятся на шаге 10. Ноль здесь честный:
                # блокировок нет, значит и нарушить их нельзя.
                lock_breaches=0,
                admission=row.admission if row else None,
                session_opened=bool(row and row.session_opened_at),
                review_done=bool(row and row.review_state == "done"),
                entry_filled=_filled(entry, tags.get(entry.id, []) if entry else []),
            )
        )
    return out


def _filled(entry, entry_tags: list[str]) -> bool:
    """Считается ли запись заполненной.

    Пустая строка в базе — это не запись. Достаточно чего-то одного: оценки,
    статуса, текста или тегов. «Короткая форма вне рынка» из ТЗ 7.1 — это
    ровно такой минимум, и требовать больше значило бы придумать своё условие.
    """
    if entry is None:
        return False
    return bool(entry.score or entry.status or (entry.body or "").strip() or entry_tags)


async def marks_in_range(
    s: AsyncSession, user_id: uuid.UUID, since: dt.date, until: dt.date
) -> dict:
    return await streaks_repo.marked_days(s, user_id, since, until)
