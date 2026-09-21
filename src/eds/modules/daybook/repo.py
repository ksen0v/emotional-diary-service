"""Доступ к таблицам daybook."""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.daybook.models import PremarketCheck, TradingDay


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
