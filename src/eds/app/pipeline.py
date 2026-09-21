"""Сверка: забрать сделки у активного источника и отдать их приёмнику.

Слой оркестрации: он единственный имеет право знать сразу о нескольких модулях.
Сверка стоит здесь, потому что соединяет источник, настройки пользователя
и приём сделок, то есть не принадлежит ни одному модулю.
"""

import contextlib
import datetime as dt
import logging
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.ingest import IngestContext, IngestReport
from eds.contracts.source import TradeSource
from eds.modules.identity import repo as identity_repo
from eds.modules.source import repo as source_repo
from eds.modules.source.adapters.fake.source import FakeSource
from eds.modules.source.adapters.tmm.rest import TmmClient
from eds.modules.source.adapters.tmm.source import TmmSource
from eds.modules.source.models import Connection
from eds.modules.trades import service as trades_service
from eds.platform import crypto
from eds.platform.errors import AppError

log = logging.getLogger("eds.pipeline")


@contextlib.asynccontextmanager
async def source_for(
    s: AsyncSession, connection: Connection
) -> AsyncIterator[tuple[TradeSource, TmmClient | None]]:
    """Адаптер активного источника вместе с его сетевым клиентом.

    Контекст, а не функция: у сетевого источника есть соединение, которое надо
    закрыть, и забыть про это легко. Второе значение — клиент, если он есть:
    из него после прохода забираются прочитанные лимиты провайдера.
    Шаг 14 добавит сюда binance.
    """
    if connection.provider == "fake":
        yield FakeSource(s, connection.id), None
        return

    if connection.provider == "tmm":
        if not connection.key_encrypted:
            raise AppError(
                "key_missing",
                "У подключения не сохранён ключ. Подключи источник заново.",
                409,
            )
        key = crypto.decrypt(connection.key_encrypted)
        client = TmmClient(key)
        async with client:
            yield TmmSource(client), client
        return

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


async def sync(
    s: AsyncSession, user_id: uuid.UUID, *, kind: str = "manual"
) -> IngestReport:
    """Один проход сверки по активному источнику.

    Окно начинается с ingest_from и никогда не уходит раньше: истории не
    импортируем, поэтому прошлое не может просочиться даже при полном
    перечитывании. Приём идемпотентен, поэтому лишний проход ничего не ломает.
    """
    connection = await source_repo.active_connection(s, user_id)
    if connection is None:
        raise AppError("no_active_source", "Источник сделок не подключён.", 409)

    window_from = connection.ingest_from
    window_to = dt.datetime.now(dt.UTC)
    run = await source_repo.start_reconcile_run(
        s,
        connection.id,
        kind=kind,
        window_from=window_from,
        window_to=window_to,
    )

    try:
        report = await _run_sync(s, user_id, connection, window_from)
    except AppError as exc:
        status = "rate_limited" if exc.code == "rate_limited" else "error"
        await source_repo.finish_reconcile_run(
            s, run, status=status, error=exc.message
        )
        await source_repo.set_state(s, connection, "error", exc.message)
        raise

    await source_repo.finish_reconcile_run(
        s,
        run,
        status="ok",
        trades_seen=report.received,
        trades_new=report.inserted,
    )
    if connection.state != "connected":
        await source_repo.set_state(s, connection, "connected", None)

    log.info(
        "сверка %s: получено %s, принято %s, переразмечено %s",
        connection.provider,
        report.received,
        report.inserted,
        report.remarked,
    )
    return report


async def _run_sync(
    s: AsyncSession,
    user_id: uuid.UUID,
    connection: Connection,
    window_from: dt.datetime,
) -> IngestReport:
    async with source_for(s, connection) as (source, client):
        # Счета обновляем каждым проходом: трейдер мог добавить биржевой ключ
        # у провайдера уже после подключения, и без этого его сделки молча
        # отбрасывались бы как сделки неизвестного счёта.
        await source_repo.upsert_accounts(
            s, user_id, connection.id, await source.fetch_accounts()
        )

        # Словарь тегов приходит целиком, а не только из встретившихся сделок:
        # сразу после подключения сделок нет, а отметить «этот тег —
        # нарушение» трейдеру нужно уже тогда.
        await source_repo.learn_tags(
            s, user_id, connection.id, await source.fetch_tags()
        )

        trades = await source.fetch_trades(since=window_from, until=None)
        for item in trades:
            await source_repo.learn_tags(s, user_id, connection.id, item.tags)
        await s.flush()

        ctx = await build_context(s, user_id, connection)
        report = await trades_service.ingest_batch(s, ctx, trades)

        if client is not None:
            await source_repo.save_rate_limit(
                s,
                connection.id,
                limit_value=client.rate_limit.limit,
                remaining=client.rate_limit.remaining,
                reset_at=client.rate_limit.reset_at,
            )
    return report


def today_for(timezone: str, cutoff: dt.time, now: dt.datetime | None = None) -> dt.date:
    """Текущий торговый день пользователя. Тем же правилом, что и у сделки."""
    from eds.modules.trades.normalize import trading_day

    return trading_day(now or dt.datetime.now(dt.UTC), timezone, cutoff)
