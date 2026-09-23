"""Таблицы модуля streaks. Схема streaks, миграция 0007."""

import datetime as dt
import uuid

from sqlalchemy import Boolean, Date, DateTime, SmallInteger, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from eds.platform.orm import Base

SCHEMA = "streaks"


class DayMarkRow(Base):
    __tablename__ = "day_marks"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    counted: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)


class StateRow(Base):
    __tablename__ = "state"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    current: Mapped[int] = mapped_column(SmallInteger)
    best: Mapped[int] = mapped_column(SmallInteger)
    last_day: Mapped[dt.date | None] = mapped_column(Date)
    freezes_month: Mapped[dt.date | None] = mapped_column(Date)
    freezes_used: Mapped[int] = mapped_column(SmallInteger)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
