"""Сверка: забрать сделки у активного источника и отдать их приёмнику."""

import datetime as dt
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.ingest import IngestContext, IngestReport
from eds.contracts.source import TradeSource
from eds.modules.identity import repo as identity_repo
from eds.modules.source import repo as source_repo
from eds.modules.source.adapters.fake.source import FakeSource
from eds.modules.source.models import Connection
from eds.modules.trades import service as trades_service
from eds.platform.errors import AppError

log = logging.getLogger("eds.pipeline")


def adapter_for(s: AsyncSession, connection: Connection) -> TradeSource:
    """Адаптер по провайдеру. Шаг 4 добавит сюда tmm, шаг 14 — binance."""
    if connection.provider == "fake":
        return FakeSource(s, connection.id)
    raise AppError(
        "provider_not_supported",
        f"Источник «{connection.provider}» пока не поддерживается.",
        501,
    )


async def build_context(
    s: AsyncSession, user_id: uuid.UUID, connection: Connection
) -> IngestContext:
    settings_row = await identity_repo.settings_of(s, user_id)
    if settings_row is None:
        raise AppError("not_found", "Настройки пользователя не найдены.", 404)

    accounts = await source_repo.accounts_of(s, connection.id)
    violations = await source_repo.violation_tag_ids(s, connection.id)

    return IngestContext(
        user_id=user_id,
        source=connection.provider,
        connection_id=connection.id,
        account_ids={a.external_id: a.id for a in accounts},
        violation_tag_ids=frozenset(violations),
        timezone=settings_row.timezone,
        day_cutoff=settings_row.day_cutoff,
        significance_pct=settings_row.significance_pct,
        ingest_from=connection.ingest_from,
    )


async def sync(s: AsyncSession, user_id: uuid.UUID) -> IngestReport:
    """Один проход сверки по активному источнику.

    Окно начинается с ingest_from и никогда не уходит раньше: истории не
    импортируем, поэтому прошлое не может просочиться даже при полном перечитывании.
    """
    connection = await source_repo.active_connection(s, user_id)
    if connection is None:
        raise AppError(
            "no_active_source",
            "Источник сделок не подключён.",
            409,
        )

    source = adapter_for(s, connection)
    trades = await source.fetch_trades(since=connection.ingest_from)

    # Теги узнаём из самих сделок: словарь наполняется по мере торговли,
    # а что считать нарушением — решает трейдер на экране разметки.
    for item in trades:
        await source_repo.learn_tags(s, user_id, connection.id, item.tags)
    await s.flush()

    ctx = await build_context(s, user_id, connection)
    report = await trades_service.ingest_batch(s, ctx, trades)
    log.info(
        "сверка %s: получено %s, принято %s, переразмечено %s",
        connection.provider,
        report.received,
        report.inserted,
        report.remarked,
    )
    return report


def today_for(timezone: str, cutoff: dt.time, now: dt.datetime | None = None) -> dt.date:
    """Текущий торговый день пользователя. Тем же правилом, что и у сделки."""
    from eds.modules.trades.normalize import trading_day

    return trading_day(now or dt.datetime.now(dt.UTC), timezone, cutoff)
