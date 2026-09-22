"""Таблицы модуля daybook. Схема daybook, миграция 0005."""

import datetime as dt
import uuid

from sqlalchemy import Date, DateTime, ForeignKey, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from eds.platform.orm import Base

SCHEMA = "daybook"


class TradingDay(Base):
    __tablename__ = "trading_days"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    admission: Mapped[str | None] = mapped_column(Text)
    check_score: Mapped[int | None] = mapped_column(SmallInteger)
    session_opened_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    session_closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    review_state: Mapped[str] = mapped_column(Text)


class PremarketCheck(Base):
    __tablename__ = "premarket_checks"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    day: Mapped[dt.date] = mapped_column(Date)
    answers: Mapped[dict] = mapped_column(JSONB)
    score: Mapped[int] = mapped_column(SmallInteger)
    verdict: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    level: Mapped[str] = mapped_column(Text)
    period_start: Mapped[dt.date] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    score: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    editable_until: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class EntryTag(Base):
    __tablename__ = "entry_tags"
    __table_args__ = {"schema": SCHEMA}

    entry_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.entries.id"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(Text, primary_key=True)


class EntryComment(Base):
    __tablename__ = "entry_comments"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.entries.id")
    )
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class SessionReview(Base):
    __tablename__ = "session_reviews"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    day: Mapped[dt.date] = mapped_column(Date)
    plan_followed: Mapped[str] = mapped_column(Text)
    pull_text: Mapped[str | None] = mapped_column(Text)
    execution_score: Mapped[int | None] = mapped_column(SmallInteger)
    takeaway: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
