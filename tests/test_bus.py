"""Шина: событие доезжает от продюсера к консьюмеру, повтор не дублируется.

Требует TEST_DATABASE_URL с применёнными миграциями.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eds.contracts import events as ev
from eds.platform import bus


@pytest.fixture
async def factory(test_db_url: str):
    engine = create_async_engine(test_db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_publish_and_consume(factory, monkeypatch) -> None:
    monkeypatch.setattr(bus, "session_factory", lambda: factory)

    received: list[bus.Event] = []

    async def handler(event: bus.Event) -> None:
        received.append(event)

    async with factory() as s:
        event_id = await bus.publish(s, ev.PLATFORM_TEST_PING, {"n": 1})
        await s.commit()
    assert event_id is not None

    consumer = bus.Consumer(f"test-{os.getpid()}-{event_id}", handler)
    handled = await consumer.step()

    assert handled >= 1
    assert any(e.id == event_id for e in received)

    # второй проход не должен обработать то же событие снова
    received.clear()
    assert await consumer.step() == 0
    assert received == []

    # курсор тестового консьюмера за собой убираем: иначе он останется
    # в базе разработчика и будет виден на странице состояния
    async with factory() as s:
        await s.execute(
            text("DELETE FROM events.cursors WHERE consumer = :c"), {"c": consumer.name}
        )
        await s.commit()


async def test_dedup_key_blocks_second_publish(factory) -> None:
    async with factory() as s:
        key = f"test-dedup-{os.getpid()}"
        first = await bus.publish(s, ev.PLATFORM_TEST_PING, {"n": 1}, dedup_key=key)
        await s.commit()
        second = await bus.publish(s, ev.PLATFORM_TEST_PING, {"n": 2}, dedup_key=key)
        await s.commit()

    assert first is not None
    assert second is None


async def test_schemas_exist(factory) -> None:
    from eds.platform.db import MODULE_SCHEMAS

    async with factory() as s:
        rows = await s.execute(text("SELECT schema_name FROM information_schema.schemata"))
        names = {r[0] for r in rows}
    missing = [name for name in MODULE_SCHEMAS if name not in names]
    assert not missing, f"не созданы схемы: {missing}"
