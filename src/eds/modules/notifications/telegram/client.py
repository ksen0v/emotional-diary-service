"""Клиент Telegram: вызовы API и разбор того, что вернулось.

**Главный урок предыдущего шага, применённый здесь.** Три ошибки в потоке
к бирже дожили до живого ключа, потому что транспорт не был покрыт тестами:
проверялось, что делать с пришедшим кадром, и ни одного теста не было на то,
когда соединение считается мёртвым. Telegram — такая же чужая сеть со своими
таймаутами и своими молчаливыми отказами, поэтому здесь отдельно и явно
описано, чем «не ответил» отличается от «ответил, что нечего отдать»,
и это проверяется тестами наравне с разбором ответов.

**Что здесь считается контактом.** Успешный HTTP-ответ. Пустой список
обновлений — нормальный ответ long polling: Telegram держит запрос до
`POLL_TIMEOUT_SEC` и возвращает пустоту, если за это время никто ничего
не написал. Молчание пользователей — это не состояние связи, и прикладной
сторож «давно не было сообщений» здесь был бы ровно той ошибкой, которая
рвала живой сокет Binance каждые три минуты. Живость проверяет сам запрос.

**Почему свой цикл, а не диспетчер aiogram.** Стек зафиксирован ТЗ 9.7
(aiogram 3, long polling), и клиент библиотеки мы используем как есть.
Но политика отказов — когда повторять, когда ждать, когда сдаться и сказать
об этом вслух — должна быть нашей и под тестом, а не спрятана в чужом
`start_polling`. Отступление названо в README.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)

log = logging.getLogger("eds.telegram")

# Сколько Telegram держит запрос обновлений, прежде чем ответить пустотой.
POLL_TIMEOUT_SEC = 25
# Свой таймаут чтения — заведомо больше, чем держит сервер. Если сделать
# меньше или равным, каждый спокойный опрос выглядел бы обрывом связи,
# и сервис переподключался бы на ровном месте.
READ_TIMEOUT_SEC = 40
# Обычный запрос (отправка сообщения) столько ждать не должен.
CALL_TIMEOUT_SEC = 20

# Нас интересуют только два вида обновлений: сообщение (привязка и согласие)
# и нажатие кнопки (подтверждение снятия). Остальное Telegram не присылает
# вовсе — это и экономия, и защита от чужих сценариев.
ALLOWED_UPDATES = ["message", "callback_query"]

# Виды отказов. Разница между ними — это и есть то, чего не было в шаге 12.
RETRY = "retry"  # сеть, таймаут, 5xx: повторить с паузой
WAIT = "wait"  # 429: подождать ровно столько, сколько сказали
AUTH = "auth"  # токен не тот или отозван: повторять бессмысленно
CONFLICT = "conflict"  # тот же бот опрашивается из другого места
PERMANENT = "permanent"  # адресат недостижим: чат не начат, бот заблокирован


class TelegramFailure(Exception):
    """Отказ Telegram, разобранный по смыслу."""

    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after

    def __repr__(self) -> str:  # pragma: no cover — только для логов
        return f"TelegramFailure({self.kind}, {self.message!r})"


@dataclass(frozen=True)
class BotIdentity:
    id: int
    username: str
    first_name: str


def classify(exc: BaseException) -> TelegramFailure:
    """Исключение Telegram → отказ, с которым понятно, что делать.

    Отдельная чистая функция, потому что именно это и надо проверять тестом:
    разбор ответа и решение «связь мертва» — разные вещи, и вторую забыть
    проще всего.
    """
    if isinstance(exc, TelegramRetryAfter):
        return TelegramFailure(
            WAIT, f"Telegram просит подождать {exc.retry_after} с.", float(exc.retry_after)
        )
    if isinstance(exc, TelegramUnauthorizedError):
        return TelegramFailure(AUTH, "Токен бота не принят Telegram.")
    if isinstance(exc, TelegramConflictError):
        # Не тишина, а конфликт: тот же токен опрашивает кто-то ещё. Молчать
        # об этом нельзя — снаружи это выглядит как «бот не отвечает».
        return TelegramFailure(
            CONFLICT,
            "Этого бота уже опрашивает другой процесс: "
            "запущен второй сервис или включён webhook.",
        )
    if isinstance(exc, TelegramForbiddenError):
        return TelegramFailure(
            PERMANENT, "Адресат недоступен: бот заблокирован или диалог не начат."
        )
    if isinstance(exc, TelegramNotFound):
        return TelegramFailure(PERMANENT, "Чат не найден.")
    if isinstance(exc, TelegramBadRequest):
        return TelegramFailure(PERMANENT, f"Telegram отклонил запрос: {exc}")
    if isinstance(exc, TelegramServerError):
        return TelegramFailure(RETRY, f"Telegram ответил ошибкой сервера: {exc}")
    if isinstance(exc, TelegramNetworkError | TimeoutError | asyncio.TimeoutError):
        return TelegramFailure(RETRY, f"Telegram не ответил: {exc}")
    return TelegramFailure(RETRY, f"Неожиданная ошибка Telegram: {exc}")


def _plain(update: Any) -> dict[str, Any]:
    """Объект aiogram → обычный словарь.

    Наш код разбирает словари, а не модели библиотеки: так обработчики
    проверяются без aiogram вообще, поддельным обновлением из трёх полей.
    """
    if isinstance(update, dict):
        return update
    dump = getattr(update, "model_dump", None)
    if dump is None:  # pragma: no cover — на случай чужого объекта
        return dict(update)
    return dump(exclude_none=True)


class BotClient:
    """Тонкая обёртка над клиентом aiogram: вызов и разбор отказа.

    `api` подменяется в тестах. Ни одного сетевого вызова в тестах нет —
    поддельный объект поднимает настоящие исключения aiogram, и проверяется
    именно наше решение, что с ними делать.
    """

    def __init__(self, token: str, *, api: Any | None = None):
        self._own_api = api is None
        self._api = api if api is not None else _make_bot(token)

    async def me(self) -> BotIdentity:
        try:
            raw = await self._api.get_me()
        except Exception as exc:  # noqa: BLE001 — разбираем сами, ниже по виду
            raise classify(exc) from exc
        data = _plain(raw)
        return BotIdentity(
            id=int(data["id"]),
            username=str(data.get("username") or ""),
            first_name=str(data.get("first_name") or ""),
        )

    async def get_updates(
        self, offset: int | None, timeout: int = POLL_TIMEOUT_SEC
    ) -> list[dict[str, Any]]:
        try:
            raw = await self._api.get_updates(
                offset=offset,
                timeout=timeout,
                allowed_updates=ALLOWED_UPDATES,
                request_timeout=timeout + 10,
            )
        except Exception as exc:  # noqa: BLE001
            raise classify(exc) from exc
        return [_plain(item) for item in raw]

    async def send(
        self,
        chat_id: int,
        text: str,
        keyboard: dict[str, Any] | None = None,
    ) -> int:
        try:
            raw = await self._api.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                request_timeout=CALL_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001
            raise classify(exc) from exc
        return int(_plain(raw).get("message_id", 0))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Погасить «часики» на кнопке у друга.

        Неудача здесь ничего не значит для дела: подтверждение уже записано,
        а кнопка — это косметика. Поэтому ошибка только в журнал.
        """
        try:
            await self._api.answer_callback_query(
                callback_query_id=callback_id,
                text=text,
                request_timeout=CALL_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001
            log.info("ответ на кнопку не прошёл: %s", classify(exc).message)

    async def close(self) -> None:
        if not self._own_api:
            return
        session = getattr(self._api, "session", None)
        if session is not None:
            await session.close()


def _make_bot(token: str) -> Bot:
    return Bot(token=token, session=AiohttpSession(timeout=READ_TIMEOUT_SEC))


def confirm_keyboard(lock_id: str, label: str = "Подтверждаю снятие") -> dict[str, Any]:
    """Кнопка подтверждения под просьбой другу.

    Не ссылка и не команда: нажатие приходит обратно готовым `callback_data`,
    и друг не набирает ничего руками. `lock_id` внутри — чтобы подтверждение
    относилось к конкретной блокировке, а не к «последней».
    """
    return {
        "inline_keyboard": [[{"text": label, "callback_data": f"lift:{lock_id}"}]]
    }
