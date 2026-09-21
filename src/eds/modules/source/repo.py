"""Доступ к таблицам source."""

import datetime as dt
import uuid

from sqlalchemy import delete as sql_delete
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.source import IncomingAccount, IncomingTag
from eds.modules.source.models import (
    Account,
    Connection,
    FakeFeedItem,
    RateLimitRow,
    ReconcileRun,
    Tag,
)


async def active_connection(s: AsyncSession, user_id: uuid.UUID) -> Connection | None:
    res = await s.execute(
        select(Connection).where(
            Connection.user_id == user_id, Connection.is_active.is_(True)
        )
    )
    return res.scalar_one_or_none()


async def connection_by_id(
    s: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID
) -> Connection | None:
    res = await s.execute(
        select(Connection).where(
            Connection.id == connection_id, Connection.user_id == user_id
        )
    )
    return res.scalar_one_or_none()


async def connections_of(s: AsyncSession, user_id: uuid.UUID) -> list[Connection]:
    res = await s.execute(
        select(Connection)
        .where(Connection.user_id == user_id)
        .order_by(Connection.created_at)
    )
    return list(res.scalars())


async def create_connection(
    s: AsyncSession,
    user_id: uuid.UUID,
    provider: str,
    capabilities: dict,
    market: str | None = None,
    key_masked: str | None = None,
    ingest_from: dt.datetime | None = None,
    key_encrypted: bytes | None = None,
    secret_encrypted: bytes | None = None,
    key_version: int = 1,
    base_url: str | None = None,
) -> Connection:
    now = dt.datetime.now(dt.UTC)
    row = Connection(
        id=uuid.uuid4(),
        user_id=user_id,
        provider=provider,
        market=market,
        key_encrypted=key_encrypted,
        secret_encrypted=secret_encrypted,
        key_version=key_version,
        key_masked=key_masked,
        auth_kind="api_key",
        is_active=False,
        activated_at=None,
        # Точка отсчёта: сделки, закрытые раньше, в сервис не попадают.
        # Истории не импортируем — решение Архитектуры ч.1 §4.3.
        ingest_from=ingest_from or now,
        state="connected",
        base_url=base_url,
        capabilities=capabilities,
        permissions=None,
        last_error=None,
        created_at=now,
    )
    s.add(row)
    await s.flush()
    return row


async def deactivate_all(s: AsyncSession, user_id: uuid.UUID) -> None:
    await s.execute(
        update(Connection)
        .where(Connection.user_id == user_id, Connection.is_active.is_(True))
        .values(is_active=False)
    )


async def activate(s: AsyncSession, connection: Connection) -> Connection:
    connection.is_active = True
    connection.activated_at = dt.datetime.now(dt.UTC)
    await s.flush()
    return connection


async def upsert_accounts(
    s: AsyncSession,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    accounts: list[IncomingAccount],
) -> list[Account]:
    now = dt.datetime.now(dt.UTC)
    for item in accounts:
        stmt = (
            pg_insert(Account)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                connection_id=connection_id,
                external_id=item.external_id,
                name=item.name,
                exchange=item.exchange,
                market=item.market,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=[Account.connection_id, Account.external_id],
                set_={"name": item.name},
            )
        )
        await s.execute(stmt)
    await s.flush()
    res = await s.execute(
        select(Account)
        .where(Account.connection_id == connection_id)
        .order_by(Account.created_at)
    )
    return list(res.scalars())


async def accounts_of(s: AsyncSession, connection_id: uuid.UUID) -> list[Account]:
    res = await s.execute(
        select(Account)
        .where(Account.connection_id == connection_id)
        .order_by(Account.created_at)
    )
    return list(res.scalars())


async def account_by_external(
    s: AsyncSession, connection_id: uuid.UUID, external_id: str
) -> Account | None:
    res = await s.execute(
        select(Account).where(
            Account.connection_id == connection_id, Account.external_id == external_id
        )
    )
    return res.scalar_one_or_none()


async def learn_tags(
    s: AsyncSession,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    tags: tuple[IncomingTag, ...] | list[IncomingTag],
) -> None:
    """Запомнить теги, которые встретились в сделке.

    is_violation остаётся false: что считать нарушением, решает трейдер
    на экране разметки. Сервис сам этого не решает никогда.
    """
    if not tags:
        return
    now = dt.datetime.now(dt.UTC)
    for tag in tags:
        stmt = (
            pg_insert(Tag)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                connection_id=connection_id,
                external_id=tag.external_id,
                column_key=tag.column_key,
                name=tag.name,
                is_violation=False,
                seen_at=now,
            )
            .on_conflict_do_update(
                index_elements=[Tag.connection_id, Tag.external_id],
                set_={"name": tag.name},
            )
        )
        await s.execute(stmt)


async def violation_tag_ids(s: AsyncSession, connection_id: uuid.UUID) -> set[str]:
    res = await s.execute(
        select(Tag.external_id).where(
            Tag.connection_id == connection_id, Tag.is_violation.is_(True)
        )
    )
    return {row[0] for row in res}


async def tags_of(s: AsyncSession, connection_id: uuid.UUID) -> list[Tag]:
    res = await s.execute(
        select(Tag).where(Tag.connection_id == connection_id).order_by(Tag.name)
    )
    return list(res.scalars())


async def add_fake_trade(
    s: AsyncSession, connection_id: uuid.UUID, payload: dict, close_time: dt.datetime
) -> FakeFeedItem:
    row = FakeFeedItem(
        connection_id=connection_id,
        payload=payload,
        close_time=close_time,
        created_at=dt.datetime.now(dt.UTC),
    )
    s.add(row)
    await s.flush()
    return row


async def fake_feed_size(s: AsyncSession, connection_id: uuid.UUID) -> int:
    res = await s.execute(
        select(FakeFeedItem.id).where(FakeFeedItem.connection_id == connection_id)
    )
    return len(list(res))


async def connection_by_provider(
    s: AsyncSession, user_id: uuid.UUID, provider: str, market: str | None
) -> Connection | None:
    res = await s.execute(
        select(Connection).where(
            Connection.user_id == user_id,
            Connection.provider == provider,
            Connection.market.is_(None) if market is None else Connection.market == market,
        )
    )
    return res.scalar_one_or_none()


async def delete_connection(s: AsyncSession, connection: Connection) -> None:
    """Удалить подключение. Сделки остаются: история неизменяема (ТЗ 9.2).

    Каскад уносит счета, словарь тегов и ленту фейка — всё, что принадлежит
    подключению. Сделки на него не ссылаются внешним ключом именно поэтому:
    между схемами модулей ключей нет, и удаление источника не трогает историю.
    """
    await s.execute(sql_delete(Connection).where(Connection.id == connection.id))
    await s.flush()


async def start_reconcile_run(
    s: AsyncSession,
    connection_id: uuid.UUID,
    *,
    kind: str,
    window_from: dt.datetime | None,
    window_to: dt.datetime | None,
) -> ReconcileRun:
    row = ReconcileRun(
        connection_id=connection_id,
        kind=kind,
        window_from=window_from,
        window_to=window_to,
        started_at=dt.datetime.now(dt.UTC),
        finished_at=None,
        status="running",
        trades_seen=0,
        trades_new=0,
        error=None,
    )
    s.add(row)
    await s.flush()
    return row


async def finish_reconcile_run(
    s: AsyncSession,
    run: ReconcileRun,
    *,
    status: str,
    trades_seen: int = 0,
    trades_new: int = 0,
    error: str | None = None,
) -> ReconcileRun:
    run.status = status
    run.trades_seen = trades_seen
    run.trades_new = trades_new
    run.error = error
    run.finished_at = dt.datetime.now(dt.UTC)
    await s.flush()
    return run


async def last_reconcile_run(
    s: AsyncSession, connection_id: uuid.UUID
) -> ReconcileRun | None:
    res = await s.execute(
        select(ReconcileRun)
        .where(ReconcileRun.connection_id == connection_id)
        .order_by(ReconcileRun.started_at.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def save_rate_limit(
    s: AsyncSession,
    connection_id: uuid.UUID,
    *,
    limit_value: int | None,
    remaining: int | None,
    reset_at: dt.datetime | None,
) -> None:
    """Сохранить снимок лимитов провайдера. Ничего не читаем — только пишем факт."""
    if limit_value is None and remaining is None and reset_at is None:
        return
    now = dt.datetime.now(dt.UTC)
    stmt = (
        pg_insert(RateLimitRow)
        .values(
            connection_id=connection_id,
            limit_value=limit_value,
            remaining=remaining,
            reset_at=reset_at,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[RateLimitRow.connection_id],
            set_={
                "limit_value": limit_value,
                "remaining": remaining,
                "reset_at": reset_at,
                "updated_at": now,
            },
        )
    )
    await s.execute(stmt)


async def rate_limit_of(
    s: AsyncSession, connection_id: uuid.UUID
) -> RateLimitRow | None:
    res = await s.execute(
        select(RateLimitRow).where(RateLimitRow.connection_id == connection_id)
    )
    return res.scalar_one_or_none()


async def set_state(
    s: AsyncSession, connection: Connection, state: str, error: str | None = None
) -> Connection:
    connection.state = state
    connection.last_error = error
    await s.flush()
    return connection


async def set_capabilities(
    s: AsyncSession, connection: Connection, capabilities: dict
) -> Connection:
    connection.capabilities = capabilities
    await s.flush()
    return connection
