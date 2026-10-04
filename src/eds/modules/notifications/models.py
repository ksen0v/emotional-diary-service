"""Таблицы модуля notifications. Схема notify, миграция 0011."""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, DateTime, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from eds.platform.orm import Base

SCHEMA = "notify"

# Состояния привязки.
PENDING = "pending"
LINKED = "linked"

# Состояния контакта. Двойное согласие (ТЗ 6.8): пока друг не подтвердил
# в боте, сигналы ему не идут и правило с условием «подтверждение» не создать.
CONFIRMED = "confirmed"

# Каналы. Экран блокировки каналом не считается: он не отправляется,
# а показывается, и выключить его нельзя.
SELF = "telegram_self"
BUDDY = "telegram_buddy"

# Состояния очереди. `skipped` — не ошибка: так помечается сообщение, которое
# не поехало по решению сервиса (режим наблюдения, выключенный Telegram,
# отсутствующий адресат). Хранить причину молча в `failed` нельзя: «не смогли»
# и «не стали» разбираются по-разному.
QUEUED = "queued"
SENT = "sent"
FAILED = "failed"
SKIPPED = "skipped"


class LinkRow(Base):
    __tablename__ = "telegram_links"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    link_code: Mapped[str | None] = mapped_column(Text)
    code_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(Text)
    linked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class ContactRow(Base):
    __tablename__ = "contacts"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    handle: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text)
    invite_code: Mapped[str | None] = mapped_column(Text)
    invited_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Сутки задержки из ТЗ 6.8: отключить того, кто может остановить, нельзя
    # сразу — именно этого хочется в тильте.
    removal_effective_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Персональный текст для конкретного друга. Перебивает общий `buddy_signal`:
    # двум разным людям пишут по-разному (Архитектура ч.1 §6).
    template: Mapped[str | None] = mapped_column(Text)


class TemplateRow(Base):
    __tablename__ = "templates"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    body: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class OutboundRow(Base):
    __tablename__ = "outbound"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    channel: Mapped[str] = mapped_column(Text)
    template: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Готовый текст, а не только данные: уведомление несёт то, что было
    # правдой в момент события, даже если шаблон переписали, пока оно лежало.
    body: Mapped[str] = mapped_column(Text)
    keyboard: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    dedup_key: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
