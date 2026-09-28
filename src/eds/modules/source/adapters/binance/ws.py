"""Поток пользовательских событий Binance: listenKey и WebSocket.

Транспорт и только транспорт: здесь нет ни базы, ни сделок, ни правил. Класс
открывает соединение, держит его живым и отдаёт наружу разобранные события.
Что с ними делать, решает оркестрация (`app/stream.py`) — по той же причине,
по которой адаптер TMM не знает про модуль trades.

Три вещи, на которых такие потоки ломаются, и все три этот файл уже ломал.

**Ключ потока живёт 60 минут и умирает молча.** Продление — раз в 30, с запасом
вдвое. Пропущенное продление не даёт ошибки: биржа присылает `listenKeyExpired`
и закрывает соединение, а если само продление не прошло по сети — сокет
остаётся открытым и перестаёт что-либо значить. Поэтому неудачное продление
здесь роняет сессию в переподключение, а не возвращается молча.

**Разрыв — норма, а не сбой.** Соединение рвут прокси, сеть и сама биржа.
Переподключение встроено сюда с нарастающей паузой, а страховкой от потерянных
за время разрыва событий служит сверка через REST: поток быстрый, но не
надёжный, надёжна сверка (Архитектура ч.1 §5.6).

**Тишина — это не обрыв.** Здесь стоял сторож: «не пришло ни одного сообщения
за три минуты — значит соединение мёртвое». Он был неправ дважды. Трейдер
не торгует непрерывно, и час тишины — обычное состояние рабочего соединения.
А Binance шлёт ping раз в те же три минуты, библиотека отвечает pong сама,
и до `recv()` эти кадры не доходят — то есть живое соединение выглядело ровно
как мёртвое. На живом ключе сторож рвал здоровый сокет каждые три минуты,
пауза перед переподключением росла до минуты, и сервис был слеп по минуте
из каждых трёх. Живость проверяет ping/pong самой библиотеки: сокет, который
не отвечает, закрывается, и обрыв приходит сюда исключением, как и должен.
"""

import asyncio
import contextlib
import datetime as dt
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
# Пауза перед переподключением: растёт до потолка, чтобы при долгой
# недоступности биржи не биться в неё каждую секунду.
RECONNECT_START_SEC = 2.0
RECONNECT_MAX_SEC = 60.0
# Свой ping раз в двадцать секунд и ожидание pong двадцать. Это и есть проверка
# живости; прикладного сторожа по тишине здесь нет — см. шапку файла.
PING_INTERVAL_SEC = 20.0
PING_TIMEOUT_SEC = 20.0
CLOSE_TIMEOUT_SEC = 5.0

# Событие «ключ потока истёк». Приходит перед тем, как биржа закроет
# соединение; обрабатываем сами, чтобы переподключиться с новым ключом,
# а не разбирать потом закрытие без объяснения.
LISTEN_KEY_EXPIRED = "listenKeyExpired"


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
        # Причина, по которой поток сейчас не работает. Без неё снаружи видно
        # только «поток есть», и молчащий по полчаса WebSocket выглядит
        # так же, как работающий, — ровно та ошибка, из-за которой первая
        # живая проверка ничего не доказала.
        self.last_error: str | None = None
        self.last_event_at: dt.datetime | None = None
        self.opened_at: dt.datetime | None = None

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

    async def probe_socket(self, listen_key: str, *, timeout: float = 10.0) -> None:
        """Открыть соединение и сразу закрыть. Нужна только для диагностики.

        Молчание не проверяем: биржа не присылает ничего, пока ничего
        не происходит, и ждать события значило бы требовать от трейдера
        совершить сделку ради проверки.
        """
        socket = await asyncio.wait_for(
            self._open(f"{self._ws_base}/{listen_key}"), timeout=timeout
        )
        await socket.close()

    def _open(self, url: str):
        """Соединение с параметрами живости.

        Отдельным методом, потому что мест два — рабочая сессия
        и диагностическая проверка, — и разойтись им нельзя.
        """
        return self._connect(
            url,
            ping_interval=PING_INTERVAL_SEC,
            ping_timeout=PING_TIMEOUT_SEC,
            close_timeout=CLOSE_TIMEOUT_SEC,
        )

    async def events(self) -> AsyncIterator[dict]:
        """Бесконечный поток событий с переподключением.

        Выход — только по `stop()`. Ошибки соединения не пробрасываются
        наружу: для вызывающего обрыв это не исключение, а пауза.
        """
        pause = RECONNECT_START_SEC
        while not self._stop.is_set():
            try:
                key = await self.open_listen_key()
            except Exception as exc:  # noqa: BLE001 — любая причина значит «ждём»
                self.last_error = f"ключ потока не получен: {type(exc).__name__}: {exc}"
                log.warning("Binance: %s", self.last_error)
                await self._wait(pause)
                pause = min(pause * 2, RECONNECT_MAX_SEC)
                continue

            opened = False
            try:
                async for event in self._session(f"{self._ws_base}/{key}"):
                    opened = True
                    self.last_event_at = dt.datetime.now(dt.UTC)
                    yield event
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"поток оборвался: {type(exc).__name__}: {exc}"
                log.warning("Binance: %s", self.last_error)
            finally:
                opened = opened or self.opened_at is not None
                await self.close_listen_key()
                self.connected = False

            if self._stop.is_set():
                return
            self.reconnects += 1
            # Пауза считается от того, удалось ли поднять соединение, а не от
            # того, пришли ли по нему события. Иначе у молчащего — то есть
            # совершенно здорового — потока окно слепоты росло до минуты.
            pause = RECONNECT_START_SEC if opened else pause
            await self._wait(pause)
            pause = min(pause * 2, RECONNECT_MAX_SEC)

    async def _session(self, url: str) -> AsyncIterator[dict]:
        self.opened_at = None
        async with self._open(url) as socket:
            self.connected = True
            self.opened_at = dt.datetime.now(dt.UTC)
            self.last_error = None
            log.info("Binance: поток открыт")
            keeper = asyncio.create_task(self._keepalive_loop(socket))
            try:
                while not self._stop.is_set():
                    raw = await socket.recv()
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("e") == LISTEN_KEY_EXPIRED:
                        # Биржа сейчас закроет соединение. Уходим сами, чтобы
                        # взять новый ключ, а не разбирать закрытие без причины.
                        self.last_error = "ключ потока истёк, переподключаюсь"
                        log.warning("Binance: %s", self.last_error)
                        return
                    yield event
            finally:
                keeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keeper

    async def _keepalive_loop(self, socket) -> None:
        """Продление ключа. Не продлилось — рвём соединение.

        Раньше здесь был `return`, и это была тихая смерть: ключ переставал
        действовать, а сокет оставался открытым и снаружи выглядел рабочим.
        """
        while not self._stop.is_set():
            await self._wait(KEEPALIVE_SEC)
            if self._stop.is_set():
                return
            try:
                await self.keepalive()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"продление ключа потока не прошло: {exc}"
                log.warning("Binance: %s", self.last_error)
                with contextlib.suppress(Exception):
                    await socket.close()
                return

    async def _wait(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
