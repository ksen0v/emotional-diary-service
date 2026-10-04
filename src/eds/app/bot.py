"""Бот: кто его держит, что он понимает и как переживает чужую сеть.

Здесь сходятся три вещи, которые не могут жить в модуле уведомлений: команда
`/start` привязывает **аккаунт** (identity), кнопка друга снимает **блокировку**
(incidents), а очередь отправки принадлежит **notifications**. Поэтому бот
живёт в оркестрации — по тому же правилу, по которому здесь живут потоки
к бирже и планировщик.

**Что бот умеет, и почему так мало.** Привязать аккаунт, принять согласие
доверенного лица, принять подтверждение снятия. Всё. Ни метрик, ни разметки,
ни управления сервисом из чата: ничего такого в ТЗ нет, а бот с командами,
которых никто не просил, — это ещё одна поверхность, которую надо защищать
и чинить.

**Отношение к сети — то же, что у потока к бирже.** Пустой ответ long polling
означает «связь жива, писать некому», и прикладного сторожа по тишине здесь
нет. Политика отказов целиком лежит в `poller.py` и проверяется тестами
наравне с разбором сообщений: урок шага 13 в том, что «когда считать
соединение мёртвым» — это код, а не надежда.
"""

import asyncio
import contextlib
import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import admin
from eds.app import notify as app_notify
from eds.contracts import events as ev
from eds.modules.incidents import repo as incidents_repo
from eds.modules.incidents import service as incidents_service
from eds.modules.notifications import repo as notify_repo
from eds.modules.notifications import service as notify_service
from eds.modules.notifications.telegram.client import BotClient
from eds.modules.notifications.telegram.poller import Poller
from eds.platform import auth, bus
from eds.platform.db import session_factory

log = logging.getLogger("eds.bot")

# Как часто разгребается очередь отправки. Две секунды — это задержка, которую
# трейдер не заметит, и один запрос по частичному индексу, который ничего
# не стоит.
SEND_EVERY_SEC = 2.0
# Как часто проверяем, не появился ли токен. Вставленный токен поднимает бота
# сразу — событием, — а эта проверка нужна на случай, если событие потерялось
# или процесс поднялся раньше базы.
TOKEN_CHECK_SEC = 30.0
# Пауза перед новой попыткой, если опрос свалился целиком.
RESTART_SEC = 10.0

NO_TOKEN = "no_token"
STARTING = "starting"

HELP = (
    "Это бот сервиса дисциплины. Он присылает алерты о правилах и блокировках.\n"
    "Чтобы привязать аккаунт, открой настройки сервиса и нажми «Привязать Telegram» "
    "— там будет код и ссылка."
)

LINKED_TEXT = (
    "Аккаунт привязан. Сюда будут приходить алерты о нарушениях и блокировках.\n"
    "Тексты сообщений можно переписать своими словами в настройках сервиса."
)

LINK_FAILED = (
    "Код не подошёл: он уже использован или устарел. "
    "Открой настройки сервиса и возьми новый."
)

CONSENT_TEXT = (
    "Согласие записано. Тебе придёт сообщение, только если сработает правило "
    "трейдера: факт срабатывания, без сумм и сделок.\n"
    "Иногда с кнопкой «Подтверждаю снятие» — нажимай её, только если правда "
    "поговорил с ним."
)

CONSENT_FAILED = "Ссылка недействительна или устарела. Попроси прислать новую."

CONFIRMED_TEXT = "Подтверждение принято. Блокировка снята."
CONFIRM_LATE = "Эта блокировка уже закончилась — подтверждать нечего."
CONFIRM_FOREIGN = "Эта кнопка не для тебя."


class BotRunner:
    """Владелец соединения с Telegram: опрос, отправка и состояние."""

    def __init__(self) -> None:
        self.client: BotClient | None = None
        self.poller: Poller | None = None
        self.username: str | None = None
        self.state: str = NO_TOKEN
        self.last_error: str | None = None
        self.sent_total = 0
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._reload = asyncio.Event()

    # --- жизненный цикл ---

    def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._supervise(), name="bot-poll"),
            asyncio.create_task(self._send_loop(), name="bot-send"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        if self.poller is not None:
            self.poller.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        await self._drop_client()

    def reload(self) -> None:
        """Токен изменился — поднять бота заново, не дожидаясь обхода."""
        self._reload.set()
        if self.poller is not None:
            self.poller.stop()

    def snapshot(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "state": self.state,
            "username": self.username,
            "sent": self.sent_total,
            "last_error": self.last_error,
        }
        if self.poller is not None:
            data.update(self.poller.snapshot())
        return data

    # --- опрос ---

    async def _supervise(self) -> None:
        while not self._stop.is_set():
            token = await self._token()
            if token is None:
                self.state = NO_TOKEN
                await self._wait(TOKEN_CHECK_SEC)
                continue

            self.state = STARTING
            self._reload.clear()
            self.client = BotClient(token)
            self.poller = Poller(self.client, self._handle_update)
            try:
                await self.poller.run()
            except asyncio.CancelledError:  # pragma: no cover — остановка процесса
                raise
            except Exception as exc:  # noqa: BLE001 — цикл не должен умирать молча
                self.last_error = str(exc)
                log.exception("опрос Telegram упал")
            finally:
                self.state = self.poller.state if self.poller else NO_TOKEN
                self.last_error = self.poller.last_error if self.poller else None
                await self._drop_client()

            if self._stop.is_set():
                return
            # Токен не принят — ждём, пока его поменяют. Повторять бессмысленно.
            wait = TOKEN_CHECK_SEC if self.state == "auth_error" else RESTART_SEC
            await self._wait(wait)

    async def _token(self) -> str | None:
        try:
            async with session_factory()() as s:
                token = await notify_service.bot_token(s)
                self.username = await notify_service.bot_username(s)
                return token
        except Exception as exc:  # noqa: BLE001 — база может быть ещё не поднята
            self.last_error = str(exc)
            return None

    async def _drop_client(self) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.close()
        self.client = None

    async def _wait(self, seconds: float) -> None:
        stop = asyncio.create_task(self._stop.wait())
        reload_ = asyncio.create_task(self._reload.wait())
        done, pending = await asyncio.wait(
            {stop, reload_}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(Exception):
                task.result()

    # --- отправка ---

    async def _send_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(SEND_EVERY_SEC)
            if self.client is None:
                continue
            try:
                async with session_factory()() as s:
                    counts = await notify_service.dispatch(
                        s, self.client, dt.datetime.now(dt.UTC)
                    )
                    await s.commit()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # noqa: BLE001 — очередь не роняет процесс
                self.last_error = str(exc)
                log.exception("отправка уведомлений не прошла")
                continue
            if counts["sent"]:
                self.sent_total += counts["sent"]
                log.info("отправлено уведомлений: %s", counts["sent"])

    # --- обработка обновлений ---

    async def _handle_update(self, update: dict[str, Any]) -> None:
        async with session_factory()() as s:
            await handle_update(s, update, self._reply)
            await s.commit()

    async def _reply(self, chat_id: int, text: str) -> None:
        if self.client is None:  # pragma: no cover — обработка идёт при живом клиенте
            return
        with contextlib.suppress(Exception):
            await self.client.send(chat_id, text)


runner = BotRunner()


# --- разбор обновлений: чистая часть, проверяемая без Telegram ---


async def handle_update(
    s: AsyncSession,
    update: dict[str, Any],
    reply,
) -> None:
    """Одно обновление от Telegram.

    Идемпотентно во всех ветках: Telegram присылает обновление заново, пока
    его не подтвердили сдвигом `offset`, а `offset` двигается после обработки.
    Повторная привязка гасит код, повторное согласие не меняет уже данное,
    повторное подтверждение упирается в первичный ключ.
    """
    message = update.get("message")
    if isinstance(message, dict):
        await _on_message(s, message, reply)
        return
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        await _on_callback(s, callback, reply)


async def _on_message(s: AsyncSession, message: dict[str, Any], reply) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if chat_id is None:
        return
    if not text.startswith("/start"):
        await reply(int(chat_id), HELP)
        return

    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not code:
        await reply(int(chat_id), HELP)
        return

    # Вход в админку. Стоит первым: код одноразовый и живёт минуты,
    # а остальные ветки долгоживущие, и перепутать их нельзя.
    if code.startswith(admin.PREFIX):
        answer = await admin.confirm_from_bot(s, code, int(chat_id))
        if answer is not None:
            await reply(int(chat_id), answer)
            return

    if code.startswith("B-"):
        contact = await notify_service.confirm_contact(s, code, int(chat_id))
        if contact is None:
            await reply(int(chat_id), CONSENT_FAILED)
            return
        await bus.publish(
            s,
            ev.NOTIFY_CONTACT_CONFIRMED,
            {
                "user_id": str(contact.user_id),
                "contact_id": str(contact.id),
                "display_name": contact.display_name,
            },
            dedup_key=f"contact-confirmed:{contact.id}",
        )
        await reply(int(chat_id), CONSENT_TEXT)
        log.info("доверенное лицо подтвердило согласие: %s", contact.handle)
        return

    user_id = await notify_service.complete_link(s, code, int(chat_id))
    if user_id is None:
        await reply(int(chat_id), LINK_FAILED)
        return
    await bus.publish(
        s,
        ev.NOTIFY_TELEGRAM_LINKED,
        {"user_id": str(user_id)},
        dedup_key=f"tg-linked:{user_id}:{chat_id}",
    )
    await reply(int(chat_id), LINKED_TEXT)
    log.info("Telegram привязан к пользователю %s", user_id)


async def _on_callback(s: AsyncSession, callback: dict[str, Any], reply) -> None:
    """Друг нажал «Подтверждаю снятие»."""
    data = str(callback.get("data") or "")
    chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
    if not data.startswith("lift:") or chat_id is None:
        return

    contact = await notify_repo.contact_by_chat(s, int(chat_id))
    if contact is None:
        await reply(int(chat_id), CONFIRM_FOREIGN)
        return

    try:
        lock_id = uuid.UUID(data.split(":", 1)[1])
    except ValueError:  # pragma: no cover — данные кнопки собираем мы сами
        return

    lock = await incidents_repo.lock_by_id(s, contact.user_id, lock_id)
    if lock is None:
        await reply(int(chat_id), CONFIRM_FOREIGN)
        return
    if lock.state != incidents_service.ACTIVE:
        await reply(int(chat_id), CONFIRM_LATE)
        return

    now = dt.datetime.now(dt.UTC)
    await incidents_service.confirm_by_buddy(
        s, contact.user_id, lock, contact_id=contact.id, chat_id=int(chat_id), now=now
    )
    await reply(int(chat_id), CONFIRMED_TEXT)
    log.info("доверенное лицо подтвердило снятие блокировки %s", lock.id)


# --- просьба подтвердить, со стороны сервиса ---


async def ask_buddy(
    s: AsyncSession, user_id: uuid.UUID, lock, prefs: auth.UserPrefs
) -> None:
    await app_notify.request_buddy_confirm(s, user_id, lock, prefs)
