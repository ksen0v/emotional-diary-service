"""Страница состояния и health отвечают даже когда базы нет.

Это требование, а не мелочь: если база не поднялась, страница обязана открыться
и сказать, что именно не готово. Иначе первое, что увидит человек, — пустой экран.
"""

import httpx
import pytest

from eds.entrypoints.api import app
from eds.version import STEP


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_shape(client) -> None:
    res = await client.get("/api/v1/health")
    assert res.status_code == 200
    body = res.json()

    assert body["service"] == "Emotional Diary Service"
    assert body["step"]["number"] == STEP
    for key in ("version", "env", "server_time", "secret_key_set", "db", "bus", "ok"):
        assert key in body, f"в health нет поля {key}"
    for key in ("connected", "revision", "schemas", "error"):
        assert key in body["db"]


async def test_status_page_opens(client) -> None:
    res = await client.get("/")
    assert res.status_code == 200
    assert "Emotional Diary Service" in res.text
    assert "text/html" in res.headers["content-type"]
