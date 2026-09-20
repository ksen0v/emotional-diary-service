import os

import pytest

# Тесты, которым нужна база, берут её из TEST_DATABASE_URL, иначе пропускаются.
TEST_DB = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def test_db_url() -> str:
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL не задан — тесты с базой пропущены")
    return TEST_DB
