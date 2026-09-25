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
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import engine
from eds.contracts.ingest import IngestContext, IngestReport
from eds.contracts.source import TradeSource
from eds.modules.identity import repo as identity_repo
from eds.modules.source import repo as source_repo
from eds.modules.source.adapters.binance.rest import WEIGHT_LIMIT_1M, BinanceClient
from eds.modules.source.adapters.binance.source import BinanceSource
from eds.modules.source.adapters.fake.source import FakeSource
from eds.modules.source.adapters.tmm.rest import TmmClient
from eds.modules.source.adapters.tmm.source import TmmSource
from eds.modules.source.models import Connection
from eds.modules.trades import service as trades_service
from eds.platform import auth, crypto
from eds.platform.errors import AppError

log = logging.getLogger("eds.pipeline")


@contextlib.asynccontextmanager
async def source_for(
    s: AsyncSession, connection: Connection
) -> AsyncIterator[tuple[TradeSource, TmmClient | BinanceClient | None]]:
    """Адаптер активного источника вместе с его сетевым клиентом.

    Контекст, а не функция: у сетевого источника есть соединение, которое надо
    закрыть, и забыть про это легко. Второе значение — клиент, если он есть:
    из него после прохода забираются прочитанные лимиты провайдера.
    """
    if connection.provider == "fake":
        # Возможности фейка живут в подключении: он умеет изображать и TMM,
        # и источник без тегов с открытыми позициями.
        yield FakeSource(s, connection.id, connection.capabilities), None
        return

    if not connection.key_encrypted:
        raise AppError(
            "key_missing",
            "У подключения не сохранён ключ. Подключи источник заново.",
            409,
        )
    key = crypto.decrypt(connection.key_encrypted)

    if connection.provider == "tmm":
        client = TmmClient(key)
        async with client:
            yield TmmSource(client), client
        return

    if connection.provider == "binance":
        if not connection.secret_encrypted:
            raise AppError(
                "key_missing",
                "У подключения Binance не сохранён секрет. Подключи источник заново.",
                409,
            )
        secret = crypto.decrypt(connection.secret_encrypted)
        binance = BinanceClient(key, secret)
        async with binance:
            # Источнику нужна сессия: сделок биржа не отдаёт, он собирает их
            # из своих же сохранённых исполнений (Архитектура ч.1 §5.5).
            yield (
                BinanceSource(
                    binance,
                    s=s,
                    connection_id=connection.id,
                    user_id=connection.user_id,
                ),
                binance,
            )
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

    # Движок правил вызывается синхронно после батча (ТЗ 9.7): между приходом
    # сделки и блокировкой не должно быть очереди, которую кто-то не разобрал.
    prefs = await auth.prefs_of(s, user_id)
    engine_report = await engine.after_ingest(
        s, user_id, prefs, sorted(report.touched_days)
    )

    log.info(
        "сверка %s: получено %s, принято %s, переразмечено %s, сработало правил %s",
        connection.provider,
        report.received,
        report.inserted,
        report.remarked,
        engine_report.fired,
    )
    report.engine = engine_report.as_dict()
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

        if isinstance(client, TmmClient):
            await source_repo.save_rate_limit(
                s,
                connection.id,
                limit_value=client.rate_limit.limit,
                remaining=client.rate_limit.remaining,
                reset_at=client.rate_limit.reset_at,
            )
        elif isinstance(client, BinanceClient):
            # Binance считает не остаток запросов, а использованный вес на IP.
            # Кладём в ту же таблицу остаток, а не использованное: колонка
            # называется `remaining`, и класть в неё обратное по смыслу число
            # значило бы завести второй язык внутри одной таблицы.
            used = client.weight.used_1m
            await source_repo.save_rate_limit(
                s,
                connection.id,
                limit_value=WEIGHT_LIMIT_1M,
                remaining=None if used is None else WEIGHT_LIMIT_1M - used,
                reset_at=None,
            )
    return report


def today_for(timezone: str, cutoff: dt.time, now: dt.datetime | None = None) -> dt.date:
    """Текущий торговый день пользователя. Тем же правилом, что и у сделки."""
    from eds.modules.trades.normalize import trading_day

    return trading_day(now or dt.datetime.now(dt.UTC), timezone, cutoff)


async def refresh_positions(
    s: AsyncSession, user_id: uuid.UUID, *, now: dt.datetime | None = None
) -> dict:
    """Перечитать открытые позиции и досчитать по ним показатели дня.

    Существует ровно ради одного: просадка с учётом нереализованного — это
    момент тильта, когда трейдер сидит в минусе и ничего не закрывает. Если
    считать её только на закрытой сделке, правило сработает после того, как
    всё уже случилось.

    Источник без открытых позиций сюда не попадает и не должен: `null` в
    счётчиках означает «источник этого не даёт», а ноль означал бы «открытых
    позиций нет» (Архитектура ч.2 §3.5).
    """
    moment = now or dt.datetime.now(dt.UTC)
    connection = await source_repo.active_connection(s, user_id)
    if connection is None:
        raise AppError("no_active_source", "Источник сделок не подключён.", 409)
    if not connection.capabilities.get("provides_positions"):
        return {"available": False, "positions": 0, "unrealized_pct": None}

    async with source_for(s, connection) as (source, _client):
        positions = await source.fetch_positions()
        balance = await source.fetch_balance()

    if balance is not None:
        await source_repo.save_balance(
            s,
            connection.id,
            balance.taken_at,
            balance.wallet_usdt,
            balance.equity_usdt,
        )

    base = balance.wallet_usdt if balance is not None else None
    rows = []
    total = Decimal("0")
    for position in positions:
        total += position.unrealized_usd
        rows.append(
            {
                "symbol": position.symbol,
                "position_side": position.position_side,
                "qty": position.qty,
                "entry_price": position.entry_price,
                "mark_price": position.mark_price,
                "unrealized_usd": position.unrealized_usd,
                "unrealized_pct": (
                    position.unrealized_usd / base * 100
                    if base
                    else Decimal("0")
                ),
                "liquidation": position.liquidation,
            }
        )
    await source_repo.save_positions(s, connection.id, rows)

    # Без баланса процент не посчитать. Ноль здесь был бы хуже пустоты:
    # он читался бы как «открытых позиций нет».
    unrealized_pct = (total / base * 100) if base else None

    prefs = await auth.prefs_of(s, user_id)
    day = today_for(prefs.timezone, prefs.day_cutoff, moment)
    # Повод с точностью до минуты: обновление позиций приходит часто, а
    # срабатывание правила — событие, и журнал проверок отсекает повтор
    # внутри той же минуты.
    trigger = f"positions:{day.isoformat()}:{moment:%H:%M}"
    counters = await engine.run_day(
        s,
        user_id,
        prefs,
        day,
        now=moment,
        unrealized_pct=unrealized_pct,
        position_trigger=trigger,
    )
    return {
        "available": True,
        "positions": len(rows),
        "unrealized_pct": str(unrealized_pct) if unrealized_pct is not None else None,
        "drawdown_full_pct": (
            str(counters.drawdown_full_pct)
            if counters.drawdown_full_pct is not None
            else None
        ),
    }
