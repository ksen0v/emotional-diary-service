"""Настройки: сохранение, валидация, предупреждение про границу дня."""

import httpx
import pytest

from tests.test_identity import PASSWORD, csrf, register

pytestmark = pytest.mark.usefixtures("clean_users")


async def test_settings_persist(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.patch(
        "/api/v1/me/settings",
        json={"pass_score": 21, "min_score": 14, "significance_pct": "0.75"},
        headers=csrf(app_client),
    )
    assert res.status_code == 200, res.text
    assert res.json()["settings"]["pass_score"] == 21
    assert res.json()["notice"] is None

    # перезаход подтверждает, что сохранилось в базе, а не в памяти процесса
    app_client.cookies.clear()
    await app_client.post(
        "/api/v1/auth/login", json={"email": "vlad@edstest.net", "password": PASSWORD}
    )
    me = (await app_client.get("/api/v1/me")).json()
    assert me["settings"]["pass_score"] == 21
    assert me["settings"]["min_score"] == 14
    assert float(me["settings"]["significance_pct"]) == 0.75


async def test_timezone_change_returns_notice(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.patch(
        "/api/v1/me/settings",
        json={"timezone": "Asia/Almaty"},
        headers=csrf(app_client),
    )
    assert res.status_code == 200
    notice = res.json()["notice"]
    # предупреждение обязательно: история не пересчитывается, и об этом надо сказать
    assert notice and "следующего торгового дня" in notice


async def test_unknown_timezone_rejected(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.patch(
        "/api/v1/me/settings",
        json={"timezone": "Europe/Atlantis"},
        headers=csrf(app_client),
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "unknown_timezone"


async def test_pass_must_be_above_min(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.patch(
        "/api/v1/me/settings",
        json={"pass_score": 13, "min_score": 13},
        headers=csrf(app_client),
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "pass_not_above_min"


async def test_significance_out_of_range(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.patch(
        "/api/v1/me/settings",
        json={"significance_pct": "50"},
        headers=csrf(app_client),
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "value_out_of_range"


async def test_empty_patch_rejected(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.patch(
        "/api/v1/me/settings", json={}, headers=csrf(app_client)
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "validation_failed"


async def test_shadow_mode_toggles(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.patch(
        "/api/v1/me/settings", json={"shadow_mode": True}, headers=csrf(app_client)
    )
    assert res.status_code == 200
    assert res.json()["settings"]["shadow_mode"] is True
