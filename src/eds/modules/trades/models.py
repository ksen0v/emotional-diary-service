"""Таблицы модуля trades. Схема trades, миграция 0003."""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from eds.platform.orm import Base

SCHEMA = "trades"


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    source: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    profit_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    percent: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    size_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    account_return_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    open_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    is_open: Mapped[bool] = mapped_column(Boolean)
    trading_day: Mapped[dt.date] = mapped_column(Date)
    is_significant: Mapped[bool] = mapped_column(Boolean)
    marking: Mapped[str] = mapped_column(Text)
    marked_by: Mapped[str] = mapped_column(Text)
    tags_hash: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class TradeTag(Base):
    __tablename__ = "trade_tags"
    __table_args__ = {"schema": SCHEMA}

    trade_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.trades.id"), primary_key=True
    )
    external_id: Mapped[str] = mapped_column(Text, primary_key=True)
    column_key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
