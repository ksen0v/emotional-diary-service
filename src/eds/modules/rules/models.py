"""Таблицы модуля rules. Схема rules, миграции 0008 и 0009."""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    SmallInteger,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from eds.platform.orm import Base

SCHEMA = "rules"


class RuleRow(Base):
    __tablename__ = "rules"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    system_code: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    actions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    unlock: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class DayCounterRow(Base):
    """Состояние дня — кеш движка. Истина в сделках, здесь быстрый доступ."""

    __tablename__ = "day_counters"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    loss_streak: Mapped[int] = mapped_column(SmallInteger)
    equity_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    peak_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    loss_sum_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    unrealized_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    drawdown_full_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    significant_trades: Mapped[int] = mapped_column(SmallInteger)
    all_trades: Mapped[int] = mapped_column(SmallInteger)
    last_trade_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class EvaluationRow(Base):
    """Журнал проверок правила.

    Нужен трижды: идемпотентность по (rule_id, trigger_ref), снимок чисел для
    ответа на «почему сработало» и предыдущее состояние условия — правило
    срабатывает на переходе, а не на каждой сделке, пока условие выполняется.
    """

    __tablename__ = "evaluations"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    day: Mapped[dt.date] = mapped_column(Date)
    trigger_ref: Mapped[str] = mapped_column(Text)
    fired: Mapped[bool] = mapped_column(Boolean)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
