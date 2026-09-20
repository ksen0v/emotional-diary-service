"""Событийная шина на transactional outbox (Архитектура ч.1 §3).

Продюсер пишет событие в events.outbox в той же транзакции, что и своё изменение:
либо оба зафиксировались, либо ни одно. Консьюмер читает по своему курсору,
поэтому доставка «хотя бы один раз», а обработчики обязаны быть идемпотентными.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eds.platform.config import settings
from eds.platform.db import session_factory

log = logging.getLogger("eds.bus")

BATCH = 100


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    payload: dict[str, Any]


async def publish(
    s: AsyncSession,
    event_type: str,
    payload: dict[str, Any] | None = None,
    dedup_key: str | None = None,
) -> int | None:
    """Положить событие в outbox. Транзакцией управляет вызывающий.

    dedup_key делает публикацию идемпотентной: повтор возвращает None
    вместо второго события.
    """
    result = await s.execute(
        text(
            """
            INSERT INTO events.outbox (event_type, payload, dedup_key)
            VALUES (:t, CAST(:p AS jsonb), :d)
            ON CONFLICT (dedup_key) DO NOTHING
            RETURNING id
            """
        ),
        {"t": event_type, "p": _json(payload or {}), "d": dedup_key},
    )
    row = result.first()
    return row[0] if row else None


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


Handler = Callable[[Event], Awaitable[None]]


class Consumer:
    """Один консьюмер = одно имя и один курсор в events.cursors."""

    def __init__(self, name: str, handler: Handler, types: tuple[str, ...] | None = None):
        self.name = name
        self.handler = handler
        self.types = types
        self._stop = asyncio.Event()

    async def step(self) -> int:
        """Один проход: взять порцию событий, обработать, сдвинуть курсор.

        Возвращает число обработанных событий.
        """
        handled = 0
        async with session_factory()() as s:
            await s.execute(
                text(
                    """
                    INSERT INTO events.cursors (consumer) VALUES (:c)
                    ON CONFLICT (consumer) DO NOTHING
                    """
                ),
                {"c": self.name},
            )
            await s.commit()

            cursor = await s.execute(
                text(
                    "SELECT last_id FROM events.cursors "
                    "WHERE consumer = :c FOR UPDATE SKIP LOCKED"
                ),
                {"c": self.name},
            )
            row = cursor.first()
            if row is None:
                await s.rollback()
                return 0
            last_id = row[0]

            events = await s.execute(
                text(
                    """
                    SELECT id, event_type, payload FROM events.outbox
                    WHERE id > :last
                    ORDER BY id
                    LIMIT :limit
                    """
                ),
                {"last": last_id, "limit": BATCH},
            )
            rows = events.all()

            for event_id, event_type, payload in rows:
                if self.types is None or event_type in self.types:
                    try:
                        await self.handler(
                            Event(id=event_id, type=event_type, payload=payload or {})
                        )
                    except Exception as exc:
                        log.exception(
                            "консьюмер %s не смог обработать событие %s", self.name, event_id
                        )
                        await s.execute(
                            text(
                                """
                                INSERT INTO events.dead_letters (consumer, event_id, error)
                                VALUES (:c, :e, :err)
                                """
                            ),
                            {"c": self.name, "e": event_id, "err": f"{type(exc).__name__}: {exc}"},
                        )
                last_id = event_id
                handled += 1

            if handled:
                await s.execute(
                    text(
                        """
                        UPDATE events.cursors
                        SET last_id = :last, updated_at = now()
                        WHERE consumer = :c
                        """
                    ),
                    {"last": last_id, "c": self.name},
                )
            await s.commit()
        return handled

    async def run(self) -> None:
        """Бесконечный цикл. Падение одного прохода не убивает консьюмера."""
        log.info("консьюмер %s запущен", self.name)
        while not self._stop.is_set():
            try:
                if await self.step() == 0:
                    await asyncio.sleep(settings().bus_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("консьюмер %s: ошибка прохода, повтор через секунду", self.name)
                await asyncio.sleep(1.0)
        log.info("консьюмер %s остановлен", self.name)

    def stop(self) -> None:
        self._stop.set()


async def bus_state() -> dict:
    """Состояние шины для страницы состояния."""
    state: dict = {"published": 0, "consumers": [], "dead_letters": 0, "error": None}
    try:
        async with session_factory()() as s:
            published = await s.execute(text("SELECT count(*) FROM events.outbox"))
            state["published"] = published.scalar_one()

            dead = await s.execute(text("SELECT count(*) FROM events.dead_letters"))
            state["dead_letters"] = dead.scalar_one()

            # handled считаем по строкам, а не по значению курсора: ON CONFLICT DO NOTHING
            # сжигает номера последовательности, поэтому last_id может быть больше,
            # чем число событий, и на странице это выглядело бы противоречием.
            cursors = await s.execute(
                text(
                    """
                    SELECT
                        c.consumer,
                        c.last_id,
                        (
                            SELECT count(*) FROM events.outbox o
                            WHERE o.id <= c.last_id
                        ) AS handled,
                        (
                            SELECT count(*) FROM events.outbox o
                            WHERE o.id > c.last_id
                        ) AS pending
                    FROM events.cursors c
                    WHERE c.consumer NOT LIKE 'test-%'
                    ORDER BY c.consumer
                    """
                )
            )
            state["consumers"] = [
                {
                    "name": name,
                    "last_id": last_id,
                    "handled": handled,
                    "pending": pending,
                }
                for name, last_id, handled, pending in cursors
            ]
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
    return state
