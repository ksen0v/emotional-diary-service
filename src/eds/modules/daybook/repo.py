"""Доступ к таблицам daybook."""

import datetime as dt
import uuid

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.daybook.models import (
    Entry,
    EntryComment,
    EntryTag,
    PremarketCheck,
    SessionReview,
    TradingDay,
)


async def day_of(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> TradingDay | None:
    res = await s.execute(
        select(TradingDay).where(TradingDay.user_id == user_id, TradingDay.day == day)
    )
    return res.scalar_one_or_none()


async def ensure_day(s: AsyncSession, user_id: uuid.UUID, day: dt.date) -> TradingDay:
    """Строка дня создаётся при первом обращении, а не по расписанию.

    Иначе пришлось бы каждую ночь заводить строки всем пользователям, включая
    тех, кто не торгует, и появление дня зависело бы от живости планировщика.
    """
    row = await day_of(s, user_id, day)
    if row is not None:
        return row
    row = TradingDay(
        user_id=user_id,
        day=day,
        admission=None,
        check_score=None,
        session_opened_at=None,
        session_closed_at=None,
        review_state="none",
    )
    s.add(row)
    await s.flush()
    return row


async def check_of(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> PremarketCheck | None:
    res = await s.execute(
        select(PremarketCheck).where(
            PremarketCheck.user_id == user_id, PremarketCheck.day == day
        )
    )
    return res.scalar_one_or_none()


async def save_check(
    s: AsyncSession,
    user_id: uuid.UUID,
    day: dt.date,
    *,
    answers: dict[str, int],
    score: int,
    verdict: str,
) -> PremarketCheck:
    row = PremarketCheck(
        id=uuid.uuid4(),
        user_id=user_id,
        day=day,
        answers=answers,
        score=score,
        verdict=verdict,
        created_at=dt.datetime.now(dt.UTC),
    )
    s.add(row)
    await s.flush()
    return row


async def days_with_open_session(
    s: AsyncSession, user_id: uuid.UUID, before: dt.date
) -> list[TradingDay]:
    """Дни с незакрытой сессией, которые уже кончились."""
    res = await s.execute(
        select(TradingDay)
        .where(
            TradingDay.user_id == user_id,
            TradingDay.day < before,
            TradingDay.session_opened_at.is_not(None),
            TradingDay.session_closed_at.is_(None),
        )
        .order_by(TradingDay.day)
    )
    return list(res.scalars())


async def recent_days(
    s: AsyncSession, user_id: uuid.UUID, limit: int = 30
) -> list[TradingDay]:
    res = await s.execute(
        select(TradingDay)
        .where(TradingDay.user_id == user_id)
        .order_by(TradingDay.day.desc())
        .limit(limit)
    )
    return list(res.scalars())


# --- дневник ---


async def entry_of(
    s: AsyncSession, user_id: uuid.UUID, level: str, period_start: dt.date
) -> Entry | None:
    res = await s.execute(
        select(Entry).where(
            Entry.user_id == user_id,
            Entry.level == level,
            Entry.period_start == period_start,
        )
    )
    return res.scalar_one_or_none()


async def entry_by_id(
    s: AsyncSession, user_id: uuid.UUID, entry_id: uuid.UUID
) -> Entry | None:
    res = await s.execute(
        select(Entry).where(Entry.id == entry_id, Entry.user_id == user_id)
    )
    return res.scalar_one_or_none()


async def entries_in_range(
    s: AsyncSession, user_id: uuid.UUID, level: str, since: dt.date, until: dt.date
) -> list[Entry]:
    res = await s.execute(
        select(Entry)
        .where(
            Entry.user_id == user_id,
            Entry.level == level,
            Entry.period_start >= since,
            Entry.period_start <= until,
        )
        .order_by(Entry.period_start.desc())
    )
    return list(res.scalars())


async def create_entry(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    level: str,
    period_start: dt.date,
    period_end: dt.date,
    editable_until: dt.datetime,
) -> Entry:
    now = dt.datetime.now(dt.UTC)
    row = Entry(
        id=uuid.uuid4(),
        user_id=user_id,
        level=level,
        period_start=period_start,
        period_end=period_end,
        score=None,
        status=None,
        body=None,
        editable_until=editable_until,
        created_at=now,
        updated_at=now,
    )
    s.add(row)
    await s.flush()
    return row


async def replace_tags(s: AsyncSession, entry_id: uuid.UUID, tags: list[str]) -> None:
    await s.execute(sql_delete(EntryTag).where(EntryTag.entry_id == entry_id))
    for tag in tags:
        s.add(EntryTag(entry_id=entry_id, tag=tag))
    await s.flush()


async def tags_of(
    s: AsyncSession, entry_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not entry_ids:
        return {}
    res = await s.execute(
        select(EntryTag.entry_id, EntryTag.tag)
        .where(EntryTag.entry_id.in_(entry_ids))
        .order_by(EntryTag.tag)
    )
    out: dict[uuid.UUID, list[str]] = {}
    for entry_id, tag in res:
        out.setdefault(entry_id, []).append(tag)
    return out


async def add_comment(
    s: AsyncSession, entry_id: uuid.UUID, body: str
) -> EntryComment:
    row = EntryComment(
        id=uuid.uuid4(),
        entry_id=entry_id,
        body=body,
        created_at=dt.datetime.now(dt.UTC),
    )
    s.add(row)
    await s.flush()
    return row


async def comments_of(
    s: AsyncSession, entry_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[EntryComment]]:
    if not entry_ids:
        return {}
    res = await s.execute(
        select(EntryComment)
        .where(EntryComment.entry_id.in_(entry_ids))
        .order_by(EntryComment.created_at)
    )
    out: dict[uuid.UUID, list[EntryComment]] = {}
    for row in res.scalars():
        out.setdefault(row.entry_id, []).append(row)
    return out


# --- разбор ---


async def review_of(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> SessionReview | None:
    res = await s.execute(
        select(SessionReview).where(
            SessionReview.user_id == user_id, SessionReview.day == day
        )
    )
    return res.scalar_one_or_none()


async def save_review(
    s: AsyncSession,
    user_id: uuid.UUID,
    day: dt.date,
    *,
    plan_followed: str,
    pull_text: str | None,
    execution_score: int | None,
    takeaway: str | None,
) -> SessionReview:
    row = SessionReview(
        id=uuid.uuid4(),
        user_id=user_id,
        day=day,
        plan_followed=plan_followed,
        pull_text=pull_text,
        execution_score=execution_score,
        takeaway=takeaway,
        created_at=dt.datetime.now(dt.UTC),
    )
    s.add(row)
    await s.flush()
    return row


async def oldest_pending_review(
    s: AsyncSession, user_id: uuid.UUID, before: dt.date
) -> TradingDay | None:
    """Самый старый незакрытый разбор среди прошедших дней.

    Именно прошедших: разбор за сегодня не может мешать сегодняшнему чеку —
    чек уже пройден, а сессия ещё не закрыта.
    """
    res = await s.execute(
        select(TradingDay)
        .where(
            TradingDay.user_id == user_id,
            TradingDay.day < before,
            TradingDay.review_state == "pending",
        )
        .order_by(TradingDay.day)
        .limit(1)
    )
    return res.scalar_one_or_none()


async def days_in_range(
    s: AsyncSession, user_id: uuid.UUID, since: dt.date, until: dt.date
) -> list[TradingDay]:
    res = await s.execute(
        select(TradingDay)
        .where(
            TradingDay.user_id == user_id,
            TradingDay.day >= since,
            TradingDay.day <= until,
        )
        .order_by(TradingDay.day)
    )
    return list(res.scalars())
