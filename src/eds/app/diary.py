"""Дневник: записи трейдера рядом с объективными фактами периода.

Живёт в оркестрации, потому что соединяет три модуля: запись принадлежит
daybook, цифры — trades, допуск дня — снова daybook. Именно это соседство
делает дневник дневником трейдера, а не блокнотом: оценка «ровно» рядом
с «0 нарушений, +$140» читается иначе, чем сама по себе.
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import streaks as app_streaks
from eds.modules.daybook import periods
from eds.modules.daybook import repo as daybook_repo
from eds.modules.daybook import service as daybook
from eds.modules.streaks import rules as streak_rules
from eds.modules.trades import repo as trades_repo
from eds.modules.trades import service as trades_service
from eds.platform import auth
from eds.platform.errors import UNPROCESSABLE, AppError

# Ограничение диапазона: дневник смотрят месяцами, а не годами, и без границы
# один запрос мог бы собрать всю историю в одном ответе.
MAX_RANGE_DAYS = 400


async def list_entries(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    *,
    level: str,
    since: dt.date,
    until: dt.date,
    today: dt.date,
) -> dict:
    periods.check_level(level)
    if until < since:
        raise AppError("validation_failed", "Конец диапазона раньше начала.", 400)
    if (until - since).days > MAX_RANGE_DAYS:
        raise AppError(
            "range_too_wide",
            f"Диапазон больше {MAX_RANGE_DAYS} дней.",
            UNPROCESSABLE,
        )

    rows = await daybook_repo.entries_in_range(s, user_id, level, since, until)
    by_start = {row.period_start: row for row in rows}
    tags = await daybook_repo.tags_of(s, [row.id for row in rows])
    comments = await daybook_repo.comments_of(s, [row.id for row in rows])

    if level == periods.DAY:
        items = await _day_items(
            s, user_id, since, until, today, by_start, tags, comments
        )
    else:
        items = await _period_items(
            s, user_id, level, since, until, today, by_start, tags, comments
        )
    return {"level": level, "from": since.isoformat(), "to": until.isoformat(), "items": items}


async def _day_items(
    s: AsyncSession,
    user_id: uuid.UUID,
    since: dt.date,
    until: dt.date,
    today: dt.date,
    by_start: dict,
    tags: dict,
    comments: dict,
) -> list[dict]:
    """Клетки календаря: по дню на каждый день диапазона.

    Отдаём и пустые дни тоже: календарь без пустых клеток — не календарь,
    а список, и по нему не видно, что в среду торговли не было вообще.
    """
    summaries = await trades_repo.day_summaries(s, user_id, since, until)
    days = {row.day: row for row in await daybook_repo.days_in_range(s, user_id, since, until)}
    marks = await app_streaks.marks_in_range(s, user_id, since, until)

    items: list[dict] = []
    day = since
    while day <= until:
        if day > today:
            break
        summary = summaries.get(day)
        row = days.get(day)
        entry = by_start.get(day)
        items.append(
            {
                "level": periods.DAY,
                "period_start": day.isoformat(),
                "period_end": day.isoformat(),
                "entry": (
                    None
                    if entry is None
                    else daybook.entry_out(
                        entry, tags.get(entry.id, []), comments.get(entry.id, [])
                    )
                ),
                "facts": _day_facts(summary, row, marks.get(day)),
            }
        )
        day += dt.timedelta(days=1)
    items.reverse()
    return items


def _day_facts(summary: dict | None, row, mark=None) -> dict:
    """Факты одного дня — то, что стоит рядом с записью и в клетке календаря."""
    trades = summary["trades"] if summary else 0
    unmarked = summary["unmarked"] if summary else 0
    marked = trades - unmarked
    coverage = (
        (Decimal(marked) / Decimal(trades) * 100).quantize(Decimal("0.01"))
        if trades
        else Decimal("0.00")
    )
    return {
        "trades": trades,
        "violations": summary["violations"] if summary else 0,
        "unmarked": unmarked,
        "coverage_pct": str(coverage),
        "profit_usd": str(
            trades_service.quantize_money(summary["profit_usd"] if summary else Decimal("0"))
        ),
        "account_return_pct": str(
            trades_service.quantize_pct(
                summary["account_return_pct"] if summary else Decimal("0")
            )
        ),
        "emotion_cost_usd": str(
            trades_service.quantize_money(
                summary["emotion_cost_usd"] if summary else Decimal("0")
            )
        ),
        "admission": row.admission if row else None,
        "check_score": row.check_score if row else None,
        "review_state": row.review_state if row else "none",
        # Правила и блокировки появятся на шаге 9. Здесь честные нули,
        # а не null: срабатываний действительно не было ни одного, потому что
        # движка ещё нет, и это видно по нулям, а не по прочерку.
        "rules_fired": 0,
        "locks": 0,
        "locks_kept": 0,
        # Зачёт дня и причина берутся из отметки стрика, а не пересчитываются
        # здесь заново: две копии одного правила разойдутся, и на экране
        # окажется одна причина, а в расчёте другая.
        "counted_in_streak": None if mark is None else mark.counted,
        "streak_reason": None if mark is None else mark.reason,
        "streak_reason_text": (
            None if mark is None else streak_rules.REASON_TEXT.get(mark.reason)
        ),
    }


async def _period_items(
    s: AsyncSession,
    user_id: uuid.UUID,
    level: str,
    since: dt.date,
    until: dt.date,
    today: dt.date,
    by_start: dict,
    tags: dict,
    comments: dict,
) -> list[dict]:
    items: list[dict] = []
    start, _ = periods.bounds(level, since)
    while start <= until:
        period_start, period_end = periods.bounds(level, start)
        if period_start > today:
            break
        entry = by_start.get(period_start)
        items.append(
            {
                "level": level,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "entry": (
                    None
                    if entry is None
                    else daybook.entry_out(
                        entry, tags.get(entry.id, []), comments.get(entry.id, [])
                    )
                ),
                "facts": await period_facts(s, user_id, period_start, period_end),
            }
        )
        start = _next_period(level, period_start)
    items.reverse()
    return items


def _next_period(level: str, period_start: dt.date) -> dt.date:
    if level == periods.WEEK:
        return period_start + dt.timedelta(days=7)
    return periods.last_day_of_month(period_start) + dt.timedelta(days=1)


async def period_facts(
    s: AsyncSession, user_id: uuid.UUID, since: dt.date, until: dt.date
) -> dict:
    """Метрики периода из ТЗ 5.1 — то, поверх чего пишется запись."""
    window = (since, until)
    computed, confidence = await trades_service.marking_metrics(s, user_id, window)
    totals = await trades_service.totals(s, user_id, window, None)
    summaries = await trades_repo.day_summaries(s, user_id, since, until)
    days = {
        row.day: row
        for row in await daybook_repo.days_in_range(s, user_id, since, until)
    }

    # «Дней без допуска» — дни со сделками, когда допуска не было: и явный
    # отказ, и пропущенный чек (ТЗ 5.2: пропуск чека при наличии сделок —
    # то же самое). Иначе самый простой способ обойти допуск — не проходить его.
    without = sum(
        1
        for day, summary in summaries.items()
        if summary["trades"] > 0
        and (day not in days or days[day].admission in (None, "denied"))
    )

    marking = computed.as_dict()
    tags = await daybook_repo.tag_counts_in_range(s, user_id, since, until)
    return {
        "trades": totals["count"],
        "significant_trades": totals["significant_count"],
        "violations": totals["violations_count"],
        "unmarked": totals["unmarked_count"],
        "profit_usd": str(trades_service.quantize_money(totals["profit_usd"])),
        "account_return_pct": str(
            trades_service.quantize_pct(totals["account_return_pct"])
        ),
        "coverage_pct": marking["coverage_pct"],
        "discipline_pct": marking["discipline_pct"],
        "emotion_cost_usd": marking["emotion_cost_usd"],
        "lost_on_emotions_usd": marking["lost_on_emotions_usd"],
        "violations_profitable": marking["violations"]["profitable"],
        "violations_gain_usd": marking["violations_gain_usd"],
        "days_without_admission": without,
        "days_with_trades": len(summaries),
        # Ментальные теги периода со счётчиком дней (ТЗ 5.3).
        "tags": [{"tag": tag, "days": days} for tag, days in tags],
        # Блокировки появятся на шаге 9. Нули честные: срабатываний не было,
        # потому что движка нет. Compliance при нуле блокировок — 100%,
        # и подпись «блокировок не было» не даёт принять это за измерение.
        "locks": 0,
        "locks_kept": 0,
        "rules_fired": 0,
        "counted_in_streak": None,
        "confidence": confidence,
    }


async def entry_with_facts(
    s: AsyncSession, user_id: uuid.UUID, entry
) -> dict:
    tags = await daybook_repo.tags_of(s, [entry.id])
    comments = await daybook_repo.comments_of(s, [entry.id])
    return {
        "level": entry.level,
        "period_start": entry.period_start.isoformat(),
        "period_end": entry.period_end.isoformat(),
        "entry": daybook.entry_out(
            entry, tags.get(entry.id, []), comments.get(entry.id, [])
        ),
        "facts": await period_facts(s, user_id, entry.period_start, entry.period_end),
    }
