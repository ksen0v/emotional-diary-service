"""Очередь уведомлений: кому не отправляем, почему и что делаем с отказом.

Два правила этого файла, и оба принципиальные.

**«Не смогли» и «не стали» — разные вещи.** Решение не отправлять принимается
один раз, в момент события, и записывается в очередь строкой `skipped`
с причиной. Иначе уведомление, не ушедшее из-за режима наблюдения, улетело бы
через час, когда режим выключили, — и трейдер получил бы алерт о том, чего
уже не помнит.

**Вид отказа решает, повторять ли.** «Друг заблокировал бота» повторять
бессмысленно ни сейчас, ни через час; «Telegram не ответил» — ровно наоборот.
"""

import base64
import datetime as dt
import os
import uuid

import httpx
import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import SendMessage

from eds.modules.identity import repo as identity_repo
from eds.modules.notifications import models as notify_models
from eds.modules.notifications import repo as notify_repo
from eds.modules.notifications import service as notify_service
from eds.modules.notifications.telegram.client import BotClient
from eds.platform import auth, db
from eds.platform.config import settings
from tests.test_identity import EMAIL, csrf, register
from tests.test_telegram import FakeApi

pytestmark = pytest.mark.usefixtures("clean_users")

# Токен бота шифруется тем же мастер-ключом, что ключи источников: пока мы
# не можем гарантировать, что он не утечёт из базы, он в ней не лежит открытым.
MASTER = base64.urlsafe_b64encode(b"N" * 32).decode()


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("EDS_SECRET_KEY", MASTER)
    settings.cache_clear()
    yield
    # Токен бота живёт в окружении процесса, а имя бота — в кеше модуля.
    # Без уборки установленный в одном тесте бот остался бы в следующем,
    # и проверки «бота нет» проходили бы, глядя на чужой токен.
    os.environ.pop("EDS_BOT_TOKEN", None)
    notify_service.forget_username()
    settings.cache_clear()

CHAT = 500100
BUDDY_CHAT = 500200


async def user_and_prefs():
    async with db.session_factory()() as s:
        user = await identity_repo.user_by_email(s, EMAIL)
        assert user is not None
        return user.id, await auth.prefs_of(s, user.id)


async def install_bot(username: str = "eds_test_bot") -> None:
    """Токен бота как в окружении сервиса, но без сети: getMe подделан.

    Имя бота кешируется в процессе, поэтому здесь же его и прогреваем:
    в рантайме это делает первый же запрос к Telegram.
    """
    os.environ["EDS_BOT_TOKEN"] = "123:ABC"
    settings.cache_clear()
    notify_service.forget_username()
    api = FakeApi()
    api.me = {"id": 42, "username": username, "first_name": "EDS"}
    await notify_service.bot_username(api=api)


async def link_chat(chat_id: int = CHAT) -> None:
    user_id, _ = await user_and_prefs()
    async with db.session_factory()() as s:
        data = await notify_service.start_link(s, user_id)
        await s.commit()
    async with db.session_factory()() as s:
        assert await notify_service.complete_link(s, data["code"], chat_id) == user_id
        await s.commit()


async def confirm_buddy(chat_id: int = BUDDY_CHAT) -> None:
    user_id, _ = await user_and_prefs()
    async with db.session_factory()() as s:
        contact = await notify_service.invite(s, user_id, "@maksim", "Максим")
        code = contact.invite_code
        await s.commit()
    async with db.session_factory()() as s:
        assert await notify_service.confirm_contact(s, code, chat_id) is not None
        await s.commit()


async def notify(key: str, values: dict, dedup: str | None = None):
    user_id, prefs = await user_and_prefs()
    async with db.session_factory()() as s:
        row = await notify_service.notify(
            s, user_id=user_id, key=key, values=values, prefs=prefs, dedup_key=dedup
        )
        await s.commit()
        return row


LOCK_VALUES = {
    "rule_name": "2 стопа подряд",
    "minutes": 30,
    "until": "15:12",
    "day": "19 сентября",
}


# --- кому не отправляем и почему ---


async def test_nothing_is_sent_without_a_bot(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    row = await notify("lock_started", LOCK_VALUES)
    assert row.state == notify_models.SKIPPED
    assert "токен бота" in row.error
    # Текст при этом собран и сохранён: в журнале видно, что именно
    # не ушло, — на этом и держится приёмка, пока бот не привязан.
    assert row.body == "Сработало: 2 стопа подряд. Блокировка до 15:12."


async def test_nothing_is_sent_without_a_link(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await install_bot()
    row = await notify("lock_started", LOCK_VALUES)
    assert row.state == notify_models.SKIPPED
    assert "не привязан" in row.error


async def test_shadow_mode_stops_notifications(app_client: httpx.AsyncClient) -> None:
    """Режим наблюдения: инциденты пишутся, уведомления не отправляются.

    Флаг читается ровно в двух местах — здесь и при применении блокировки
    (Архитектура ч.2 §5.10). Это и делает его удаление дешёвым.
    """
    await register(app_client)
    await install_bot()
    await link_chat()
    res = await app_client.patch(
        "/api/v1/me/settings", headers=csrf(app_client), json={"shadow_mode": True}
    )
    assert res.status_code == 200, res.text

    row = await notify("lock_started", LOCK_VALUES)
    assert row.state == notify_models.SKIPPED
    assert "наблюдения" in row.error


async def test_telegram_toggle_stops_notifications(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await install_bot()
    await link_chat()
    res = await app_client.patch(
        "/api/v1/me/settings",
        headers=csrf(app_client),
        json={"telegram_enabled": False},
    )
    assert res.status_code == 200, res.text

    row = await notify("lock_started", LOCK_VALUES)
    assert row.state == notify_models.SKIPPED
    assert "выключены" in row.error


async def test_buddy_signal_waits_for_consent(app_client: httpx.AsyncClient) -> None:
    """Двойное согласие (ТЗ 6.8): без подтверждения сигнал не уходит."""
    await register(app_client)
    await install_bot()
    await link_chat()
    user_id, _ = await user_and_prefs()
    async with db.session_factory()() as s:
        await notify_service.invite(s, user_id, "@maksim", "Максим")
        await s.commit()

    row = await notify(
        "buddy_signal",
        {"trader_name": "Владислав", "rule_name": "2 стопа", "day": "19 сентября"},
    )
    assert row.state == notify_models.SKIPPED
    assert "согласие" in row.error


async def test_ready_notification_is_queued(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await install_bot()
    await link_chat()
    row = await notify("lock_started", LOCK_VALUES)
    assert row.state == notify_models.QUEUED
    assert row.chat_id == CHAT


async def test_same_event_is_queued_once(app_client: httpx.AsyncClient) -> None:
    """Одно событие приходит и из потока, и из сверки — сообщение одно."""
    await register(app_client)
    await install_bot()
    await link_chat()
    first = await notify("lock_started", LOCK_VALUES, dedup="lock_started:abc")
    second = await notify("lock_started", LOCK_VALUES, dedup="lock_started:abc")
    assert first is not None
    assert second is None


async def test_personal_text_beats_the_common_one(app_client: httpx.AsyncClient) -> None:
    """Двум разным людям пишут по-разному (Архитектура ч.1 §6)."""
    await register(app_client)
    await install_bot()
    await link_chat()
    await confirm_buddy()
    user_id, _ = await user_and_prefs()
    async with db.session_factory()() as s:
        await notify_service.set_contact_template(
            s, user_id, "Позвони {trader_name}, он опять в тильте."
        )
        await s.commit()

    row = await notify(
        "buddy_signal",
        {"trader_name": "Владислав", "rule_name": "2 стопа", "day": "19 сентября"},
    )
    assert row.body == "Позвони Владислав, он опять в тильте."


async def test_personal_text_cannot_carry_money(app_client: httpx.AsyncClient) -> None:
    """Запрет ТЗ 9.5 действует и на персональный текст контакта.

    Иначе он обходится за секунду: общий шаблон проверен, а персональный —
    то же сообщение тому же человеку — нет.
    """
    from eds.platform.errors import AppError

    await register(app_client)
    user_id, _ = await user_and_prefs()
    async with db.session_factory()() as s:
        await notify_service.invite(s, user_id, "@maksim", "Максим")
        await s.commit()
    async with db.session_factory()() as s:
        with pytest.raises(AppError) as err:
            await notify_service.set_contact_template(
                s, user_id, "Он слил {profit_usd}"
            )
    assert err.value.code == "forbidden_placeholder"


# --- отправка ---


async def dispatch(api: FakeApi) -> dict:
    async with db.session_factory()() as s:
        counts = await notify_service.dispatch(
            s, BotClient("t", api=api), dt.datetime.now(dt.UTC)
        )
        await s.commit()
        return counts


async def only_row():
    user_id, _ = await user_and_prefs()
    async with db.session_factory()() as s:
        rows = await notify_repo.recent(s, user_id, limit=1)
        return rows[0]


async def test_queued_message_is_sent_once(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await install_bot()
    await link_chat()
    await notify("lock_started", LOCK_VALUES)

    api = FakeApi()
    assert (await dispatch(api))["sent"] == 1
    assert api.sent[0][0] == CHAT
    # Повторный проход очереди ничего не отправляет второй раз.
    assert (await dispatch(api))["sent"] == 0
    row = await only_row()
    assert row.state == notify_models.SENT
    assert row.sent_at is not None


class FailingApi(FakeApi):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def send_message(self, **kwargs):
        raise self.error


async def test_blocked_bot_is_not_retried(app_client: httpx.AsyncClient) -> None:
    """403 — отказ навсегда. Повторять бессмысленно ни сейчас, ни через час."""
    await register(app_client)
    await install_bot()
    await link_chat()
    await notify("lock_started", LOCK_VALUES)

    api = FailingApi(
        TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="blocked")
    )
    counts = await dispatch(api)
    assert counts["failed"] == 1
    row = await only_row()
    assert row.state == notify_models.FAILED
    assert row.attempts == 1


async def test_network_error_is_retried_with_a_pause(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    await install_bot()
    await link_chat()
    await notify("lock_started", LOCK_VALUES)

    api = FailingApi(TelegramServerError(method=SendMessage(chat_id=1, text="x"), message="502"))
    counts = await dispatch(api)
    assert counts["retry"] == 1
    row = await only_row()
    assert row.state == notify_models.QUEUED
    assert row.next_attempt_at > dt.datetime.now(dt.UTC)


async def test_retry_after_is_obeyed(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await install_bot()
    await link_chat()
    await notify("lock_started", LOCK_VALUES)

    api = FailingApi(
        TelegramRetryAfter(
            method=SendMessage(chat_id=1, text="x"), message="flood", retry_after=120
        )
    )
    await dispatch(api)
    row = await only_row()
    delay = (row.next_attempt_at - dt.datetime.now(dt.UTC)).total_seconds()
    assert 100 < delay <= 120


async def test_endless_retries_end(app_client: httpx.AsyncClient) -> None:
    """Повторы не вечны: иначе очередь превращается в вечный шум в логе."""
    await register(app_client)
    await install_bot()
    await link_chat()
    await notify("lock_started", LOCK_VALUES)

    api = FailingApi(TelegramServerError(method=SendMessage(chat_id=1, text="x"), message="502"))
    for attempt in range(1, notify_service.MAX_SEND_ATTEMPTS + 1):
        async with db.session_factory()() as s:
            # Часы двигаем накопительно: после каждой неудачи строка ждёт
            # своей паузы, и без этого второй проход её просто не увидит.
            await notify_service.dispatch(
                s,
                BotClient("t", api=api),
                dt.datetime.now(dt.UTC) + dt.timedelta(hours=attempt),
            )
            await s.commit()
    row = await only_row()
    assert row.state == notify_models.FAILED
    assert row.attempts == notify_service.MAX_SEND_ATTEMPTS


async def test_buddy_signals_are_counted(app_client: httpx.AsyncClient) -> None:
    """Счётчик сигналов за месяц (ТЗ 6.8): если их десятки, сигнал обесценился."""
    await register(app_client)
    await install_bot()
    await link_chat()
    await confirm_buddy()
    await notify(
        "buddy_signal",
        {"trader_name": "Владислав", "rule_name": "2 стопа", "day": "19 сентября"},
        dedup="buddy:1",
    )
    await dispatch(FakeApi())

    user_id, _ = await user_and_prefs()
    async with db.session_factory()() as s:
        count = await notify_service.signals_this_month(
            s, user_id, dt.datetime.now(dt.UTC)
        )
    assert count == 1


async def test_message_without_a_chat_is_not_lost(app_client: httpx.AsyncClient) -> None:
    """Строка без адресата не висит в очереди вечно, а становится неудачной."""
    await register(app_client)
    user_id, _ = await user_and_prefs()
    async with db.session_factory()() as s:
        await notify_repo.enqueue(
            s,
            user_id=user_id,
            channel=notify_models.SELF,
            template="lock_started",
            payload={},
            body="текст",
            chat_id=None,
            dedup_key=f"orphan:{uuid.uuid4()}",
        )
        await s.commit()

    counts = await dispatch(FakeApi())
    assert counts["failed"] == 1
    row = await only_row()
    assert row.state == notify_models.FAILED
    assert "некуда" in row.error
