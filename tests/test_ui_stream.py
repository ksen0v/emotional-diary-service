"""Живое обновление интерфейса: хаб подписок и эндпоинт потока.

Проверяется то, из-за чего этот шаг вообще делается: блокировка должна
доехать до открытой вкладки сама. Плюс три границы, на которых такие потоки
обычно и ломаются: лимит соединений, догон после разрыва и переполненная
очередь вкладки, которая перестала читать.
"""

import asyncio
import uuid

import httpx
import pytest

from eds.app import ui_stream
from eds.contracts import events as ev
from eds.platform import bus
from tests.test_identity import register

pytestmark = pytest.mark.usefixtures("clean_users")

USER = uuid.uuid4()


@pytest.fixture(autouse=True)
def clean_hub():
    ui_stream.hub = ui_stream.Hub()
    yield
    ui_stream.hub = ui_stream.Hub()


# --- хаб ---


def test_subscriber_gets_what_was_published() -> None:
    queue = ui_stream.hub.subscribe(USER)
    ui_stream.hub.publish(USER, "lock_started", {"lock_id": "9f2c"})

    event = queue.get_nowait()
    assert event.type == "lock_started"
    assert event.data == {"lock_id": "9f2c"}


def test_events_do_not_leak_between_users() -> None:
    """Доступ строго по user_id (ТЗ 9.3). Для потока это не формальность."""
    mine = ui_stream.hub.subscribe(USER)
    other = ui_stream.hub.subscribe(uuid.uuid4())
    ui_stream.hub.publish(USER, "lock_started", {"lock_id": "9f2c"})

    assert mine.qsize() == 1
    assert other.qsize() == 0


def test_encoding_is_the_sse_frame() -> None:
    event = ui_stream.UiEvent(id=7, type="lock_started", data={"rule": "2 стопа"})
    text = event.encode()
    assert text.startswith("id: 7\nevent: lock_started\ndata: ")
    assert text.endswith("\n\n")
    # Русский текст уходит как есть: SSE это UTF-8, а \\u0441-экранирование
    # пришлось бы разворачивать на фронте.
    assert "2 стопа" in text


def test_replay_returns_only_what_was_missed() -> None:
    ui_stream.hub.subscribe(USER)
    first = ui_stream.hub.publish(USER, "trade_ingested", {"n": 1})
    second = ui_stream.hub.publish(USER, "trade_ingested", {"n": 2})

    missed = ui_stream.hub.replay(USER, first.id)
    assert [e.id for e in missed] == [second.id]
    # Без Last-Event-ID догонять нечего: вкладка перечитает `today`.
    assert ui_stream.hub.replay(USER, None) == []


def test_full_queue_drops_events_instead_of_growing() -> None:
    """Вкладка перестала читать — копить для неё бессмысленно.

    `today` остаётся полным источником истины, и она догонит перечитыванием.
    Копить значило бы держать память за вкладку, которой, возможно, уже нет.
    """
    queue = ui_stream.hub.subscribe(USER)
    for n in range(ui_stream.QUEUE_SIZE + 10):
        ui_stream.hub.publish(USER, "trade_ingested", {"n": n})
    assert queue.qsize() == ui_stream.QUEUE_SIZE


def test_unsubscribe_removes_the_queue() -> None:
    queue = ui_stream.hub.subscribe(USER)
    assert ui_stream.hub.count(USER) == 1
    ui_stream.hub.unsubscribe(USER, queue)
    assert ui_stream.hub.count(USER) == 0


# --- мост от шины ---


async def test_domain_event_becomes_a_screen_event() -> None:
    queue = ui_stream.hub.subscribe(USER)
    await ui_stream.on_event(
        bus.Event(
            id=1,
            type=ev.INCIDENTS_LOCK_STARTED,
            payload={"user_id": str(USER), "lock_id": "9f2c", "rule_name": "2 стопа"},
        )
    )
    event = queue.get_nowait()
    assert event.type == "lock_started"
    # user_id на экран не уезжает: вкладка и так знает, чья она.
    assert "user_id" not in event.data
    assert event.data["rule_name"] == "2 стопа"


async def test_unknown_event_types_are_ignored() -> None:
    queue = ui_stream.hub.subscribe(USER)
    await ui_stream.on_event(
        bus.Event(id=1, type=ev.PLATFORM_TEST_PING, payload={"user_id": str(USER)})
    )
    assert queue.qsize() == 0


def test_consumer_asks_only_for_what_it_shows() -> None:
    """Фильтр по типам событий — не оптимизация.

    Без него консьюдер тащил бы из шины всё подряд и сдвигал курсор по чужим
    событиям, а нам важно догонять ровно то, от чего меняется картинка.
    """
    consumer = ui_stream.consumer()
    assert consumer.name == "ui_push"
    assert set(consumer.types or ()) == set(ui_stream.UI_TYPES)


# --- эндпоинт ---


async def test_first_frame_is_a_heartbeat_then_events_follow() -> None:
    """Кадры проверяются напрямую, без живого соединения.

    Через HTTP это не проверить: транспорт httpx дочитывает ответ до конца,
    прежде чем вернуть его, а поток по определению не кончается. Тест на живом
    соединении просто висел бы до таймаута и ничего не доказывал.
    """
    queue = ui_stream.hub.subscribe(USER)
    stream = ui_stream.frames(USER, queue)
    # Первый кадр — хартбит: иначе вкладка не знает, что соединение
    # состоялось, пока не случится первое событие.
    assert (await anext(stream)).startswith(":ka")

    ui_stream.hub.publish(USER, "lock_started", {"rule_name": "2 стопа"})
    frame = await anext(stream)
    assert frame.startswith("id: ")
    assert "event: lock_started" in frame
    assert "2 стопа" in frame

    await stream.aclose()
    # Закрытая вкладка снимается с подписки — иначе события копились бы
    # в очередь, которую никто не читает.
    assert ui_stream.hub.count(USER) == 0


async def test_missed_events_go_out_before_the_heartbeat() -> None:
    """Догон по Last-Event-ID идёт первым, иначе порядок на экране соврёт."""
    queue = ui_stream.hub.subscribe(USER)
    missed = [ui_stream.UiEvent(id=4, type="trade_ingested", data={"n": 1})]
    stream = ui_stream.frames(USER, queue, missed)
    assert "event: trade_ingested" in await anext(stream)
    assert (await anext(stream)).startswith(":ka")
    await stream.aclose()


async def test_disconnected_tab_ends_the_stream() -> None:
    queue = ui_stream.hub.subscribe(USER)

    async def gone() -> bool:
        return True

    frames = [f async for f in ui_stream.frames(USER, queue, disconnected=gone)]
    assert frames == [frames[0]]
    assert ui_stream.hub.count(USER) == 0


async def test_fourth_tab_is_refused_but_not_broken(
    app_client: httpx.AsyncClient,
) -> None:
    """Четвёртая вкладка переходит на опрос (Архитектура ч.2 §6, решение 2).

    Именно `429` с понятным кодом, а не молчаливый отказ: фронт по коду
    понимает, что надо опрашивать, а не что сервис сломался.
    """
    await register(app_client)
    me = (await app_client.get("/api/v1/me")).json()
    user_id = uuid.UUID(me["user"]["id"])
    for _ in range(ui_stream.MAX_CONNECTIONS):
        ui_stream.hub.subscribe(user_id)

    res = await app_client.get("/api/v1/stream")
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "stream_limit_reached"


async def test_stream_needs_a_session(app_client: httpx.AsyncClient) -> None:
    res = await app_client.get("/api/v1/stream")
    assert res.status_code == 401


async def test_lock_reaches_the_open_tab(app_client: httpx.AsyncClient) -> None:
    """Главное, ради чего шаг делается: блокировка доезжает сама.

    Проверяется через хаб, а не через живое соединение: соединение держится
    открытым, и тест, который его читает, должен был бы ждать по таймауту.
    Мост от шины до очереди вкладки — это и есть путь события.
    """
    await register(app_client)
    me = (await app_client.get("/api/v1/me")).json()
    user_id = uuid.UUID(me["user"]["id"])

    queue = ui_stream.hub.subscribe(user_id)
    await ui_stream.on_event(
        bus.Event(
            id=1,
            type=ev.INCIDENTS_LOCK_STARTED,
            payload={
                "user_id": str(user_id),
                "lock_id": str(uuid.uuid4()),
                "rule_name": "2 стопа подряд",
            },
        )
    )
    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event.type == "lock_started"
    assert event.data["rule_name"] == "2 стопа подряд"
