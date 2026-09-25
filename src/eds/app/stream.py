"""Поток от биржи к сервису: события Binance → сделки → правила.

Оркестрация, а не адаптер: транспорт живёт в `adapters/binance/ws.py`, а здесь
решается, что делать с пришедшим событием. Разделение то же, что у сверки.

**Зачем поток вообще, если экран всё равно не обновляется сам.** Живого
обновления интерфейса пока нет, и блокировку видно только после перезагрузки.
Но правила считаются не экраном, а сервером: без потока сервис узнаёт о сделке
только когда трейдер нажмёт «Сверить» или когда сработает расписание, то есть
до десяти минут спустя. Для сервиса, чья работа — встать между импульсом
и следующей сделкой, десять минут это не задержка, а отсутствие функции.

**Поток не заменяет сверку.** Он быстрый, но теряет события на разрывах.
Надёжна сверка, и она идёт своим расписанием независимо (`app/scheduler.py`).
"""

import asyncio
import contextlib
import logging
import uuid

from eds.app import pipeline
from eds.modules.source import repo as source_repo
from eds.modules.source.adapters.binance import mapping
from eds.modules.source.adapters.binance.rest import BinanceClient
from eds.modules.source.adapters.binance.source import BinanceSource
from eds.modules.source.adapters.binance.ws import UserDataStream
from eds.platform import auth, crypto
from eds.platform.db import session_factory

log = logging.getLogger("eds.stream")

# Пауза перед повторной попыткой, когда подключение вообще не поднялось.
RETRY_SEC = 30.0


class BinanceStream:
    """Поток одного подключения Binance. Живёт, пока подключение активно."""

    def __init__(self, user_id: uuid.UUID, connection_id: uuid.UUID):
        self.user_id = user_id
        self.connection_id = connection_id
        self._stream: UserDataStream | None = None
        self._stop = asyncio.Event()
        self.fills_applied = 0
        self.last_error: str | None = None

    def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            self._stream.stop()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — поток не должен падать совсем
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("поток Binance остановился: %s", self.last_error)
            if self._stop.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=RETRY_SEC)

    async def _session(self) -> None:
        async with session_factory()() as s:
            connection = await source_repo.connection_by_id(
                s, self.user_id, self.connection_id
            )
            if connection is None or not connection.is_active:
                self.stop()
                return
            if not connection.key_encrypted or not connection.secret_encrypted:
                self.last_error = "у подключения нет ключа или секрета"
                self.stop()
                return
            key = crypto.decrypt(connection.key_encrypted)
            secret = crypto.decrypt(connection.secret_encrypted)

        client = BinanceClient(key, secret)
        async with client:
            self._stream = UserDataStream(client)
            async for event in self._stream.events():
                if self._stop.is_set():
                    return
                await self._handle(event)

    async def _handle(self, event: dict) -> None:
        kind = event.get("e")
        if kind == mapping.ORDER_UPDATE:
            await self._on_order(event)
        elif kind == mapping.ACCOUNT_UPDATE:
            # Баланс и позиции приходят здесь же, но считать по ним просадку
            # мы не спешим: их перечитывает расписание целиком, а частичное
            # обновление из события легко разошлось бы с настоящим состоянием.
            log.debug("Binance: обновление счёта")

    async def _on_order(self, event: dict) -> None:
        try:
            fill = mapping.fill_from_stream(event)
        except mapping.MappingError as exc:
            log.warning("Binance: событие исполнения не разобрано: %s", exc)
            return
        if fill is None:
            return

        async with session_factory()() as s:
            connection = await source_repo.connection_by_id(
                s, self.user_id, self.connection_id
            )
            if connection is None or not connection.is_active:
                self.stop()
                return
            # Клиент здесь не нужен: исполнение уже пришло, ходить за ним
            # в сеть незачем. Источнику нужна только сессия.
            source = BinanceSource(
                None,  # type: ignore[arg-type]
                s=s,
                connection_id=connection.id,
                user_id=self.user_id,
            )
            trades = await source.accept_fills([(fill, event)])
            if not trades:
                await s.commit()
                return

            ctx = await pipeline.build_context(s, self.user_id, connection)
            report = await pipeline.trades_service.ingest_batch(s, ctx, trades)
            prefs = await auth.prefs_of(s, self.user_id)
            engine_report = await pipeline.engine.after_ingest(
                s, self.user_id, prefs, sorted(report.touched_days)
            )
            await s.commit()

        self.fills_applied += 1
        log.info(
            "поток: исполнение %s %s → сделок %s, сработало правил %s",
            fill.symbol,
            fill.external_id,
            report.inserted,
            engine_report.fired,
        )


class StreamRegistry:
    """Потоки всех активных подключений Binance.

    Один процесс на всех: шардирование по пользователям (Архитектура ч.1 §8)
    понадобится, когда пользователей станет много, а пока их один. Заложено
    то, что важно уже сейчас: поток принадлежит подключению, а не процессу,
    и снимается вместе с ним.
    """

    def __init__(self) -> None:
        self._running: dict[uuid.UUID, tuple[BinanceStream, asyncio.Task]] = {}

    async def sync_with_db(self) -> None:
        """Поднять потоки для активных подключений Binance, лишние — погасить."""
        async with session_factory()() as s:
            wanted = {
                row.id: row.user_id
                for row in await source_repo.active_binance_connections(s)
            }

        for connection_id in list(self._running):
            if connection_id not in wanted:
                await self._stop_one(connection_id)

        for connection_id, user_id in wanted.items():
            if connection_id in self._running:
                continue
            stream = BinanceStream(user_id, connection_id)
            task = asyncio.create_task(
                stream.run(), name=f"binance-stream-{connection_id}"
            )
            self._running[connection_id] = (stream, task)
            log.info("поток Binance поднят для подключения %s", connection_id)

    async def _stop_one(self, connection_id: uuid.UUID) -> None:
        stream, task = self._running.pop(connection_id)
        stream.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        log.info("поток Binance снят для подключения %s", connection_id)

    async def stop_all(self) -> None:
        for connection_id in list(self._running):
            await self._stop_one(connection_id)

    def state(self) -> list[dict]:
        return [
            {
                "connection_id": str(connection_id),
                "fills_applied": stream.fills_applied,
                "last_error": stream.last_error,
            }
            for connection_id, (stream, _task) in self._running.items()
        ]
