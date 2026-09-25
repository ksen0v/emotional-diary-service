"""Задачи по времени: сверка, позиции и надзор за потоками.

Первый планировщик в проекте. До сих пор всё считалось на чтении экрана или
по кнопке, и этого хватало, пока источник был фейковый. С настоящей биржей
так нельзя: сделка, о которой сервис узнаёт только когда трейдер откроет
страницу, — это не защита, а отчёт.

**Почему свои задачи на asyncio, а не APScheduler.** Архитектура ч.1 §9.7
называет APScheduler, но там же сказано главное: планировщик живёт в общем
процессе с остальным, а не в Celery. Задачи здесь — три цикла «поспать и
сделать», и библиотека ради них добавила бы зависимость, конфигурацию
и свой способ падать. Шов от этого не страдает: каждая задача — отдельная
корутина, и вынести её в процесс `worker` можно, ничего не переписав.
Отступление названо в README.

**Двойной запуск.** Пока процесс один, сталкиваться нечему. Когда их станет
несколько, сюда встанет advisory-лок Postgres — место для него подготовлено
одним вопросом в `_guard`, а не разбросано по задачам.
"""

import asyncio
import contextlib
import datetime as dt
import logging

from eds.app import pipeline
from eds.app.stream import StreamRegistry
from eds.modules.source import repo as source_repo
from eds.platform.db import session_factory
from eds.platform.errors import AppError

log = logging.getLogger("eds.scheduler")

# Сверка — страховка от потерянного события потока (Архитектура ч.1 §5.6).
RECONCILE_SEC = 10 * 60
# Позиции — раз в минуту при открытой сессии. Чаще незачем: правило на
# просадку с открытой позицией измеряется процентами депозита, а не секундами.
POSITIONS_SEC = 60
# Пересмотр списка потоков: подключение могло появиться или смениться.
STREAMS_SEC = 60


class Scheduler:
    """Фоновые задачи процесса. Каждая — отдельная корутина, каждая переживает
    свою ошибку: упавшая сверка не должна уносить с собой поток."""

    def __init__(self, streams: StreamRegistry | None = None):
        self.streams = streams or StreamRegistry()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.runs: dict[str, int] = {"reconcile": 0, "positions": 0, "streams": 0}
        self.last_error: dict[str, str | None] = {}

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(
                self._loop("streams", STREAMS_SEC, self._refresh_streams),
                name="scheduler-streams",
            ),
            asyncio.create_task(
                self._loop("reconcile", RECONCILE_SEC, self._reconcile_all),
                name="scheduler-reconcile",
            ),
            asyncio.create_task(
                self._loop("positions", POSITIONS_SEC, self._positions_all),
                name="scheduler-positions",
            ),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.streams.stop_all()

    async def _loop(self, name: str, seconds: float, work) -> None:
        # Первый проход — сразу: после перезапуска сервиса ждать десять минут,
        # прежде чем узнать о вчерашних сделках, незачем.
        while not self._stop.is_set():
            try:
                await work()
                self.runs[name] = self.runs.get(name, 0) + 1
                self.last_error[name] = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — цикл обязан пережить ошибку
                self.last_error[name] = f"{type(exc).__name__}: {exc}"
                log.warning("задача %s: %s", name, self.last_error[name])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _refresh_streams(self) -> None:
        await self.streams.sync_with_db()

    async def _reconcile_all(self) -> None:
        """Сверка по каждому активному сетевому источнику.

        Фейковый источник пропускаем: у него нет сети, и сверка по расписанию
        означала бы, что dev-инструмент живёт своей жизнью в фоне.
        """
        for user_id, provider in await _active_users():
            if provider == "fake":
                continue
            async with session_factory()() as s:
                try:
                    report = await pipeline.sync(s, user_id, kind="scheduled")
                    await s.commit()
                except AppError as exc:
                    # Неудачную сверку тоже надо сохранить: без записи в журнале
                    # на вопрос «почему сделки не приехали» нечем ответить.
                    with contextlib.suppress(Exception):
                        await s.commit()
                    log.warning("сверка по расписанию: %s", exc.message)
                    continue
            if report.inserted:
                log.info(
                    "сверка по расписанию: принято %s сделок", report.inserted
                )

    async def _positions_all(self) -> None:
        """Открытые позиции — только у источника, который их отдаёт.

        Проверку «отдаёт ли» делает `refresh_positions`: спрашивать возможности
        в двух местах значило бы однажды разойтись.
        """
        for user_id, _provider in await _active_users():
            async with session_factory()() as s:
                try:
                    await pipeline.refresh_positions(s, user_id)
                    await s.commit()
                except AppError as exc:
                    log.debug("позиции: %s", exc.message)

    def state(self) -> dict:
        return {
            "runs": dict(self.runs),
            "errors": {k: v for k, v in self.last_error.items() if v},
            "streams": self.streams.state(),
            "checked_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }


async def _active_users() -> list[tuple]:
    async with session_factory()() as s:
        return await source_repo.active_connection_owners(s)
