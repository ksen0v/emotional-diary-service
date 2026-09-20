import os
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DB = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def test_db_url() -> str:
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL не задан — тесты с базой пропущены")
    return TEST_DB


@pytest.fixture
async def factory(test_db_url: str):
    engine = create_async_engine(test_db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def app_client(test_db_url: str, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент к приложению, подключённому к тестовой базе.

    Приложение собирается на настоящем движке: подменять базу заглушкой смысла нет,
    потому что проверять надо в том числе ограничения СУБД.
    """
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("EDS_ENV", "test")

    from eds.platform import db
    from eds.platform.config import settings

    settings.cache_clear()
    await db.dispose()

    from eds.entrypoints.api import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await db.dispose()
    settings.cache_clear()


@pytest.fixture
async def clean_users(factory):
    """Удаляем тестовых пользователей до и после: почта уникальна."""

    async def wipe() -> None:
        async with factory() as s:
            await s.execute(
                text("DELETE FROM identity.users WHERE email LIKE '%@edstest.net'")
            )
            await s.commit()

    await wipe()
    yield
    await wipe()
