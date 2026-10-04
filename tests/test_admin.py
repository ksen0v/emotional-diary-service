"""Админка: вход через бота, права, чистка и удаление.

Здесь проверяется то, что руками проверить дорого или страшно: что код входа
сам по себе ничего не значит, что чужой чат не пускает, что код одноразовый,
и что чистка уносит ровно выбранное, а не соседнее.

Сети нет: обновления подаются в обработчик бота такими, какими их присылает
Telegram, а токен сохраняется через поддельный клиент.
"""

import base64
import datetime as dt
import uuid

import httpx
import pytest

from eds.app import admin
from eds.contracts.streaks import DayMark
from eds.modules.daybook import repo as daybook_repo
from eds.modules.identity import repo as identity_repo
from eds.modules.notifications import repo as notify_repo
from eds.modules.notifications import service as notify_service
from eds.modules.rules import repo as rules_repo
from eds.modules.streaks import repo as streaks_repo
from eds.platform import db
from eds.platform.config import settings
from eds.platform.errors import AppError
from tests.test_bot_flow import feed, message
from tests.test_identity import EMAIL, PASSWORD, csrf, register
from tests.test_telegram import FakeApi

pytestmark = pytest.mark.usefixtures("clean_users")

MASTER = base64.urlsafe_b64encode(b"N" * 32).decode()

ADMIN_CHAT = 810100
STRANGER_CHAT = 810200
OTHER_EMAIL = "other@edstest.net"


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("EDS_SECRET_KEY", MASTER)
    settings.cache_clear()
    yield
    settings.cache_clear()


async def user_id_of(email: str = EMAIL) -> uuid.UUID:
    async with db.session_factory()() as s:
        user = await identity_repo.user_by_email(s, email)
        assert user is not None
        return user.id


async def install_bot(user_id: uuid.UUID) -> None:
    async with db.session_factory()() as s:
        await notify_service.save_token(s, user_id, "123:ABC", api=FakeApi())
        await s.commit()


async def link_chat(client: httpx.AsyncClient, chat_id: int) -> None:
    """Привязать Telegram тем же путём, которым это делает трейдер."""
    res = await client.post("/api/v1/notify/telegram/link", headers=csrf(client))
    assert res.status_code == 200, res.text
    await feed(message(chat_id, f"/start {res.json()['code']}"))


async def make_admin(email: str = EMAIL) -> None:
    async with db.session_factory()() as s:
        assert await identity_repo.set_admin(s, email) is not None
        await s.commit()


async def start_login() -> dict:
    async with db.session_factory()() as s:
        data = await admin.start_login(s)
        await s.commit()
        return data


async def test_login_needs_bot(app_client: httpx.AsyncClient) -> None:
    """Без бота ссылки нет и кода нет.

    Выдать код, который некуда отнести, значило бы предложить вход, которого
    не существует, — ровно то, что правило «экран не врёт» запрещает.
    """
    await register(app_client)
    await make_admin()
    with pytest.raises(AppError) as err:
        await start_login()
    assert err.value.code == "bot_not_installed"


async def test_code_alone_opens_nothing(app_client: httpx.AsyncClient) -> None:
    """Код выдан, но никто не подтвердил — входа нет.

    Главное свойство этой схемы: страница входа открыта всем, и получить код
    не значит получить доступ.
    """
    await register(app_client)
    await install_bot(await user_id_of())
    await make_admin()

    login = await start_login()
    assert login["bot_url"].startswith("https://t.me/eds_test_bot?start=A-")

    res = await app_client.get(f"/api/v1/admin/login/{login['code']}")
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "waiting"


async def test_stranger_chat_is_refused(app_client: httpx.AsyncClient) -> None:
    """Ссылку открыл чужой чат — не пускаем и не рассказываем почему."""
    await register(app_client)
    await install_bot(await user_id_of())
    await make_admin()
    login = await start_login()

    replies = await feed(message(STRANGER_CHAT, f"/start {login['code']}"))
    assert replies.last() == "Нет доступа."

    res = await app_client.get(f"/api/v1/admin/login/{login['code']}")
    assert res.json()["state"] == "waiting"


async def test_linked_but_not_admin_is_refused(app_client: httpx.AsyncClient) -> None:
    """Чат привязан, но признака админа нет.

    Текст отказа тот же, что у чужого чата: по ответу бота нельзя понять,
    существует ли админ и кто он.
    """
    await register(app_client)
    await install_bot(await user_id_of())
    await link_chat(app_client, ADMIN_CHAT)
    # Признак админа намеренно не ставим.
    login = await start_login()

    replies = await feed(message(ADMIN_CHAT, f"/start {login['code']}"))
    assert replies.last() == "Нет доступа."
    res = await app_client.get(f"/api/v1/admin/login/{login['code']}")
    assert res.json()["state"] == "waiting"


async def test_admin_login_is_single_use(app_client: httpx.AsyncClient) -> None:
    """Полный вход: ссылка из бота открывает сессию, и только один раз."""
    await register(app_client)
    await install_bot(await user_id_of())
    await link_chat(app_client, ADMIN_CHAT)
    await make_admin()
    login = await start_login()

    replies = await feed(message(ADMIN_CHAT, f"/start {login['code']}"))
    assert "подтверждён" in replies.last()

    res = await app_client.get(f"/api/v1/admin/login/{login['code']}")
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "ready"
    assert res.json()["email"] == EMAIL

    # Второй раз тем же кодом войти нельзя: код гаснет после выдачи сессии.
    again = await app_client.get(f"/api/v1/admin/login/{login['code']}")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_done"


async def test_expired_code_does_not_open(app_client: httpx.AsyncClient) -> None:
    """Просроченный код не пускает, даже если его подтвердили."""
    await register(app_client)
    await install_bot(await user_id_of())
    await link_chat(app_client, ADMIN_CHAT)
    await make_admin()
    login = await start_login()

    async with db.session_factory()() as s:
        row = await identity_repo.admin_login_by_code(s, login["code"])
        assert row is not None
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        await s.commit()

    replies = await feed(message(ADMIN_CHAT, f"/start {login['code']}"))
    assert "просрочен" in replies.last()
    res = await app_client.get(f"/api/v1/admin/login/{login['code']}")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "login_expired"


async def test_users_hidden_from_non_admin(app_client: httpx.AsyncClient) -> None:
    """Обычный вошедший пользователь админских ручек не видит.

    404, а не 403: по ответу не должно быть видно, что такие ручки есть.
    """
    await register(app_client)
    res = await app_client.get("/api/v1/admin/users")
    assert res.status_code == 404, res.text


async def test_users_list_counts(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await make_admin()
    res = await app_client.get("/api/v1/admin/users")
    assert res.status_code == 200, res.text
    rows = res.json()["users"]
    assert [row["email"] for row in rows] == [EMAIL]
    assert rows[0]["is_admin"] is True
    assert rows[0]["trades"] == 0
    # Системные правила заводятся не при регистрации, а при первом открытии
    # экрана правил. Счётчик это и показывает — сколько есть, а не сколько
    # должно быть.
    assert rows[0]["rules"] == 0

    assert (await app_client.get("/api/v1/rules")).status_code == 200
    again = await app_client.get("/api/v1/admin/users")
    assert again.json()["users"][0]["rules"] == 4


async def test_purge_takes_only_chosen(app_client: httpx.AsyncClient) -> None:
    """Чистка уносит выбранное и не трогает соседнее.

    Правила остаются сознательно: это то, что трейдер написал про себя,
    а не история торговли.
    """
    await register(app_client)
    await make_admin()
    user_id = await user_id_of()
    day = dt.date.today()

    # Системные правила заводятся при первом открытии экрана правил.
    assert (await app_client.get("/api/v1/rules")).status_code == 200

    entry = await app_client.put(
        f"/api/v1/entries/day/{day.isoformat()}",
        headers=csrf(app_client),
        json={"score": 4, "status": "Дисциплинированный", "tags": [], "body": "Ровно."},
    )
    assert entry.status_code == 200, entry.text
    async with db.session_factory()() as s:
        await streaks_repo.upsert_mark(
            s, user_id, DayMark(day=day, counted=True, reason="ok")
        )
        await s.commit()

    res = await app_client.post(
        f"/api/v1/admin/users/{user_id}/purge",
        headers=csrf(app_client),
        json={"parts": ["diary"]},
    )
    assert res.status_code == 200, res.text

    async with db.session_factory()() as s:
        assert await daybook_repo.count_entries(s, user_id) == 0
        # Стрик не просили — он на месте.
        assert len(await streaks_repo.marks_of(s, user_id, day)) == 1
        assert await rules_repo.count_rules(s, user_id) == 4


async def test_purge_rejects_unknown_part(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await make_admin()
    user_id = await user_id_of()
    res = await app_client.post(
        f"/api/v1/admin/users/{user_id}/purge",
        headers=csrf(app_client),
        json={"parts": ["everything"]},
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_failed"


async def test_admin_cannot_delete_himself(app_client: httpx.AsyncClient) -> None:
    """Снести себя из своей же админки — это потерять вход в неё."""
    await register(app_client)
    await make_admin()
    user_id = await user_id_of()
    res = await app_client.delete(
        f"/api/v1/admin/users/{user_id}", headers=csrf(app_client)
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"


async def test_delete_user_takes_everything(app_client: httpx.AsyncClient) -> None:
    """Удаление уносит пользователя и всё, что на нём висело."""
    await register(app_client)
    await make_admin()
    other = await register(app_client, OTHER_EMAIL)
    assert other.status_code == 201, other.text
    # Регистрация второго пользователя переключила куку сессии на него:
    # возвращаем вход админа, иначе удалять будет некому.
    victim_id = await user_id_of(OTHER_EMAIL)
    login = await app_client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text

    res = await app_client.delete(
        f"/api/v1/admin/users/{victim_id}", headers=csrf(app_client)
    )
    assert res.status_code == 200, res.text
    assert res.json()["deleted"] == OTHER_EMAIL

    async with db.session_factory()() as s:
        assert await identity_repo.user_by_email(s, OTHER_EMAIL) is None
        assert await rules_repo.count_rules(s, victim_id) == 0
        assert await notify_repo.link_of(s, victim_id) is None
