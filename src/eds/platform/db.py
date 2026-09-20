"""Подключение к базе. Одна база, схема на модуль (см. Архитектура ч.1 §6)."""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from eds.platform.config import settings

# Схемы модулей. Список один на весь проект: по нему строятся миграции
# и проверка состояния на странице /.
MODULE_SCHEMAS = (
    "identity",
    "source",
    "trades",
    "daybook",
    "rules",
    "incidents",
    "streaks",
    "notify",
    "events",
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings().database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(engine(), expire_on_commit=False)
    return _session_factory


async def session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI: сессия на запрос."""
    async with session_factory()() as s:
        yield s


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def db_state() -> dict:
    """Состояние базы для страницы состояния: связь, версия миграции, наличие схем."""
    state: dict = {"connected": False, "revision": None, "schemas": {}, "error": None}
    try:
        async with session_factory()() as s:
            await s.execute(text("SELECT 1"))
            state["connected"] = True

            rev = await s.execute(text("SELECT version_num FROM alembic_version"))
            row = rev.first()
            state["revision"] = row[0] if row else None

            found = await s.execute(
                text("SELECT schema_name FROM information_schema.schemata")
            )
            names = {r[0] for r in found}
            state["schemas"] = {name: (name in names) for name in MODULE_SCHEMAS}
    except Exception as exc:  # состояние, а не исключение: страница должна открыться и так
        state["error"] = f"{type(exc).__name__}: {exc}"
    return state
