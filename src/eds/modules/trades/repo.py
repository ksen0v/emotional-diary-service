"""Доступ к таблицам trades."""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.trades.models import Trade, TradeTag


async def by_external(
    s: AsyncSession, user_id: uuid.UUID, source: str, external_id: str
) -> Trade | None:
    res = await s.execute(
        select(Trade).where(
            Trade.user_id == user_id,
            Trade.source == source,
            Trade.external_id == external_id,
        )
    )
    return res.scalar_one_or_none()


async def by_id(s: AsyncSession, user_id: uuid.UUID, trade_id: uuid.UUID) -> Trade | None:
    res = await s.execute(
        select(Trade).where(Trade.id == trade_id, Trade.user_id == user_id)
    )
    return res.scalar_one_or_none()


async def insert(s: AsyncSession, trade: Trade, tags: list[TradeTag]) -> Trade:
    s.add(trade)
    await s.flush()
    for tag in tags:
        tag.trade_id = trade.id
        s.add(tag)
    await s.flush()
    return trade


async def replace_tags(
    s: AsyncSession, trade_id: uuid.UUID, tags: list[TradeTag]
) -> None:
    await s.execute(delete(TradeTag).where(TradeTag.trade_id == trade_id))
    for tag in tags:
        tag.trade_id = trade_id
        s.add(tag)
    await s.flush()


async def tags_of(s: AsyncSession, trade_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[TradeTag]]:
    if not trade_ids:
        return {}
    res = await s.execute(select(TradeTag).where(TradeTag.trade_id.in_(trade_ids)))
    grouped: dict[uuid.UUID, list[TradeTag]] = {}
    for tag in res.scalars():
        grouped.setdefault(tag.trade_id, []).append(tag)
    return grouped


def _filtered(user_id: uuid.UUID, days: tuple[dt.date, dt.date] | None, marking: str | None):
    query = select(Trade).where(Trade.user_id == user_id)
    if days is not None:
        query = query.where(Trade.trading_day >= days[0], Trade.trading_day <= days[1])
    if marking == "unmarked":
        query = query.where(Trade.marking == "unreviewed")
    elif marking == "violations":
        query = query.where(Trade.marking == "violation")
    return query


async def page(
    s: AsyncSession,
    user_id: uuid.UUID,
    days: tuple[dt.date, dt.date] | None,
    marking: str | None,
    limit: int,
    cursor: tuple[dt.datetime, uuid.UUID] | None,
) -> list[Trade]:
    """Страница ленты. Сортировка close_time DESC, id DESC — под курсор."""
    query = _filtered(user_id, days, marking)
    if cursor is not None:
        at, ident = cursor
        query = query.where(
            (Trade.close_time < at)
            | ((Trade.close_time == at) & (Trade.id < ident))
        )
    query = query.order_by(Trade.close_time.desc(), Trade.id.desc()).limit(limit + 1)
    res = await s.execute(query)
    return list(res.scalars())


async def totals(
    s: AsyncSession,
    user_id: uuid.UUID,
    days: tuple[dt.date, dt.date] | None,
    marking: str | None,
) -> dict:
    """Итоги по всей выборке фильтра, а не по странице.

    Иначе шапка ленты менялась бы при прокрутке, и трейдер решил бы,
    что сервис путается в своих же числах.
    """
    base = _filtered(user_id, days, marking).subquery()
    res = await s.execute(
        select(
            func.count(),
            func.coalesce(func.sum(base.c.profit_usd), 0),
            func.coalesce(func.sum(base.c.account_return_pct), 0),
            func.count().filter(base.c.is_significant.is_(True)),
            func.count().filter(base.c.marking == "violation"),
            func.count().filter(base.c.marking == "unreviewed"),
        ).select_from(base)
    )
    count, profit, ret, significant, violations, unmarked = res.one()
    marked = count - unmarked
    coverage = (Decimal(marked) / Decimal(count) * 100) if count else Decimal("0")
    return {
        "count": count,
        "significant_count": significant,
        "violations_count": violations,
        "unmarked_count": unmarked,
        "profit_usd": profit,
        "account_return_pct": ret,
        "coverage_pct": coverage,
    }


async def day_points(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> list[tuple[dt.datetime, uuid.UUID, Decimal]]:
    """Точки кривой дня: только закрытые сделки, по времени закрытия."""
    res = await s.execute(
        select(Trade.close_time, Trade.id, Trade.account_return_pct)
        .where(
            Trade.user_id == user_id,
            Trade.trading_day == day,
            Trade.is_open.is_(False),
            Trade.close_time.is_not(None),
        )
        .order_by(Trade.close_time, Trade.id)
    )
    return [(row[0], row[1], row[2]) for row in res]


async def days_with_trades(
    s: AsyncSession, user_id: uuid.UUID, limit: int = 30
) -> list[dt.date]:
    res = await s.execute(
        select(Trade.trading_day)
        .where(Trade.user_id == user_id)
        .group_by(Trade.trading_day)
        .order_by(Trade.trading_day.desc())
        .limit(limit)
    )
    return [row[0] for row in res]


async def all_of_source(
    s: AsyncSession, user_id: uuid.UUID, source: str
) -> list[Trade]:
    res = await s.execute(
        select(Trade)
        .where(Trade.user_id == user_id, Trade.source == source)
        .order_by(Trade.close_time)
    )
    return list(res.scalars())
