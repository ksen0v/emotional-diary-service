"""Таблицы модуля source. Схема source, миграция 0003."""

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    LargeBinary,
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
