"""Доступ к таблицам identity. Только свои таблицы, чужие схемы недоступны."""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.identity.models import ModuleFlag, Session, Settings, User


async def user_by_email(s: AsyncSession, email: str) -> User | None:
    res = await s.execute(select(User).where(User.email == email))
    return res.scalar_one_or_none()


async def user_by_id(s: AsyncSession, user_id: uuid.UUID) -> User | None:
    res = await s.execute(select(User).where(User.id == user_id))
    return res.scalar_one_or_none()


async def create_user(s: AsyncSession, email: str, password_hash: str) -> User:
    now = dt.datetime.now(dt.UTC)
    user = User(
        id=uuid.uuid4(), email=email, password_hash=password_hash, created_at=now
    )
    s.add(user)
    await s.flush()
    return user


async def create_default_settings(s: AsyncSession, user_id: uuid.UUID) -> Settings:
    now = dt.datetime.now(dt.UTC)
    row = Settings(
        user_id=user_id,
        timezone="Europe/Moscow",
        day_cutoff=dt.time(0, 0),
        pass_score=19,
        min_score=13,
        significance_pct=Decimal("0.50"),
        active_account_id=None,
        telegram_enabled=True,
        shadow_mode=False,
        updated_at=now,
    )
    s.add(row)
    await s.flush()
    return row


async def settings_of(s: AsyncSession, user_id: uuid.UUID) -> Settings | None:
    res = await s.execute(select(Settings).where(Settings.user_id == user_id))
    return res.scalar_one_or_none()


async def save_settings(s: AsyncSession, row: Settings, changes: dict) -> Settings:
    for field, value in changes.items():
        setattr(row, field, value)
    row.updated_at = dt.datetime.now(dt.UTC)
    await s.flush()
    return row


async def create_session(
    s: AsyncSession,
    user_id: uuid.UUID,
    token_hash: bytes,
    expires_at: dt.datetime,
    user_agent: str | None,
    ip: str | None,
) -> Session:
    now = dt.datetime.now(dt.UTC)
    row = Session(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=token_hash,
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
        user_agent=(user_agent or "")[:500] or None,
        ip=ip,
        revoked_at=None,
    )
    s.add(row)
    await s.flush()
    return row


async def live_session_by_hash(s: AsyncSession, token_hash: bytes) -> Session | None:
    now = dt.datetime.now(dt.UTC)
    res = await s.execute(
        select(Session).where(
            Session.token_hash == token_hash,
            Session.revoked_at.is_(None),
            Session.expires_at > now,
        )
    )
    return res.scalar_one_or_none()


async def touch_session(s: AsyncSession, session_id: uuid.UUID) -> None:
    """Обновляем last_seen не чаще раза в час, чтобы не писать на каждый запрос."""
    now = dt.datetime.now(dt.UTC)
    await s.execute(
        update(Session)
        .where(
            Session.id == session_id,
            Session.last_seen_at < now - dt.timedelta(hours=1),
        )
        .values(last_seen_at=now)
    )


async def revoke_session(s: AsyncSession, session_id: uuid.UUID) -> None:
    await s.execute(
        update(Session)
        .where(Session.id == session_id, Session.revoked_at.is_(None))
        .values(revoked_at=dt.datetime.now(dt.UTC))
    )


async def live_sessions_of(s: AsyncSession, user_id: uuid.UUID) -> list[Session]:
    now = dt.datetime.now(dt.UTC)
    res = await s.execute(
        select(Session)
        .where(
            Session.user_id == user_id,
            Session.revoked_at.is_(None),
            Session.expires_at > now,
        )
        .order_by(Session.last_seen_at.desc())
    )
    return list(res.scalars())


async def session_of_user(
    s: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> Session | None:
    res = await s.execute(
        select(Session).where(Session.id == session_id, Session.user_id == user_id)
    )
    return res.scalar_one_or_none()


async def disabled_modules(s: AsyncSession, user_id: uuid.UUID) -> list[str]:
    res = await s.execute(
        select(ModuleFlag.module).where(
            ModuleFlag.user_id == user_id, ModuleFlag.enabled.is_(False)
        )
    )
    return [row[0] for row in res]
