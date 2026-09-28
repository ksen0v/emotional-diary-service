"""Живое обновление интерфейса: мост от шины к открытым вкладкам.

Зачем это нужно, если есть `GET /today`. Блокировка возникает не по действию
трейдера в интерфейсе, а по событию извне: пришла сделка — сработало правило.
При опросе раз в тридцать секунд трейдер полминуты смотрит на обычный экран,
хотя сервис уже решил, что торговать нельзя. Для сервиса, чья суть — встроить
трение между импульсом и сделкой, эти полминуты обесценивают половину работы:
за них открывается ещё одна сделка в тильте.

**Два входа, и разница между ними существенная.**

Доменные события приходят из шины: их читает консьюмер `ui_push` со своим
курсором, ровно как любой другой потребитель. Они долговечны, переживают
перезапуск и разбираются задним числом SQL-запросом.

Живые числа открытой позиции приходят сюда напрямую из оркестрации, минуя
шину. Это сознательно: цена по открытой позиции меняется каждую секунду,
и складывать такие тики в `events.outbox`, который мы принципиально не чистим
(Архитектура ч.1 §8), значило бы засорять историю сервиса шумом, который
через минуту никому не нужен. В шину идёт факт «позиция открылась», в хаб —
её текущий результат.

**Правило, которое не даёт этому расползтись:** поток не несёт данных,
которых нет в `GET /today`. Событие — это сигнал «перечитай» плюс готовое
значение для мгновенного отображения. Если фронт начнёт собирать состояние
только из событий, первый же пропущенный разрыв даст расхождение, которое
невозможно воспроизвести.
"""

import asyncio
import datetime as dt
import json
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from eds.contracts import events as ev
from eds.platform import bus

log = logging.getLogger("eds.ui_stream")

# Лимит живых соединений на пользователя (Архитектура ч.2 §2, решение ч.2 §6).
# Три — это «терминал плюс дневник плюс телефон», настоящий рабочий набор
# вкладок. Четвёртая не ломается: она переходит на опрос и узнаёт о блокировке
# на несколько секунд позже.
MAX_CONNECTIONS = 3

# Комментарий-хартбит против прокси-таймаутов. Раз в двадцать секунд:
# типовой таймаут прокси — минута.
HEARTBEAT_SEC = 20.0

# Сколько последних событий держим на пользователя для догона по Last-Event-ID.
# Не журнал: если разрыв был длиннее буфера, вкладка просто перечитает `today`,
# который и так остаётся полным источником истины.
REPLAY_SIZE = 50

# Очередь одной вкладки. Переполнилась — значит вкладка не читает, и копить
# для неё бессмысленно: она всё равно перечитает `today`.
QUEUE_SIZE = 100


@dataclass
class UiEvent:
    """То, от чего меняется картинка на экране."""

    id: int
    type: str
    data: dict

    def encode(self) -> str:
        body = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"id: {self.id}\nevent: {self.type}\ndata: {body}\n\n"


@dataclass
class _UserState:
    queues: list[asyncio.Queue] = field(default_factory=list)
    recent: deque = field(default_factory=lambda: deque(maxlen=REPLAY_SIZE))


class Hub:
    """Подписки открытых вкладок. Живёт в памяти процесса `api`.

    В памяти, а не в базе, потому что подписка — это открытое соединение,
    а оно и так принадлежит процессу. Когда процессов станет несколько,
    сюда встанет LISTEN/NOTIFY Postgres, и форма событий не изменится.
    """

    def __init__(self) -> None:
        self._users: dict[uuid.UUID, _UserState] = {}
        self._next_id = 0

    def count(self, user_id: uuid.UUID) -> int:
        state = self._users.get(user_id)
        return len(state.queues) if state else 0

    def subscribe(self, user_id: uuid.UUID) -> asyncio.Queue:
        state = self._users.setdefault(user_id, _UserState())
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        state.queues.append(queue)
        return queue

    def unsubscribe(self, user_id: uuid.UUID, queue: asyncio.Queue) -> None:
        state = self._users.get(user_id)
        if state is None:
            return
        if queue in state.queues:
            state.queues.remove(queue)
        if not state.queues and not state.recent:
            self._users.pop(user_id, None)

    def replay(self, user_id: uuid.UUID, after_id: int | None) -> list[UiEvent]:
        """События, которые вкладка пропустила за время разрыва."""
        if after_id is None:
            return []
        state = self._users.get(user_id)
        if state is None:
            return []
        return [event for event in state.recent if event.id > after_id]

    def publish(self, user_id: uuid.UUID, type_: str, data: dict) -> UiEvent:
        self._next_id += 1
        event = UiEvent(id=self._next_id, type=type_, data=data)
        state = self._users.setdefault(user_id, _UserState())
        state.recent.append(event)
        for queue in list(state.queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Вкладка не читает. Выкидываем её событие, а не копим:
                # `today` остаётся полным источником истины, и она догонит.
                log.debug("очередь вкладки переполнена, событие пропущено")
        return event


# Единственный хаб процесса. Модуль, а не глобальная переменная в api.py,
# потому что писать в него будут из трёх мест: консьюмера, потока и сверки.
hub = Hub()


# Внутреннее событие → событие экрана. Шесть типов из Архитектуры ч.2 §2
# плюс живая строка открытой сделки, которой на момент написания той главы
# ещё не было.
UI_TYPES: dict[str, str] = {
    ev.TRADES_INGESTED: "trade_ingested",
    # «Появилась открытая сделка» и «у открытой сделки изменились числа» —
    # два разных события, и раньше они ходили под одним именем. Первое значит
    # «перечитай ленту»: у новой строки есть плечо, размер и теги, которых
    # в событии нет. Второе значит «подмени два числа» и ленту не трогает.
    ev.TRADES_OPENED: "trade_opened",
    ev.TRADES_MARKING_CHANGED: "trade_marked",
    ev.INCIDENTS_LOCK_STARTED: "lock_started",
    ev.INCIDENTS_LOCK_LIFTED: "lock_lifted",
    ev.INCIDENTS_LOCK_BREACHED: "lock_breached",
    ev.INCIDENTS_OPENED: "incident_opened",
    ev.STREAKS_CHANGED: "streak_changed",
    ev.SOURCE_STREAM_LOST: "sync_state",
}


async def on_event(event: bus.Event) -> None:
    """Консьюмер `ui_push`: доменное событие → событие экрана."""
    ui_type = UI_TYPES.get(event.type)
    if ui_type is None:
        return
    raw = event.payload.get("user_id")
    if not raw:
        return
    payload = {k: v for k, v in event.payload.items() if k != "user_id"}
    hub.publish(uuid.UUID(str(raw)), ui_type, payload)


def consumer() -> bus.Consumer:
    return bus.Consumer("ui_push", on_event, types=tuple(UI_TYPES))


def push_open_trade(user_id: uuid.UUID, trade: dict) -> None:
    """Живые числа открытой позиции — мимо шины, прямо на экран.

    См. шапку файла: в `outbox` идёт факт «позиция открылась», а не её цена
    в каждую секунду. Отсюда и отдельное имя события: `trade_opened` означает
    «перечитай ленту», а тик — «подмени числа в строке, которая уже стоит».
    """
    hub.publish(user_id, "open_trade_tick", trade)


def push_sync_state(user_id: uuid.UUID, state: dict) -> None:
    """Состояние связи с источником.

    Отдельный вход по той же причине: «поток жив» — это не событие предметной
    области, а свойство соединения, и в историю сервиса ему не место.
    """
    hub.publish(user_id, "sync_state", state)


def heartbeat() -> str:
    """Комментарий SSE. Данных не несёт, держит соединение открытым."""
    return f":ka {dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}\n\n"


async def frames(
    user_id: uuid.UUID,
    queue: asyncio.Queue,
    missed: Iterable[UiEvent] = (),
    disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """Кадры SSE одной вкладки.

    Вынесено из эндпоинта не ради красоты, а чтобы это вообще можно было
    проверить тестом: транспорт httpx дочитывает ответ до конца, прежде чем
    вернуть его, а поток по определению не кончается — тест на живом
    соединении просто висит до таймаута и ничего не доказывает.

    Подписку делает вызывающий, а не эта функция. Иначе между ответом «200»
    и первым шагом генератора была бы щель, в которую проваливались бы события,
    и проверка лимита соединений считала бы вкладки, которых ещё нет.
    """
    try:
        for event in missed:
            yield event.encode()
        # Первый хартбит сразу: без него вкладка не знает, что соединение
        # состоялось, пока не случится первое событие.
        yield heartbeat()
        while True:
            if disconnected is not None and await disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SEC)
            except TimeoutError:
                yield heartbeat()
                continue
            yield event.encode()
    finally:
        hub.unsubscribe(user_id, queue)
