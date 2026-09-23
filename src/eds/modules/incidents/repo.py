"""Доступ к таблицам incidents."""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.incidents.models import (
    ACTIVE,
    OPEN,
    IncidentRow,
    LockReviewRow,
    LockRow,
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def insert_incident(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    day: dt.date,
    code: str,
    rule_id: uuid.UUID | None,
    details: dict[str, Any],
    shadow: bool,
) -> IncidentRow | None:
    """Создать инцидент. None — такой уже есть.

    Ключ повтора — сделка, на которой правило сработало: она приходит и из
    потока, и из сверки, и обработаться должна один раз. Уникальность стоит
    в базе, поэтому одновременные запросы не создадут двух инцидентов.
    """
    stmt = (
        pg_insert(IncidentRow)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            day=day,
            rule_id=rule_id,
            code=code,
            outcome=OPEN,
            shadow=shadow,
            opened_at=_now(),
            closed_at=None,
            details=details,
        )
        .on_conflict_do_nothing()
        .returning(IncidentRow.id)
    )
    res = await s.execute(stmt)
    row = res.first()
    if row is None:
        return None
    return await by_id(s, user_id, row[0])


async def by_id(
    s: AsyncSession, user_id: uuid.UUID, incident_id: uuid.UUID
) -> IncidentRow | None:
    res = await s.execute(
        select(IncidentRow).where(
            IncidentRow.id == incident_id, IncidentRow.user_id == user_id
        )
    )
    return res.scalar_one_or_none()


async def incidents_of_day(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> list[IncidentRow]:
    res = await s.execute(
        select(IncidentRow)
        .where(IncidentRow.user_id == user_id, IncidentRow.day == day)
        .order_by(IncidentRow.opened_at)
    )
    return list(res.scalars())


async def insert_lock(s: AsyncSession, row: LockRow) -> LockRow:
    s.add(row)
    await s.flush()
    return row


async def active_lock(s: AsyncSession, user_id: uuid.UUID) -> LockRow | None:
    res = await s.execute(
        select(LockRow).where(LockRow.user_id == user_id, LockRow.state == ACTIVE)
    )
    return res.scalar_one_or_none()


async def lock_by_id(
    s: AsyncSession, user_id: uuid.UUID, lock_id: uuid.UUID
) -> LockRow | None:
    res = await s.execute(
        select(LockRow).where(LockRow.id == lock_id, LockRow.user_id == user_id)
    )
    return res.scalar_one_or_none()


async def locks_of_day(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> list[LockRow]:
    res = await s.execute(
        select(LockRow)
        .where(LockRow.user_id == user_id, LockRow.day == day)
        .order_by(LockRow.started_at)
    )
    return list(res.scalars())


async def review_of(s: AsyncSession, lock_id: uuid.UUID) -> LockReviewRow | None:
    res = await s.execute(
        select(LockReviewRow).where(LockReviewRow.lock_id == lock_id)
    )
    return res.scalar_one_or_none()


async def save_review(
    s: AsyncSession, lock_id: uuid.UUID, *, q1: str, q2: str, q3: str
) -> LockReviewRow:
    row = LockReviewRow(lock_id=lock_id, q1=q1, q2=q2, q3=q3, filled_at=_now())
    s.add(row)
    await s.flush()
    return row
