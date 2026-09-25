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


async def marking_counts(
    s: AsyncSession, user_id: uuid.UUID, days: tuple[dt.date, dt.date] | None
) -> dict:
    """Числа для метрик разметки одним запросом.

    Отдельный запрос, а не проход по сделкам в питоне: метрики смотрят за месяц,
    и тянуть тысячи строк ради шести чисел — плохая идея с первого дня.
    """
    base = _filtered(user_id, days, None).subquery()
    res = await s.execute(
        select(
            func.count(),
            func.count().filter(base.c.is_significant.is_(True)),
            func.count().filter(base.c.marking == "clean"),
            func.count().filter(base.c.marking == "violation"),
            func.count().filter(base.c.marking == "unreviewed"),
            func.count().filter(
                (base.c.marking == "violation") & (base.c.profit_usd > 0)
            ),
            func.coalesce(
                func.sum(base.c.profit_usd).filter(base.c.marking == "violation"), 0
            ),
            # «Слито на эмоциях» и «нарушений в плюс» — две разные суммы,
            # а не одна со знаком: прибыльное нарушение подкрепляет поведение
            # и опаснее убыточного, поэтому его нельзя прятать в сальдо (ТЗ 5.1).
            func.coalesce(
                func.sum(base.c.profit_usd).filter(
                    (base.c.marking == "violation") & (base.c.profit_usd < 0)
                ),
                0,
            ),
            func.coalesce(
                func.sum(base.c.profit_usd).filter(
                    (base.c.marking == "violation") & (base.c.profit_usd > 0)
                ),
                0,
            ),
        ).select_from(base)
    )
    (
        trades_all,
        significant,
        clean,
        violations,
        unmarked,
        violations_profitable,
        violations_profit_sum,
        violations_loss_sum,
        violations_gain_sum,
    ) = res.one()
    return {
        "trades_all": trades_all,
        "trades_significant": significant,
        "clean": clean,
        "violations": violations,
        "unmarked": unmarked,
        "violations_profitable": violations_profitable,
        "violations_profit_sum": Decimal(violations_profit_sum),
        "violations_loss_sum": Decimal(violations_loss_sum),
        "violations_gain_sum": Decimal(violations_gain_sum),
    }


async def days_count(
    s: AsyncSession, user_id: uuid.UUID, days: tuple[dt.date, dt.date] | None
) -> int:
    """Сколько торговых дней со сделками попало в период — для порога достоверности."""
    base = _filtered(user_id, days, None).subquery()
    res = await s.execute(
        select(func.count(func.distinct(base.c.trading_day))).select_from(base)
    )
    return res.scalar_one()


async def day_summaries(
    s: AsyncSession, user_id: uuid.UUID, since: dt.date, until: dt.date
) -> dict[dt.date, dict]:
    """Итоги каждого дня в диапазоне — для календаря дневника.

    Одним запросом с группировкой, а не выборкой сделок: календарь месяца
    это тридцать дней, и тянуть все сделки месяца ради трёх чисел на клетку
    означало бы платить за экран, который открывают чаще остальных.
    """
    res = await s.execute(
        select(
            Trade.trading_day,
            func.count(),
            func.count().filter(Trade.marking == "violation"),
            func.count().filter(Trade.marking == "unreviewed"),
            func.coalesce(func.sum(Trade.profit_usd), 0),
            func.coalesce(func.sum(Trade.account_return_pct), 0),
            func.coalesce(
                func.sum(Trade.profit_usd).filter(Trade.marking == "violation"), 0
            ),
        )
        .where(
            Trade.user_id == user_id,
            Trade.trading_day >= since,
            Trade.trading_day <= until,
        )
        .group_by(Trade.trading_day)
        .order_by(Trade.trading_day)
    )
    return {
        row[0]: {
            "trades": row[1],
            "violations": row[2],
            "unmarked": row[3],
            "profit_usd": Decimal(row[4]),
            "account_return_pct": Decimal(row[5]),
            "emotion_cost_usd": Decimal(row[6]),
        }
        for row in res
    }


async def day_facts(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> list[tuple[uuid.UUID, dt.datetime, dt.datetime, Decimal, Decimal, bool]]:
    """Сделки дня в том виде, в каком их видит движок правил.

    Шесть полей вместо целой сделки: движку не нужны ни символ, ни плечо,
    ни разметка, и тянуть их значило бы обещать, что он на них смотрит.
    """
    res = await s.execute(
        select(
            Trade.id,
            Trade.open_time,
            Trade.close_time,
            Trade.account_return_pct,
            Trade.profit_usd,
            Trade.is_significant,
        )
        .where(
            Trade.user_id == user_id,
            Trade.trading_day == day,
            Trade.is_open.is_(False),
        )
        .order_by(Trade.close_time, Trade.id)
    )
    return [tuple(row) for row in res]  # type: ignore[misc]


async def opened_between(
    s: AsyncSession, user_id: uuid.UUID, since: dt.datetime, until: dt.datetime
) -> list[tuple[uuid.UUID, str, dt.datetime]]:
    """Сделки, ОТКРЫТЫЕ в окне. Вход compliance-проверки.

    Именно открытые: сделка, открытая до блокировки и закрытая внутри неё,
    нарушением не является (Архитектура ч.1 §7). Разница в одном слове,
    а цена ошибки — несправедливо сгоревший стрик.
    """
    res = await s.execute(
        select(Trade.id, Trade.symbol, Trade.open_time)
        .where(
            Trade.user_id == user_id,
            Trade.open_time >= since,
            Trade.open_time <= until,
        )
        .order_by(Trade.open_time)
    )
    return [(row[0], row[1], row[2]) for row in res]


async def violations_of_day(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> list[tuple[uuid.UUID, str, dt.datetime, dt.datetime | None]]:
    """Сделки дня, отмеченные нарушением. Вход системного триггера SR-1.

    Порядок — по времени закрытия: тег появляется на закрытой сделке, и если
    их несколько, инциденты должны лечь в том же порядке, в каком трейдер их
    закрывал, а не в порядке выдачи базы.
    """
    res = await s.execute(
        select(Trade.id, Trade.symbol, Trade.open_time, Trade.close_time)
        .where(
            Trade.user_id == user_id,
            Trade.trading_day == day,
            Trade.marking == "violation",
        )
        .order_by(Trade.close_time, Trade.id)
    )
    return [(row[0], row[1], row[2], row[3]) for row in res]


async def unmarked_closed_before(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date, moment: dt.datetime
) -> list[tuple[uuid.UUID, str, dt.datetime | None]]:
    """Неразмеченные сделки дня, закрытые раньше момента. Вход SR-4.

    Именно по времени ЗАКРЫТИЯ: напоминание отсчитывается от того момента,
    когда сделку стало можно разметить, а не когда она была открыта.
    """
    res = await s.execute(
        select(Trade.id, Trade.symbol, Trade.close_time)
        .where(
            Trade.user_id == user_id,
            Trade.trading_day == day,
            Trade.marking == "unreviewed",
            Trade.is_open.is_(False),
            Trade.close_time.is_not(None),
            Trade.close_time <= moment,
        )
        .order_by(Trade.close_time, Trade.id)
    )
    return [(row[0], row[1], row[2]) for row in res]


async def opened_after_in_day(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date, moment: dt.datetime
) -> list[tuple[uuid.UUID, str, dt.datetime]]:
    """Сделки того же торгового дня, ОТКРЫТЫЕ позже момента. Вход ретропроверки.

    Окно ретропроверки — от закрытия размеченной сделки до границы её
    торгового дня (ТЗ 4.4), поэтому границы у выборки две, и обе важны:

    - `trading_day == day` держит окно внутри дня сделки. Верхняя граница
      окна — это конец дня, и выражать её временем значило бы посчитать её
      второй раз, уже из таймзоны, в которой она однажды уже посчиталась при
      приёме сделки. Торговый день сделки фиксируется при приёме и больше
      не меняется (ТЗ 9.4) — значит он и есть окно.
    - `open_time > moment` строго больше: сделка, открытая ровно в момент
      закрытия предыдущей, открыта не после неё, а вместе с ней, и вешать
      на неё нарушение было бы придиркой к секунде.

    Именно открытые, а не закрытые: то же правило, что у compliance-проверки
    (Архитектура ч.1 §7). Сделка, открытая до тега и закрытая после, торговлей
    «после несистемной сделки» не является.
    """
    res = await s.execute(
        select(Trade.id, Trade.symbol, Trade.open_time)
        .where(
            Trade.user_id == user_id,
            Trade.trading_day == day,
            Trade.open_time > moment,
        )
        .order_by(Trade.open_time, Trade.id)
    )
    return [(row[0], row[1], row[2]) for row in res]
