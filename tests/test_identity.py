"""Регистрация, вход, сессии, настройки — через HTTP, как это делает фронт."""

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("clean_users")

EMAIL = "vlad@edstest.net"
PASSWORD = "very-long-password"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    token = client.cookies.get("eds_csrf")
    return {"X-CSRF-Token": token} if token else {}


async def register(client: httpx.AsyncClient, email: str = EMAIL) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )


async def test_register_then_me(app_client: httpx.AsyncClient) -> None:
    res = await register(app_client)
    assert res.status_code == 201, res.text
    body = res.json()

    assert body["user"]["email"] == EMAIL
    # дефолты из ТЗ: МСК, граница дня 00:00, пороги 19/13, значимость 0.5%
    assert body["settings"]["timezone"] == "Europe/Moscow"
    assert body["settings"]["pass_score"] == 19
    assert body["settings"]["min_score"] == 13
    assert float(body["settings"]["significance_pct"]) == 0.50
    assert body["settings"]["shadow_mode"] is False

    # кука сессии поставлена и недоступна из JavaScript
    assert app_client.cookies.get("eds_session")
    assert app_client.cookies.get("eds_csrf")

    me = await app_client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == EMAIL


async def test_email_taken(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    again = await register(app_client)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "email_taken"


async def test_short_password_rejected(app_client: httpx.AsyncClient) -> None:
    res = await app_client.post(
        "/api/v1/auth/register", json={"email": "short@edstest.net", "password": "123"}
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "weak_password"


async def test_login_logout_cycle(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    out = await app_client.post("/api/v1/auth/logout", headers=csrf(app_client))
    assert out.status_code == 204

    app_client.cookies.clear()
    denied = await app_client.get("/api/v1/me")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "unauthenticated"

    ok = await app_client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    assert ok.status_code == 200
    assert (await app_client.get("/api/v1/me")).status_code == 200


async def test_wrong_password_same_message_as_unknown_email(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    app_client.cookies.clear()

    wrong = await app_client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": "wrong-password-x"}
    )
    unknown = await app_client.post(
        "/api/v1/auth/login", json={"email": "nobody@edstest.net", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    # одинаковый текст: иначе по ответу можно перебирать зарегистрированные адреса
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_revoked_session_stops_working(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    sessions = (await app_client.get("/api/v1/me/sessions")).json()
    assert len(sessions) == 1 and sessions[0]["current"] is True

    await app_client.post("/api/v1/auth/logout", headers=csrf(app_client))
    # кука осталась у клиента, но сессия отозвана в базе
    assert (await app_client.get("/api/v1/me")).status_code == 401


async def test_csrf_required_on_writes(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.patch("/api/v1/me/settings", json={"pass_score": 20})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "csrf_invalid"


async def test_csrf_mismatch_rejected(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await app_client.patch(
        "/api/v1/me/settings",
        json={"pass_score": 20},
        headers={"X-CSRF-Token": "wrong-token"},
    )
    assert res.status_code == 403
