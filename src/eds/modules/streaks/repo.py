"""Доступ к таблицам streaks."""

import datetime as dt
import uuid

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.streaks import DayMark
from eds.modules.streaks.models import DayMarkRow, StateRow


async def upsert_mark(s: AsyncSession, user_id: uuid.UUID, mark: DayMark) -> None:
    stmt = (
        pg_insert(DayMarkRow)
        .values(
            user_id=user_id, day=mark.day, counted=mark.counted, reason=mark.reason
        )
        .on_conflict_do_update(
            index_elements=[DayMarkRow.user_id, DayMarkRow.day],
            set_={"counted": mark.counted, "reason": mark.reason},
        )
    )
    await s.execute(stmt)


async def delete_mark(s: AsyncSession, user_id: uuid.UUID, day: dt.date) -> None:
    """Отметку убираем, когда день стал нейтральным.

    Такое бывает: трейдер удалил запись за выходной, и день снова перестал
    участвовать в расчёте. Оставить старую отметку значило бы считать серию
    по данным, которых больше нет.
    """
    await s.execute(
        sql_delete(DayMarkRow).where(
            DayMarkRow.user_id == user_id, DayMarkRow.day == day
        )
    )


async def marks_of(
    s: AsyncSession, user_id: uuid.UUID, since: dt.date | None = None
) -> list[DayMark]:
    query = select(DayMarkRow).where(DayMarkRow.user_id == user_id)
    if since is not None:
        query = query.where(DayMarkRow.day >= since)
    res = await s.execute(query.order_by(DayMarkRow.day))
    return [
        DayMark(day=row.day, counted=row.counted, reason=row.reason)
        for row in res.scalars()
    ]


async def marked_days(
    s: AsyncSession, user_id: uuid.UUID, since: dt.date, until: dt.date
) -> dict[dt.date, DayMark]:
    res = await s.execute(
        select(DayMarkRow).where(
            DayMarkRow.user_id == user_id,
            DayMarkRow.day >= since,
            DayMarkRow.day <= until,
        )
    )
    return {
        row.day: DayMark(day=row.day, counted=row.counted, reason=row.reason)
        for row in res.scalars()
    }


async def state_of(s: AsyncSession, user_id: uuid.UUID) -> StateRow | None:
    res = await s.execute(select(StateRow).where(StateRow.user_id == user_id))
    return res.scalar_one_or_none()


async def ensure_state(s: AsyncSession, user_id: uuid.UUID) -> StateRow:
    row = await state_of(s, user_id)
    if row is not None:
        return row
    row = StateRow(
        user_id=user_id,
        current=0,
        best=0,
        last_day=None,
        freezes_month=None,
        freezes_used=0,
        updated_at=dt.datetime.now(dt.UTC),
    )
    s.add(row)
    await s.flush()
    return row
