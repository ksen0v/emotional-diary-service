"""Таблицы модуля source. Схема source, миграции 0003, 0004 и 0010."""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from eds.platform.orm import Base

SCHEMA = "source"


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(Text)
    market: Mapped[str | None] = mapped_column(Text)
    key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(SmallInteger)
    key_masked: Mapped[str | None] = mapped_column(Text)
    auth_kind: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ingest_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    capabilities: Mapped[dict] = mapped_column(JSONB)
    permissions: Mapped[dict | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.connections.id")
    )
    external_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    exchange: Mapped[str | None] = mapped_column(Text)
    market: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.connections.id")
    )
    external_id: Mapped[str] = mapped_column(Text)
    column_key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    is_violation: Mapped[bool] = mapped_column(Boolean)
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class ReconcileRun(Base):
    __tablename__ = "reconcile_runs"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.connections.id")
    )
    kind: Mapped[str] = mapped_column(Text)
    window_from: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    window_to: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    trades_seen: Mapped[int] = mapped_column(Integer)
    trades_new: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class RateLimitRow(Base):
    __tablename__ = "rate_limits"
    __table_args__ = {"schema": SCHEMA}

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.connections.id"), primary_key=True
    )
    limit_value: Mapped[int | None] = mapped_column(Integer)
    remaining: Mapped[int | None] = mapped_column(Integer)
    reset_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class FakeFeedItem(Base):
    __tablename__ = "fake_feed"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.connections.id")
    )
    payload: Mapped[dict] = mapped_column(JSONB)
    close_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


# --- только для источника Binance ---
#
# Готовых сделок биржа не отдаёт, поэтому у этого источника есть свой слой
# сырых данных: исполнения, начисления и снимки баланса. Сделки — производная
# от них, и пересобираются в любой момент (Архитектура ч.1 §5.5).


class Fill(Base):
    """Исполнение. Истина для агрегатора, поэтому хранится всегда и целиком."""

    __tablename__ = "fills"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.connections.id")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    external_id: Mapped[int] = mapped_column(BigInteger)
    order_id: Mapped[int] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(Text)
    position_side: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    qty: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    commission: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    commission_asset: Mapped[str] = mapped_column(Text)
    trade_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSONB)


class Income(Base):
    """Начисления: фандинг, комиссии, переводы. Фандинг попадает в результат сделки."""

    __tablename__ = "income"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.connections.id")
    )
    external_id: Mapped[int | None] = mapped_column(BigInteger)
    symbol: Mapped[str | None] = mapped_column(Text)
    income_type: Mapped[str] = mapped_column(Text)
    income: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    asset: Mapped[str] = mapped_column(Text)
    happened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class BalanceSnapshot(Base):
    """Баланс на момент времени — база для точных процентов от депозита.

    У TMM проценты восстанавливались из `profit_deposit`, каждый от своей базы.
    Здесь база настоящая, и ради этого снимки и хранятся.
    """

    __tablename__ = "balance_snapshots"
    __table_args__ = {"schema": SCHEMA}

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.connections.id"),
        primary_key=True,
    )
    taken_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    wallet_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    equity_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 10))


class AggregateState(Base):
    """Докуда агрегатор досчитал по каждой позиции.

    `last_fill_id` — последний филл последней ЗАКРЫТОЙ сделки. Всё, что после
    него, пересобирается заново на каждом проходе: так пропущенный и позже
    доехавший филл встаёт на своё место сам, без отдельной починки.
    """

    __tablename__ = "aggregate_state"
    __table_args__ = {"schema": SCHEMA}

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.connections.id"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    position_side: Mapped[str] = mapped_column(Text, primary_key=True)
    last_fill_id: Mapped[int] = mapped_column(BigInteger)
    open_position: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Position(Base):
    """Открытая позиция в реальном времени. Отсюда берётся нереализованный убыток."""

    __tablename__ = "positions"
    __table_args__ = {"schema": SCHEMA}

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.connections.id"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    position_side: Mapped[str] = mapped_column(Text, primary_key=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    unrealized_usd: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    unrealized_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    liquidation: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
