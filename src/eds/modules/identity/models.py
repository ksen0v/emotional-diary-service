"""Таблицы модуля identity. Схема identity, миграция 0002."""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from eds.platform.orm import Base

SCHEMA = "identity"


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class Settings(Base):
    __tablename__ = "settings"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.users.id"), primary_key=True
    )
    timezone: Mapped[str] = mapped_column(Text)
    day_cutoff: Mapped[dt.time] = mapped_column(Time)
    pass_score: Mapped[int] = mapped_column(SmallInteger)
    min_score: Mapped[int] = mapped_column(SmallInteger)
    significance_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    active_account_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    telegram_enabled: Mapped[bool] = mapped_column(Boolean)
    shadow_mode: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.users.id")
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AdminLogin(Base):
    """Одноразовый вход в админку через бота.

    Код живёт минуты и гаснет после первого использования. `user_id` пуст
    до того, как кто-то откроет ссылку: в момент выдачи сервис не знает,
    кто её откроет, и личность приходит из Telegram, а не из формы.
    """

    __tablename__ = "admin_logins"
    __table_args__ = {"schema": SCHEMA}

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.users.id")
    )
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ModuleFlag(Base):
    __tablename__ = "module_flags"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.users.id"), primary_key=True
    )
    module: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
