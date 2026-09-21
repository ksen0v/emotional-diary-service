"""Приём сделок и чтение ленты.

Приём идемпотентен по (user_id, source, external_id): одна и та же сделка приходит
и из потока, и из сверки, а обработаться должна один раз. Это не оптимизация —
без этого лента удваивалась бы при каждом переподключении.
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts import events as ev
from eds.contracts.ingest import IngestContext, IngestReport
from eds.contracts.source import IncomingTag, IncomingTrade
from eds.modules.trades import metrics, normalize, repo
from eds.modules.trades.models import Trade, TradeTag
from eds.platform import bus
from eds.platform.errors import AppError


async def ingest_batch(
    s: AsyncSession, ctx: IngestContext, incoming: list[IncomingTrade]
) -> IngestReport:
    report = IngestReport()

    # Порядок обязателен: при сверке порция приходит неупорядоченной, а счётчики
    # серий зависят от последовательности. Сортируем до приёма, а не после.
    ordered = sorted(incoming, key=lambda t: (t.close_time or t.open_time, t.external_id))

    for item in ordered:
        report.received += 1

        if item.is_open or item.close_time is None:
            # Открытые позиции в MVP не принимаем (ТЗ 4.5, решение ОВ-3).
            report.skipped_open += 1
            continue

        if item.close_time < ctx.ingest_from:
            # Истории не импортируем: точка отсчёта — момент подключения.
            report.skipped_before_ingest_from += 1
            continue

        await _ingest_one(s, ctx, item, report)

    return report


async def _ingest_one(
    s: AsyncSession, ctx: IngestContext, item: IncomingTrade, report: IngestReport
) -> None:
    account_id = ctx.account_ids.get(item.account_external_id)
    if account_id is None:
        # Счёт, которого мы не знаем. Падать на одной сделке из порции нельзя,
        # но и прятать её среди других пропусков тоже: у неё свой счётчик.
        report.skipped_unknown_account += 1
        return

    marking = normalize.marking_of(item.tags, set(ctx.violation_tag_ids))
    tags_hash = normalize.tags_hash(item.tags)
    existing = await repo.by_external(s, ctx.user_id, ctx.source, item.external_id)

    if existing is None:
        trade = Trade(
            id=uuid.uuid4(),
            user_id=ctx.user_id,
            account_id=account_id,
            source=ctx.source,
            external_id=item.external_id,
            symbol=item.symbol,
            side=item.side,
            profit_usd=item.profit_usd,
            percent=item.percent,
            size_usd=item.size_usd,
            leverage=item.leverage,
            account_return_pct=item.account_return_pct,
            duration_sec=item.duration_sec,
            open_time=item.open_time,
            close_time=item.close_time,
            is_open=False,
            trading_day=normalize.trading_day(
                item.open_time, ctx.timezone, ctx.day_cutoff
            ),
            is_significant=normalize.is_significant(
                item.account_return_pct, ctx.significance_pct
            ),
            marking=marking,
            marked_by="source_tag",
            tags_hash=tags_hash,
            raw=item.raw,
            created_at=dt.datetime.now(dt.UTC),
            updated_at=dt.datetime.now(dt.UTC),
        )
        await repo.insert(s, trade, _tag_rows(item))
        await bus.publish(
            s,
            ev.TRADES_INGESTED,
            {
                "user_id": str(ctx.user_id),
                "trade_id": str(trade.id),
                "trading_day": trade.trading_day.isoformat(),
                "marking": trade.marking,
                "is_significant": trade.is_significant,
                "account_return_pct": str(trade.account_return_pct),
                "profit_usd": str(trade.profit_usd),
            },
            dedup_key=f"ingested:{trade.id}",
        )
        report.inserted += 1
        return

    if existing.tags_hash == tags_hash:
        report.unchanged += 1
        return

    # Изменилась разметка. Событие идёт только на это: обновление сделки
    # приходит на любое изменение, а движку правил интересен лишь тег.
    previous = existing.marking
    existing.marking = marking
    existing.tags_hash = tags_hash
    existing.marked_by = "source_tag"
    existing.updated_at = dt.datetime.now(dt.UTC)
    await repo.replace_tags(s, existing.id, _tag_rows(item))
    await bus.publish(
        s,
        ev.TRADES_MARKING_CHANGED,
        {
            "user_id": str(ctx.user_id),
            "trade_id": str(existing.id),
            "trading_day": existing.trading_day.isoformat(),
            "marking_before": previous,
            "marking_after": marking,
        },
        dedup_key=f"marking:{existing.id}:{tags_hash}",
    )
    report.remarked += 1


def _tag_rows(item: IncomingTrade) -> list[TradeTag]:
    return [
        TradeTag(
            external_id=tag.external_id,
            column_key=tag.column_key,
            name=tag.name,
        )
        for tag in item.tags
    ]


async def remark_all(s: AsyncSession, ctx: IngestContext) -> int:
    """Пересчитать разметку всех сделок источника по текущему словарю тегов.

    Вызывается, когда трейдер изменил, какие теги считаются нарушением.
    Разметка — производная от тегов и словаря, поэтому пересчитывается целиком,
    а не правится по месту: иначе после нескольких правок словаря состояние
    разъедется, и восстановить его будет нечем.
    """
    trades = await repo.all_of_source(s, ctx.user_id, ctx.source)
    tags_by_trade = await repo.tags_of(s, [t.id for t in trades])
    violations = set(ctx.violation_tag_ids)
    changed = 0

    for trade in trades:
        tags = tags_by_trade.get(trade.id, [])
        wanted = normalize.marking_of(
            [
                _as_incoming(tag)
                for tag in tags
            ],
            violations,
        )
        if wanted == trade.marking:
            continue
        previous = trade.marking
        trade.marking = wanted
        trade.updated_at = dt.datetime.now(dt.UTC)
        changed += 1
        await bus.publish(
            s,
            ev.TRADES_MARKING_CHANGED,
            {
                "user_id": str(ctx.user_id),
                "trade_id": str(trade.id),
                "trading_day": trade.trading_day.isoformat(),
                "marking_before": previous,
                "marking_after": wanted,
                "cause": "tag_dictionary_changed",
            },
            dedup_key=f"remark:{trade.id}:{previous}->{wanted}:{trade.updated_at.isoformat()}",
        )

    await s.flush()
    return changed


def _as_incoming(tag: TradeTag) -> IncomingTag:
    return IncomingTag(
        external_id=tag.external_id, name=tag.name, column_key=tag.column_key
    )


async def mark_by_user(
    s: AsyncSession,
    user_id: uuid.UUID,
    trade_id: uuid.UUID,
    marking: str,
    *,
    source_provides_tags: bool,
) -> tuple[Trade, dict]:
    """Своя разметка сделки.

    Работает только когда активный источник не отдаёт теги (Binance). При TMM
    разметка живёт в дневнике трейдера, и вторая точка правды здесь означала бы,
    что одна и та же сделка размечена двумя способами по-разному.
    """
    if source_provides_tags:
        raise AppError(
            "not_supported_by_source",
            "Разметка приходит из дневника источника. Поставь тег там — "
            "сервис увидит его сам.",
            409,
        )
    if marking not in (normalize.MARK_CLEAN, normalize.MARK_VIOLATION):
        raise AppError(
            "validation_failed",
            "Разметка: «по системе» или «нарушение».",
            400,
        )

    trade = await repo.by_id(s, user_id, trade_id)
    if trade is None:
        raise AppError("not_found", "Сделка не найдена.", 404)

    previous = trade.marking
    if previous == marking:
        return trade, {"changed": False}

    trade.marking = marking
    trade.marked_by = "user"
    trade.updated_at = dt.datetime.now(dt.UTC)
    await s.flush()

    await bus.publish(
        s,
        ev.TRADES_MARKING_CHANGED,
        {
            "user_id": str(user_id),
            "trade_id": str(trade.id),
            "trading_day": trade.trading_day.isoformat(),
            "marking_before": previous,
            "marking_after": marking,
            "cause": "user_marking",
        },
        dedup_key=f"user-mark:{trade.id}:{trade.updated_at.isoformat()}",
    )
    return trade, {"changed": True, "marking_before": previous}


async def marking_metrics(
    s: AsyncSession, user_id: uuid.UUID, days: tuple[dt.date, dt.date] | None
) -> tuple[metrics.MarkingMetrics, dict]:
    counts = await repo.marking_counts(s, user_id, days)
    days_with_trades = await repo.days_count(s, user_id, days)
    return metrics.compute(**counts), metrics.enough_data(days_with_trades)


# --- чтение ---


def period_days(
    period: str, today: dt.date
) -> tuple[dt.date, dt.date] | None:
    """Границы периода в торговых днях. Считает сервер, а не фронт."""
    if period == "today":
        return today, today
    if period == "week":
        return today - dt.timedelta(days=6), today
    if period == "month":
        return today - dt.timedelta(days=29), today
    return None


async def feed(
    s: AsyncSession,
    user_id: uuid.UUID,
    days: tuple[dt.date, dt.date] | None,
    marking: str | None,
    limit: int,
    cursor: tuple[dt.datetime, uuid.UUID] | None,
) -> tuple[list[Trade], dict[uuid.UUID, list[TradeTag]], bool]:
    rows = await repo.page(s, user_id, days, marking, limit, cursor)
    has_more = len(rows) > limit
    rows = rows[:limit]
    tags = await repo.tags_of(s, [row.id for row in rows])
    return rows, tags, has_more


async def curve(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> list[normalize.CurvePoint]:
    points = await repo.day_points(s, user_id, day)
    return normalize.day_curve([(at, str(ident), pct) for at, ident, pct in points])


async def totals(
    s: AsyncSession,
    user_id: uuid.UUID,
    days: tuple[dt.date, dt.date] | None,
    marking: str | None,
) -> dict:
    return await repo.totals(s, user_id, days, marking)


def quantize_money(value: Decimal | None) -> Decimal | None:
    """Наружу отдаём деньги с двумя знаками: фронт их только показывает."""
    return None if value is None else Decimal(value).quantize(Decimal("0.01"))


def quantize_pct(value: Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(value).quantize(Decimal("0.01"))
