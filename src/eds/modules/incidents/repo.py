"""Доступ к таблицам incidents."""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.incidents.models import (
    ACTIVE,
    BREACHED,
    CODE_LOCK_BREACHED,
    KEPT,
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


async def breach_counts(
    s: AsyncSession, user_id: uuid.UUID, since: dt.date, until: dt.date
) -> dict[dt.date, int]:
    """Сколько блокировок нарушено в каждом дне диапазона. Вход стрика.

    Считаются инциденты SR-2, а не блокировки в состоянии `breached`: день
    рвёт сам факт сделки во время блокировки, а блокировка при этом одна,
    даже если сделок внутри неё было три.
    """
    res = await s.execute(
        select(IncidentRow.day, func.count())
        .where(
            IncidentRow.user_id == user_id,
            IncidentRow.day >= since,
            IncidentRow.day <= until,
            IncidentRow.code == CODE_LOCK_BREACHED,
        )
        .group_by(IncidentRow.day)
    )
    return {row[0]: row[1] for row in res}


def _filtered(user_id: uuid.UUID, since: dt.date, until: dt.date, outcome: str):
    where = [
        IncidentRow.user_id == user_id,
        IncidentRow.day >= since,
        IncidentRow.day <= until,
    ]
    if outcome == "kept":
        where.append(IncidentRow.outcome == KEPT)
    elif outcome == "breached":
        where.append(IncidentRow.outcome == BREACHED)
    return where


async def page(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    since: dt.date,
    until: dt.date,
    outcome: str = "all",
    limit: int = 50,
    cursor: tuple[dt.datetime, uuid.UUID] | None = None,
) -> list[IncidentRow]:
    """Лента инцидентов, новые сверху. Курсор по (opened_at, id).

    Курсорная пагинация, а не offset: лента пополняется в реальном времени,
    и при offset вторая страница показала бы сдвинутые данные (ч.2 §1.5).
    """
    where = _filtered(user_id, since, until, outcome)
    if cursor is not None:
        at, ident = cursor
        where.append(
            or_(
                IncidentRow.opened_at < at,
                (IncidentRow.opened_at == at) & (IncidentRow.id < ident),
            )
        )
    res = await s.execute(
        select(IncidentRow)
        .where(*where)
        .order_by(IncidentRow.opened_at.desc(), IncidentRow.id.desc())
        .limit(limit + 1)
    )
    return list(res.scalars())


async def totals(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    since: dt.date,
    until: dt.date,
    outcome: str = "all",
) -> dict[str, int]:
    """Сводка над лентой: всего, соблюдено, нарушено.

    Считается по всей выборке фильтра, а не по странице: иначе полоса над
    лентой менялась бы при прокрутке (ч.2 §3.4, то же правило).
    """
    where = _filtered(user_id, since, until, outcome)
    res = await s.execute(
        select(
            func.count(),
            func.count().filter(IncidentRow.outcome == KEPT),
            func.count().filter(IncidentRow.outcome == BREACHED),
            func.count().filter(IncidentRow.outcome == OPEN),
        ).where(*where)
    )
    row = res.one()
    return {"count": row[0], "kept": row[1], "breached": row[2], "open": row[3]}


async def locks_of(
    s: AsyncSession, user_id: uuid.UUID, incident_ids: list[uuid.UUID]
) -> dict[uuid.UUID, LockRow]:
    """Блокировки по инцидентам — одним запросом на страницу ленты."""
    if not incident_ids:
        return {}
    res = await s.execute(
        select(LockRow).where(
            LockRow.user_id == user_id, LockRow.incident_id.in_(incident_ids)
        )
    )
    return {row.incident_id: row for row in res.scalars()}
