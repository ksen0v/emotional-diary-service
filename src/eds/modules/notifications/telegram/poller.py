"""Цикл опроса Telegram: когда повторять, когда ждать, когда сдаться.

Здесь живёт вся политика отказов бота, и поэтому здесь же проходит главная
граница проверок шага. Три правила, из которых она состоит:

1. **Успешный запрос сбрасывает паузу — даже если обновлений ноль.** Это то же
   решение, что в потоке к бирже: пауза сбрасывалась по первому событию,
   а у молчащего потока событий нет, и окно слепоты росло до минуты. Пустой
   ответ long polling — признак живой связи, а не её отсутствия.

2. **Сдвиг `offset` — это подтверждение обработки.** Telegram присылает
   обновление заново, пока его не подтвердили, поэтому `offset` двигается
   после обработчика, а не до. Цена — повтор после падения посреди работы,
   и поэтому обработчики идемпотентны.

3. **Одно плохое обновление не останавливает бота навсегда.** После трёх
   неудач подряд оно пропускается с громкой записью в журнал. Иначе
   «надёжность» превращается в вечный цикл по одному и тому же событию,
   и бот не видит ничего другого.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from eds.modules.notifications.telegram.client import (
    AUTH,
    CONFLICT,
    WAIT,
    BotClient,
    TelegramFailure,
)

log = logging.getLogger("eds.telegram")

BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
# Сколько раз пробуем обработать одно и то же обновление, прежде чем пропустить.
MAX_UPDATE_ATTEMPTS = 3

Handler = Callable[[dict[str, Any]], Awaitable[None]]

# Состояния для страницы состояния и карточки настроек.
POLLING = "polling"
STOPPED = "stopped"
AUTH_ERROR = "auth_error"
CONFLICT_STATE = "conflict"
RETRYING = "retrying"


class Poller:
    """Опрос обновлений. Одна корутина, переживающая свои ошибки."""

    def __init__(self, client: BotClient, handler: Handler):
        self.client = client
        self.handler = handler
        self._stop = asyncio.Event()
        self.offset: int | None = None
        self.state: str = STOPPED
        self.backoff: float = BACKOFF_START
        self.last_ok_at: dt.datetime | None = None
        self.last_error: str | None = None
        self.updates_total = 0
        self.errors_total = 0
        self._failed_update: tuple[int, int] | None = None

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self.state = POLLING
        log.info("опрос Telegram начат")
        while not self._stop.is_set():
            try:
                updates = await self.client.get_updates(self.offset)
            except TelegramFailure as failure:
                if await self._on_failure(failure):
                    return
                continue

            # Ответ получен — связь жива, даже если отдавать было нечего.
            self.last_ok_at = dt.datetime.now(dt.UTC)
            self.backoff = BACKOFF_START
            self.last_error = None
            self.state = POLLING

            for update in updates:
                if self._stop.is_set():
                    return
                await self._handle(update)
        self.state = STOPPED

    async def _handle(self, update: dict[str, Any]) -> None:
        update_id = int(update.get("update_id", 0))
        try:
            await self.handler(update)
        except Exception as exc:  # noqa: BLE001 — обработчик не должен ронять бота
            self.errors_total += 1
            attempts = self._count_failure(update_id)
            self.last_error = f"обновление {update_id}: {exc}"
            if attempts < MAX_UPDATE_ATTEMPTS:
                log.warning(
                    "обновление %s не обработано (%s из %s): %s",
                    update_id,
                    attempts,
                    MAX_UPDATE_ATTEMPTS,
                    exc,
                )
                # `offset` не двигаем: Telegram пришлёт это обновление снова.
                return
            log.error(
                "обновление %s пропущено после %s неудач: %s",
                update_id,
                attempts,
                exc,
            )
        else:
            self.updates_total += 1
            self._failed_update = None
        self.offset = update_id + 1

    def _count_failure(self, update_id: int) -> int:
        if self._failed_update and self._failed_update[0] == update_id:
            self._failed_update = (update_id, self._failed_update[1] + 1)
        else:
            self._failed_update = (update_id, 1)
        return self._failed_update[1]

    async def _on_failure(self, failure: TelegramFailure) -> bool:
        """Отказ запроса. True — цикл заканчивается совсем."""
        self.errors_total += 1
        self.last_error = failure.message

        if failure.kind == AUTH:
            # Повторять с неверным токеном бессмысленно, а Telegram за упорство
            # наказывает. Останавливаемся и говорим об этом в настройках.
            self.state = AUTH_ERROR
            log.error("опрос Telegram остановлен: %s", failure.message)
            return True

        if failure.kind == CONFLICT:
            self.state = CONFLICT_STATE
            log.error("конфликт опроса Telegram: %s", failure.message)
        else:
            self.state = RETRYING
            log.warning("опрос Telegram не прошёл: %s", failure.message)

        pause = failure.retry_after if failure.kind == WAIT else self.backoff
        if failure.kind != WAIT:
            self.backoff = min(self.backoff * 2, BACKOFF_MAX)
        await self._sleep(pause or BACKOFF_START)
        return False

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "offset": self.offset,
            "updates": self.updates_total,
            "errors": self.errors_total,
            "backoff_sec": self.backoff,
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error,
        }
