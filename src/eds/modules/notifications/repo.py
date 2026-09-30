"""Доступ к таблицам notify."""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.notifications.models import (
    BUDDY,
    CONFIRMED,
    LINKED,
    QUEUED,
    SENT,
    BotRow,
    ContactRow,
    LinkRow,
    OutboundRow,
    TemplateRow,
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- бот ---


async def bot(s: AsyncSession) -> BotRow | None:
    res = await s.execute(select(BotRow).where(BotRow.id == 1))
    return res.scalar_one_or_none()


async def save_bot(
    s: AsyncSession,
    *,
    token_encrypted: bytes,
    key_version: int,
    username: str | None,
    bot_id: int | None,
    installed_by: uuid.UUID,
) -> BotRow:
    row = await bot(s)
    if row is None:
        row = BotRow(id=1)
        s.add(row)
    row.token_encrypted = token_encrypted
    row.key_version = key_version
    row.username = username
    row.bot_id = bot_id
    row.installed_by = installed_by
    row.updated_at = _now()
    await s.flush()
    return row


async def delete_bot(s: AsyncSession) -> None:
    await s.execute(sql_delete(BotRow).where(BotRow.id == 1))


# --- привязка ---


async def link_of(s: AsyncSession, user_id: uuid.UUID) -> LinkRow | None:
    res = await s.execute(select(LinkRow).where(LinkRow.user_id == user_id))
    return res.scalar_one_or_none()


async def link_by_code(s: AsyncSession, code: str) -> LinkRow | None:
    res = await s.execute(select(LinkRow).where(LinkRow.link_code == code))
    return res.scalar_one_or_none()


async def link_by_chat(s: AsyncSession, chat_id: int) -> LinkRow | None:
    res = await s.execute(
        select(LinkRow).where(LinkRow.chat_id == chat_id, LinkRow.state == LINKED)
    )
    return res.scalar_one_or_none()


async def save_link(s: AsyncSession, row: LinkRow) -> LinkRow:
    row.updated_at = _now()
    if row not in s:
        s.add(row)
    await s.flush()
    return row


async def delete_link(s: AsyncSession, user_id: uuid.UUID) -> None:
    await s.execute(sql_delete(LinkRow).where(LinkRow.user_id == user_id))


# --- доверенное лицо ---


async def contact_of(s: AsyncSession, user_id: uuid.UUID) -> ContactRow | None:
    res = await s.execute(select(ContactRow).where(ContactRow.user_id == user_id))
    return res.scalar_one_or_none()


async def contact_by_id(
    s: AsyncSession, user_id: uuid.UUID, contact_id: uuid.UUID
) -> ContactRow | None:
    res = await s.execute(
        select(ContactRow).where(
            ContactRow.id == contact_id, ContactRow.user_id == user_id
        )
    )
    return res.scalar_one_or_none()


async def contact_by_invite(s: AsyncSession, code: str) -> ContactRow | None:
    res = await s.execute(select(ContactRow).where(ContactRow.invite_code == code))
    return res.scalar_one_or_none()


async def contact_by_chat(s: AsyncSession, chat_id: int) -> ContactRow | None:
    res = await s.execute(
        select(ContactRow).where(
            ContactRow.chat_id == chat_id, ContactRow.status == CONFIRMED
        )
    )
    return res.scalars().first()


async def save_contact(s: AsyncSession, row: ContactRow) -> ContactRow:
    if row not in s:
        s.add(row)
    await s.flush()
    return row


async def delete_contact(s: AsyncSession, contact_id: uuid.UUID) -> None:
    await s.execute(sql_delete(ContactRow).where(ContactRow.id == contact_id))


async def due_removals(s: AsyncSession, now: dt.datetime) -> list[ContactRow]:
    """Контакты, у которых истекли сутки задержки (ТЗ 6.8)."""
    res = await s.execute(
        select(ContactRow).where(
            ContactRow.removal_effective_at.is_not(None),
            ContactRow.removal_effective_at <= now,
        )
    )
    return list(res.scalars())


# --- шаблоны ---


async def templates_of(s: AsyncSession, user_id: uuid.UUID) -> dict[str, TemplateRow]:
    res = await s.execute(select(TemplateRow).where(TemplateRow.user_id == user_id))
    return {row.key: row for row in res.scalars()}


async def template_of(
    s: AsyncSession, user_id: uuid.UUID, key: str
) -> TemplateRow | None:
    res = await s.execute(
        select(TemplateRow).where(
            TemplateRow.user_id == user_id, TemplateRow.key == key
        )
    )
    return res.scalar_one_or_none()


async def upsert_template(
    s: AsyncSession, user_id: uuid.UUID, key: str, body: str
) -> TemplateRow:
    row = await template_of(s, user_id, key)
    if row is None:
        row = TemplateRow(user_id=user_id, key=key)
        s.add(row)
    row.body = body
    row.updated_at = _now()
    await s.flush()
    return row


async def delete_template(s: AsyncSession, user_id: uuid.UUID, key: str) -> None:
    await s.execute(
        sql_delete(TemplateRow).where(
            TemplateRow.user_id == user_id, TemplateRow.key == key
        )
    )


# --- очередь ---


async def enqueue(
    s: AsyncSession,
    *,
    user_id: uuid.UUID,
    channel: str,
    template: str,
    payload: dict[str, Any],
    body: str,
    chat_id: int | None,
    keyboard: dict[str, Any] | None = None,
    dedup_key: str | None = None,
    state: str = QUEUED,
    error: str | None = None,
) -> OutboundRow | None:
    """Поставить сообщение в очередь. Повтор по `dedup_key` ничего не добавляет."""
    now = _now()
    values = {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "channel": channel,
        "template": template,
        "payload": payload,
        "body": body,
        "keyboard": keyboard,
        "chat_id": chat_id,
        "dedup_key": dedup_key,
        "state": state,
        "attempts": 0,
        "next_attempt_at": now,
        "created_at": now,
        "error": error,
    }
    stmt = (
        pg_insert(OutboundRow)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[OutboundRow.dedup_key])
        .returning(OutboundRow.id)
    )
    res = await s.execute(stmt)
    new_id = res.scalar_one_or_none()
    if new_id is None:
        return None
    return await by_id(s, new_id)


async def by_id(s: AsyncSession, row_id: uuid.UUID) -> OutboundRow | None:
    res = await s.execute(select(OutboundRow).where(OutboundRow.id == row_id))
    return res.scalar_one_or_none()


async def by_dedup(s: AsyncSession, dedup_key: str) -> OutboundRow | None:
    res = await s.execute(
        select(OutboundRow).where(OutboundRow.dedup_key == dedup_key)
    )
    return res.scalar_one_or_none()


async def due(s: AsyncSession, now: dt.datetime, limit: int = 20) -> list[OutboundRow]:
    res = await s.execute(
        select(OutboundRow)
        .where(OutboundRow.state == QUEUED, OutboundRow.next_attempt_at <= now)
        .order_by(OutboundRow.created_at)
        .limit(limit)
    )
    return list(res.scalars())


async def save_outbound(s: AsyncSession, row: OutboundRow) -> OutboundRow:
    await s.flush()
    return row


async def buddy_signals_since(
    s: AsyncSession, user_id: uuid.UUID, since: dt.datetime
) -> int:
    """Сколько сигналов другу ушло с начала месяца.

    Счётчик не декоративный (ТЗ 6.8): если сигналов десятки, сигнал обесценился,
    и лучше увидеть число, чем догадаться по молчанию друга.
    """
    res = await s.execute(
        select(func.count())
        .select_from(OutboundRow)
        .where(
            OutboundRow.user_id == user_id,
            OutboundRow.channel == BUDDY,
            OutboundRow.state == SENT,
            OutboundRow.sent_at >= since,
        )
    )
    return int(res.scalar_one())


async def recent(
    s: AsyncSession, user_id: uuid.UUID, limit: int = 20
) -> list[OutboundRow]:
    res = await s.execute(
        select(OutboundRow)
        .where(OutboundRow.user_id == user_id)
        .order_by(OutboundRow.created_at.desc())
        .limit(limit)
    )
    return list(res.scalars())
