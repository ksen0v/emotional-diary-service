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


# Между схемами нет внешних ключей — это следствие границ модулей: модуль
# владеет своими таблицами, и чужая схема не может держать его за руку.
# Цена видна здесь: удаление пользователя приходится расписывать по схемам,
# и в сервисе «удалить аккаунт» тоже будет операцией уровня оркестрации.
WIPE = (
    "DELETE FROM trades.trade_tags WHERE trade_id IN ("
    " SELECT id FROM trades.trades WHERE user_id IN (SELECT id FROM test_users))",
    "DELETE FROM trades.trades WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM source.fake_feed WHERE connection_id IN ("
    " SELECT id FROM source.connections WHERE user_id IN (SELECT id FROM test_users))",
    "DELETE FROM source.tags WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM source.accounts WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM source.connections WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM rules.rules WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM streaks.day_marks WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM streaks.state WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM daybook.entry_tags WHERE entry_id IN ("
    " SELECT id FROM daybook.entries WHERE user_id IN (SELECT id FROM test_users))",
    "DELETE FROM daybook.entry_comments WHERE entry_id IN ("
    " SELECT id FROM daybook.entries WHERE user_id IN (SELECT id FROM test_users))",
    "DELETE FROM daybook.entries WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM daybook.session_reviews WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM daybook.premarket_checks WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM daybook.trading_days WHERE user_id IN (SELECT id FROM test_users)",
    "DELETE FROM identity.users WHERE email LIKE '%@edstest.net'",
)

WITH_TEST_USERS = (
    "WITH test_users AS ("
    " SELECT id FROM identity.users WHERE email LIKE '%@edstest.net') "
)


@pytest.fixture
async def clean_users(factory):
    """Чистим тестовых пользователей и всё, что к ним привязано, до и после."""

    async def wipe() -> None:
        async with factory() as s:
            for statement in WIPE:
                sql = statement
                if "test_users" in statement:
                    sql = WITH_TEST_USERS + statement
                await s.execute(text(sql))
            await s.commit()

    await wipe()
    yield
    await wipe()
