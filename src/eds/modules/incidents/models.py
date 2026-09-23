"""Таблицы модуля incidents. Схема incidents, миграция 0009.

Инциденты и блокировки неизменяемы: меняются только поля состояния. Правило
уровня репозитория продублировано триггером в базе — история, которую можно
поправить, не история (ТЗ 9.2).
"""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from eds.platform.orm import Base

SCHEMA = "incidents"

# Исходы инцидента. Оспаривания нет: их ровно два (ТЗ 9.2).
OPEN = "open"
KEPT = "kept"
BREACHED = "breached"

# Состояния блокировки (Архитектура ч.1 §7).
ACTIVE = "active"
LIFTED = "lifted"
EXPIRED = "expired"

# Коды инцидентов. Здесь только тот, который умеет создавать шаг 9;
# violation, lock_breached и no_admission придут с системными триггерами.
CODE_RULE_FIRED = "rule_fired"


class IncidentRow(Base):
    __tablename__ = "incidents"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    day: Mapped[dt.date] = mapped_column(Date)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    code: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    shadow: Mapped[bool] = mapped_column(Boolean)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class LockRow(Base):
    __tablename__ = "locks"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    incident_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    day: Mapped[dt.date] = mapped_column(Date)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    # Имя и текст правила скопированы на момент срабатывания, а не взяты
    # ссылкой: правку правила история переписывать не должна.
    rule_name: Mapped[str] = mapped_column(Text)
    rule_text: Mapped[str] = mapped_column(Text)
    rule_version: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    timer_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Граница торгового дня — жёсткий предел поверх таймера. Ни одна
    # блокировка не переходит на следующий день (ТЗ 6.6).
    window_until: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    requires: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(Text)
    lifted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    lift_reason: Mapped[str | None] = mapped_column(Text)


class LockReviewRow(Base):
    __tablename__ = "lock_reviews"
    __table_args__ = {"schema": SCHEMA}

    lock_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    q1: Mapped[str] = mapped_column(Text)
    q2: Mapped[str] = mapped_column(Text)
    q3: Mapped[str] = mapped_column(Text)
    filled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
