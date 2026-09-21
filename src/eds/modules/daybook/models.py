"""Таблицы модуля daybook. Схема daybook, миграция 0005."""

import datetime as dt
import uuid

from sqlalchemy import Date, DateTime, SmallInteger, Text
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
