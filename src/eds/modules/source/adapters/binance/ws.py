"""Поток пользовательских событий Binance: listenKey и WebSocket.

Транспорт и только транспорт: здесь нет ни базы, ни сделок, ни правил. Класс
открывает соединение, держит его живым и отдаёт наружу разобранные события.
Что с ними делать, решает оркестрация (`app/stream.py`) — по той же причине,
по которой адаптер TMM не знает про модуль trades.

Две вещи, на которых такие потоки обычно и ломаются:

**Ключ потока живёт 60 минут и умирает молча.** Продление — раз в 30, с запасом
вдвое: пропущенное продление не даёт ошибки, соединение просто перестаёт
присылать события, и снаружи это выглядит как «биржа затихла». Отсюда же
требование к оркестрации: молчание потока дольше нескольких минут при открытой
сессии — это повод для сверки, а не для ожидания.

**Разрыв — норма, а не сбой.** Соединение рвут прокси, сеть и сама биржа.
Поэтому переподключение встроено сюда с нарастающей паузой, а страховкой от
потерянных за время разрыва событий служит сверка через REST: поток быстрый,
но не надёжный, надёжна сверка (Архитектура ч.1 §5.6).
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator

import websockets

from eds.modules.source.adapters.binance.rest import (
    ENDPOINT_LISTEN_KEY,
    WS_BASE,
    BinanceClient,
)

log = logging.getLogger("eds.source.binance.ws")

# Ключ потока живёт час. Продлеваем вдвое чаще: пропущенное продление роняет
# поток без единой ошибки в логе.
KEEPALIVE_SEC = 30 * 60
# Пауза перед переподключением: растёт до потолка, чтобы при долгой недоступности
# биржи не биться в неё каждую секунду.
RECONNECT_START_SEC = 2.0
RECONNECT_MAX_SEC = 60.0
# Если за это время не пришло ни одного сообщения — считаем соединение мёртвым
# и переподключаемся. Binance шлёт ping сам, поэтому тишина дольше — это не
# затишье на рынке, а обрыв, который нам не сообщили.
SILENCE_SEC = 180.0


class UserDataStream:
    """Поток пользовательских событий одного подключения."""

    def __init__(
        self,
        client: BinanceClient,
        *,
        ws_base: str = WS_BASE,
        connect=websockets.connect,
    ):
        self._client = client
        self._ws_base = ws_base
        self._connect = connect
        self._listen_key: str | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.reconnects = 0

    def stop(self) -> None:
        self._stop.set()

    async def open_listen_key(self) -> str:
        payload = await self._client.keyed(ENDPOINT_LISTEN_KEY, method="POST")
        key = (payload or {}).get("listenKey")
        if not key:
            raise RuntimeError("Binance не выдал ключ потока")
        self._listen_key = key
        return key

    async def keepalive(self) -> None:
        if self._listen_key is None:
            return
        await self._client.keyed(ENDPOINT_LISTEN_KEY, method="PUT")

    async def close_listen_key(self) -> None:
        if self._listen_key is None:
            return
        with contextlib.suppress(Exception):
            await self._client.keyed(ENDPOINT_LISTEN_KEY, method="DELETE")
        self._listen_key = None

    async def events(self) -> AsyncIterator[dict]:
        """Бесконечный поток событий с переподключением.

        Выход — только по `stop()`. Ошибки соединения не пробрасываются
        наружу: для вызывающего обрыв это не исключение, а пауза.
        """
        pause = RECONNECT_START_SEC
        while not self._stop.is_set():
            try:
                key = await self.open_listen_key()
            except Exception as exc:  # noqa: BLE001 — любая причина means «ждём»
                log.warning("Binance: ключ потока не получен (%s)", exc)
                await self._wait(pause)
                pause = min(pause * 2, RECONNECT_MAX_SEC)
                continue

            try:
                async for event in self._session(f"{self._ws_base}/{key}"):
                    pause = RECONNECT_START_SEC
                    yield event
            except Exception as exc:  # noqa: BLE001
                log.warning("Binance: поток оборвался (%s)", type(exc).__name__)
            finally:
                await self.close_listen_key()
                self.connected = False

            if self._stop.is_set():
                return
            self.reconnects += 1
            await self._wait(pause)
            pause = min(pause * 2, RECONNECT_MAX_SEC)

    async def _session(self, url: str) -> AsyncIterator[dict]:
        async with self._connect(url) as socket:
            self.connected = True
            log.info("Binance: поток открыт")
            keeper = asyncio.create_task(self._keepalive_loop())
            try:
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(
                            socket.recv(), timeout=SILENCE_SEC
                        )
                    except TimeoutError:
                        log.warning("Binance: поток молчит, переподключаюсь")
                        return
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(event, dict):
                        yield event
            finally:
                keeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keeper

    async def _keepalive_loop(self) -> None:
        while not self._stop.is_set():
            await self._wait(KEEPALIVE_SEC)
            if self._stop.is_set():
                return
            try:
                await self.keepalive()
            except Exception as exc:  # noqa: BLE001
                log.warning("Binance: продление ключа потока не прошло (%s)", exc)
                return

    async def _wait(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
