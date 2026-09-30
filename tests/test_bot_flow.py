"""Бот целиком: привязка, двойное согласие, подтверждение снятия.

Это проверка из плана разработки для этого шага дословно: «пришёл алерт
в Telegram; переписал текст — пришёл твой текст; друг подтвердил — блокировка
снялась». Сети здесь нет: обновления подаются в обработчик такими, какими их
присылает Telegram, а отправка идёт в поддельный клиент.

Отдельно проверяется то, что руками не проверишь: повтор того же обновления
(Telegram присылает его заново, пока не подтвердили), пауза между просьбами
и запрет отключать доверенное лицо во время инцидента.
"""

import base64
import datetime as dt
import uuid

import httpx
import pytest

from eds.app import bot as app_bot
from eds.modules.identity import repo as identity_repo
from eds.modules.incidents import repo as incidents_repo
from eds.modules.notifications import models as notify_models
from eds.modules.notifications import repo as notify_repo
from eds.modules.notifications import service as notify_service
from eds.modules.notifications.telegram.client import BotClient
from eds.platform import db
from eds.platform.config import settings
from tests.test_identity import EMAIL, csrf, move_day_boundary_away, register
from tests.test_lock_flow import TWO_STOPS, setup, two_stops
from tests.test_premarket import BEST
from tests.test_telegram import FakeApi
from tests.test_trades_flow import consumer_named, drain

pytestmark = pytest.mark.usefixtures("clean_users")

MASTER = base64.urlsafe_b64encode(b"N" * 32).decode()

TRADER_CHAT = 700100
BUDDY_CHAT = 700200


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("EDS_SECRET_KEY", MASTER)
    settings.cache_clear()
    yield
    settings.cache_clear()


class Replies:
    """Ответы бота в чат. Настоящей отправки в тестах нет."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def last(self) -> str:
        return self.sent[-1][1] if self.sent else ""


async def user_id_of() -> uuid.UUID:
    async with db.session_factory()() as s:
        user = await identity_repo.user_by_email(s, EMAIL)
        assert user is not None
        return user.id


async def feed(update: dict) -> Replies:
    """Одно обновление от Telegram — как его получит бот."""
    replies = Replies()
    async with db.session_factory()() as s:
        await app_bot.handle_update(s, update, replies)
        await s.commit()
    return replies


def message(chat_id: int, text: str, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


def button(chat_id: int, data: str, update_id: int = 2) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"chat": {"id": chat_id}},
        },
    }


async def install_bot(client: httpx.AsyncClient) -> None:
    async with db.session_factory()() as s:
        await notify_service.save_token(s, await user_id_of(), "123:ABC", api=FakeApi())
        await s.commit()


async def link_code(client: httpx.AsyncClient) -> str:
    res = await client.post("/api/v1/notify/telegram/link", headers=csrf(client))
    assert res.status_code == 200, res.text
    return res.json()["code"]


async def invite_code(client: httpx.AsyncClient) -> str:
    res = await client.post(
        "/api/v1/notify/contacts",
        headers=csrf(client),
        json={"handle": "@maksim", "display_name": "Максим"},
    )
    assert res.status_code == 201, res.text
    body = res.json()["contact"]
    assert body["status"] == "pending"
    # Приглашение — ссылка, которую трейдер пересылает сам: бот не может
    # написать первым тому, кто его не запускал.
    assert body["invite_url"].startswith("https://t.me/eds_test_bot?start=B-")
    return body["invite_url"].split("start=")[1]


async def deliver(client: httpx.AsyncClient) -> FakeApi:
    """Догнать шину и разослать очередь.

    В тестах консьюмеры и цикл отправки не крутятся сами: `app_client`
    поднимает приложение без фоновых задач. Поэтому и шина, и очередь
    прогоняются руками — теми же самыми, что работают в процессе.
    """
    await drain(consumer_named("notify"))
    api = FakeApi()
    async with db.session_factory()() as s:
        await notify_service.dispatch(s, BotClient("t", api=api), dt.datetime.now(dt.UTC))
        await s.commit()
    return api


async def notify_settings(client: httpx.AsyncClient) -> dict:
    res = await client.get("/api/v1/notify/settings")
    assert res.status_code == 200, res.text
    return res.json()


# --- привязка ---


async def test_start_with_code_links_the_account(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await install_bot(app_client)
    code = await link_code(app_client)

    replies = await feed(message(TRADER_CHAT, f"/start {code}"))
    assert "привязан" in replies.last()

    state = await notify_settings(app_client)
    assert state["telegram"]["state"] == "linked"
    assert "уходят в Telegram" in state["notes"]["state"]


async def test_the_same_update_twice_changes_nothing(
    app_client: httpx.AsyncClient,
) -> None:
    """Telegram присылает обновление заново, пока его не подтвердили.

    Поэтому обработчик обязан быть идемпотентным: код гасится первой же
    привязкой, и повтор получает честный отказ, а не вторую привязку.
    """
    await register(app_client)
    await install_bot(app_client)
    code = await link_code(app_client)

    await feed(message(TRADER_CHAT, f"/start {code}"))
    replies = await feed(message(TRADER_CHAT, f"/start {code}"))
    assert "Код не подошёл" in replies.last()
    assert (await notify_settings(app_client))["telegram"]["state"] == "linked"


async def test_expired_code_is_refused(app_client: httpx.AsyncClient) -> None:
    """Код живёт недолго: он же уходит в ссылку, а ссылка попадает куда угодно."""
    await register(app_client)
    await install_bot(app_client)
    code = await link_code(app_client)
    async with db.session_factory()() as s:
        row = await notify_repo.link_of(s, await user_id_of())
        row.code_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        await notify_repo.save_link(s, row)
        await s.commit()

    replies = await feed(message(TRADER_CHAT, f"/start {code}"))
    assert "Код не подошёл" in replies.last()


async def test_stranger_gets_an_explanation(app_client: httpx.AsyncClient) -> None:
    """Бот с командами, которых никто не просил, — лишняя поверхность.

    На всё остальное он отвечает одним объяснением, что это за бот и как
    его привязать.
    """
    await register(app_client)
    await install_bot(app_client)
    replies = await feed(message(999999, "привет"))
    assert "бот сервиса дисциплины" in replies.last()


# --- двойное согласие ---


async def test_buddy_consent_is_double_opt_in(app_client: httpx.AsyncClient) -> None:
    """Друг подтверждает согласие сам, в боте (ТЗ 6.8).

    До подтверждения ему ничего не отправляется, и правило с сигналом ему
    создать нельзя.
    """
    await register(app_client)
    await install_bot(app_client)
    code = await invite_code(app_client)

    metrics = (await app_client.get("/api/v1/rules/metrics")).json()
    assert metrics["buddy_available"] is False

    replies = await feed(message(BUDDY_CHAT, f"/start {code}"))
    assert "Согласие записано" in replies.last()
    # Друг сразу узнаёт, что именно ему будет приходить: факт, а не финансы.
    assert "без сумм" in replies.last()

    contact = (await notify_settings(app_client))["contact"]
    assert contact["status"] == "confirmed"
    metrics = (await app_client.get("/api/v1/rules/metrics")).json()
    assert metrics["buddy_available"] is True
    assert "Максим" in metrics["buddy_note"]


async def test_rule_with_a_signal_appears_only_after_consent(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    await install_bot(app_client)
    await app_client.post("/api/v1/source/connections/fake", headers=csrf(app_client))

    with_buddy = {**TWO_STOPS, "actions": {**TWO_STOPS["actions"], "buddy": True}}
    res = await app_client.post("/api/v1/rules", headers=csrf(app_client), json=with_buddy)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "no_confirmed_contact"

    code = await invite_code(app_client)
    await feed(message(BUDDY_CHAT, f"/start {code}"))

    res = await app_client.post("/api/v1/rules", headers=csrf(app_client), json=with_buddy)
    assert res.status_code == 201, res.text


# --- отключение контакта ---


async def test_contact_removal_waits_a_day(app_client: httpx.AsyncClient) -> None:
    """Сутки задержки из ТЗ 6.8: в тильте первым желанием будет отключить того,
    кто может остановить. Отмена при этом мгновенная — она работает в нужную
    сторону."""
    await register(app_client)
    await install_bot(app_client)
    code = await invite_code(app_client)
    await feed(message(BUDDY_CHAT, f"/start {code}"))
    contact_id = (await notify_settings(app_client))["contact"]["id"]

    res = await app_client.delete(
        f"/api/v1/notify/contacts/{contact_id}", headers=csrf(app_client)
    )
    assert res.status_code == 202, res.text
    effective = res.json()["contact"]["removal_effective_at"]
    assert effective is not None
    # До этого времени сигналы продолжают уходить — как написано в прототипе.
    assert (await notify_settings(app_client))["contact"]["status"] == "confirmed"

    res = await app_client.delete(
        f"/api/v1/notify/contacts/{contact_id}/removal", headers=csrf(app_client)
    )
    assert res.status_code == 200, res.text
    assert res.json()["contact"]["removal_effective_at"] is None


async def test_due_removal_is_applied(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await install_bot(app_client)
    code = await invite_code(app_client)
    await feed(message(BUDDY_CHAT, f"/start {code}"))
    contact_id = (await notify_settings(app_client))["contact"]["id"]
    await app_client.delete(
        f"/api/v1/notify/contacts/{contact_id}", headers=csrf(app_client)
    )

    async with db.session_factory()() as s:
        removed = await notify_service.apply_due_removals(
            s, dt.datetime.now(dt.UTC) + dt.timedelta(hours=25)
        )
        await s.commit()
    assert removed == 1
    assert (await notify_settings(app_client))["contact"] is None


# --- подтверждение снятия ---


BUDDY_RULE = {
    **TWO_STOPS,
    "name": "2 стопа подряд",
    "unlock": {"timer": False, "review": False, "buddy": True},
}


async def lock_with_buddy(client: httpx.AsyncClient) -> dict:
    """Блокировка, которую снимает только подтверждение друга.

    Порядок здесь не случайный: правило с условием «подтверждение друга»
    не сохраняется, пока согласие не получено (ТЗ 6.8), поэтому сначала
    контакт, потом правило.
    """
    await register(client)
    await move_day_boundary_away(client)
    res = await client.post("/api/v1/source/connections/fake", headers=csrf(client))
    assert res.status_code == 200, res.text
    res = await client.post(
        "/api/v1/premarket/checks", headers=csrf(client), json={"answers": BEST}
    )
    assert res.status_code == 200, res.text

    await install_bot(client)
    code = await invite_code(client)
    await feed(message(BUDDY_CHAT, f"/start {code}"))
    res = await client.post("/api/v1/rules", headers=csrf(client), json=BUDDY_RULE)
    assert res.status_code == 201, res.text
    await two_stops(client)
    res = await client.get("/api/v1/locks/active")
    assert res.status_code == 200, res.text
    lock = res.json()["lock"]
    assert lock is not None, "две убыточные сделки должны были включить блокировку"
    return lock


async def test_buddy_confirmation_lifts_the_lock(
    app_client: httpx.AsyncClient,
) -> None:
    """Проверка из плана: друг подтвердил — блокировка снялась."""
    lock = await lock_with_buddy(app_client)
    assert lock["buddy"]["state"] == "idle"
    assert lock["buddy"]["display_name"] == "Максим"
    # Худший сценарий назван заранее: фолбэка нет (ТЗ 6.6).
    assert "границе дня" in lock["buddy"]["fallback_text"]

    res = await app_client.post(
        f"/api/v1/locks/{lock['id']}/request-buddy", headers=csrf(app_client)
    )
    assert res.status_code == 200, res.text
    assert res.json()["buddy"]["state"] == "waiting"

    replies = await feed(button(BUDDY_CHAT, f"lift:{lock['id']}"))
    assert "Подтверждение принято" in replies.last()

    res = await app_client.get("/api/v1/locks/active")
    assert res.json()["lock"] is None, "после подтверждения блокировки быть не должно"


async def test_request_has_a_cooldown(app_client: httpx.AsyncClient) -> None:
    """Друга нельзя завалить просьбами в тильте (Архитектура ч.2 §3.7)."""
    lock = await lock_with_buddy(app_client)

    first = await app_client.post(
        f"/api/v1/locks/{lock['id']}/request-buddy", headers=csrf(app_client)
    )
    assert first.status_code == 200, first.text
    second = await app_client.post(
        f"/api/v1/locks/{lock['id']}/request-buddy", headers=csrf(app_client)
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "too_many_attempts"


async def test_confirmation_button_belongs_to_its_lock(
    app_client: httpx.AsyncClient,
) -> None:
    """Нажатие относится к конкретной блокировке, а не к «последней».

    И приходит оно от конкретного чата: кнопка, переславшая кому-то ещё,
    снятия не даёт.
    """
    lock = await lock_with_buddy(app_client)

    replies = await feed(button(999999, f"lift:{lock['id']}"))
    assert "не для тебя" in replies.last()
    assert (await app_client.get("/api/v1/locks/active")).json()["lock"] is not None

    replies = await feed(button(BUDDY_CHAT, f"lift:{uuid.uuid4()}"))
    assert "не для тебя" in replies.last()
    assert (await app_client.get("/api/v1/locks/active")).json()["lock"] is not None


async def test_second_press_is_harmless(app_client: httpx.AsyncClient) -> None:
    """Друг нажал дважды — подтверждение одно, и бот говорит об этом спокойно."""
    lock = await lock_with_buddy(app_client)

    await feed(button(BUDDY_CHAT, f"lift:{lock['id']}"))
    replies = await feed(button(BUDDY_CHAT, f"lift:{lock['id']}", update_id=3))
    assert "уже закончилась" in replies.last()

    async with db.session_factory()() as s:
        confirmation = await incidents_repo.confirmation_of(
            s, uuid.UUID(lock["id"])
        )
    assert confirmation is not None


async def test_contact_cannot_be_removed_during_an_incident(
    app_client: httpx.AsyncClient,
) -> None:
    """Отключить того, кто может остановить, нельзя посреди инцидента (ТЗ 6.8)."""
    await lock_with_buddy(app_client)
    contact_id = (await notify_settings(app_client))["contact"]["id"]

    res = await app_client.delete(
        f"/api/v1/notify/contacts/{contact_id}", headers=csrf(app_client)
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "lock_active"


# --- текст, который переписал трейдер ---


async def test_trader_text_is_what_arrives(app_client: httpx.AsyncClient) -> None:
    """Проверка из плана: переписал текст — пришёл твой текст.

    Ни отката на дефолт, ни правок: что написано, то и придёт (ТЗ 9.5).
    """
    await setup(app_client)
    await install_bot(app_client)
    code = await link_code(app_client)
    await feed(message(TRADER_CHAT, f"/start {code}"))

    res = await app_client.put(
        "/api/v1/notify/templates/lock_started",
        headers=csrf(app_client),
        json={"body": "Два стопа. Хватит. До {until}."},
    )
    assert res.status_code == 200, res.text
    assert res.json()["template"]["is_customized"] is True

    await two_stops(app_client)

    api = await deliver(app_client)
    texts = [text for _chat, text, _kb in api.sent]
    assert any(t.startswith("Два стопа. Хватит. До ") for t in texts), texts
    assert all("Сработало:" not in t for t in texts)


async def test_default_text_returns(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await app_client.put(
        "/api/v1/notify/templates/lock_started",
        headers=csrf(app_client),
        json={"body": "Хватит."},
    )
    res = await app_client.delete(
        "/api/v1/notify/templates/lock_started", headers=csrf(app_client)
    )
    assert res.status_code == 200, res.text
    body = res.json()["template"]
    assert body["is_customized"] is False
    assert body["body"] == "Сработало: {rule_name}. Блокировка до {until}."


async def test_preview_renders_on_the_server(app_client: httpx.AsyncClient) -> None:
    """Предпросмотр считает сервер: иначе трейдер увидит свой текст первый раз
    в момент блокировки, то есть в худший момент для правок."""
    await register(app_client)
    res = await app_client.post(
        "/api/v1/notify/templates/lock_started/preview",
        headers=csrf(app_client),
        json={"body": "Два стопа. Хватит. До {until}."},
    )
    assert res.status_code == 200, res.text
    assert res.json()["rendered"] == "Два стопа. Хватит. До 15:12."


async def test_alert_reaches_telegram(app_client: httpx.AsyncClient) -> None:
    """Проверка из плана: пришёл алерт в Telegram.

    Путь целиком: сделки → правило → инцидент → шина → очередь → бот.
    """
    await setup(app_client)
    await install_bot(app_client)
    code = await link_code(app_client)
    await feed(message(TRADER_CHAT, f"/start {code}"))

    await two_stops(app_client)
    api = await deliver(app_client)

    user_id = await user_id_of()
    async with db.session_factory()() as s:
        queued = await notify_repo.recent(s, user_id, limit=10)
    started = [r for r in queued if r.template == "lock_started"]
    assert started, [r.template for r in queued]
    assert started[0].state == notify_models.SENT
    assert started[0].chat_id == TRADER_CHAT
    assert any("Блокировка до" in text for _chat, text, _kb in api.sent)


# --- красная полоса нарушения ---


async def breach_the_lock(client: httpx.AsyncClient) -> dict:
    """Сделка во время блокировки — сценарий В из дизайна."""
    import asyncio

    from tests.test_trades_flow import push, sync

    await two_stops(client)
    # Блокировка началась мгновение назад: ждём, чтобы время открытия
    # следующей сделки заведомо попало внутрь её окна.
    await asyncio.sleep(1.5)
    await push(
        client, profit="12.00", pct="0.03", minutes_ago=0, duration_sec=1, symbol="XRPUSDT"
    )
    report = await sync(client)
    assert report["engine"]["breaches"] == 1
    res = await client.get("/api/v1/today")
    assert res.status_code == 200, res.text
    return res.json()["lock"]


async def test_banner_mentions_the_signal_only_when_it_was_sent(
    app_client: httpx.AsyncClient,
) -> None:
    """Третья фраза полосы — «Максиму отправлен сигнал» — правда или её нет.

    В прототипе она стоит всегда. Но сигнал уходит не всегда: контакт мог
    не подтвердить согласие, трейдер мог выключить сигнал у SR-2, мог быть
    включён режим наблюдения. Обещанная отправка, которой не было, — это
    та же ложь, что заглушка, только незаметнее.
    """
    await setup(app_client)
    await install_bot(app_client)
    lock = await breach_the_lock(app_client)
    await drain(consumer_named("notify"))

    assert lock["breach"] is not None
    # Стрик у нового пользователя нулевой, поэтому фраза про него короткая —
    # но она есть, а вот про сигнал не должно быть ни слова.
    assert "не зачтётся" in lock["breach"]["streak_text"]
    assert "сигнал" not in lock["breach"]["streak_text"], "контакта нет — сигнала не было"


async def test_banner_names_the_contact_when_the_signal_went_out(
    app_client: httpx.AsyncClient,
) -> None:
    await setup(app_client)
    await install_bot(app_client)
    code = await invite_code(app_client)
    await feed(message(BUDDY_CHAT, f"/start {code}"))

    await breach_the_lock(app_client)
    # Сигнал ставится в очередь консьюмером шины, как в рабочем процессе.
    await drain(consumer_named("notify"))

    res = await app_client.get("/api/v1/today")
    lock = res.json()["lock"]
    assert "Максим получил сигнал." in lock["breach"]["streak_text"]
