"""Транспорт потока Binance: подъём, тишина, переподключение, ключ.

Этих тестов не было, и поэтому первая живая проверка Binance нашла три ошибки
подряд, которых не видел ни один из 388 зелёных тестов. Все три были не в том,
как разбираются события, а в том, когда соединение считается мёртвым, — то есть
ровно в той части, которую «проверю руками» не проверяет: чтобы увидеть их
вживую, надо смотреть в лог час.

Сеть здесь поддельная. Настоящую биржу из контейнера не достать, а если бы
и достать — тест на живом соединении проверял бы Binance, а не наш код.
"""

import asyncio

import pytest

from eds.modules.source.adapters.binance import ws as ws_mod
from eds.modules.source.adapters.binance.ws import UserDataStream


class FakeSocket:
    """Сокет, который отдаёт заготовленные кадры, а потом молчит.

    Молчит — значит `recv()` не возвращается. Это нормальное состояние
    соединения, когда трейдер не торгует, и именно его прежний сторож
    принимал за обрыв.
    """

    def __init__(self, frames=(), *, then=None):
        self._frames = list(frames)
        self._then = then
        self.closed = False
        self.recv_calls = 0

    async def recv(self) -> str:
        self.recv_calls += 1
        if self._frames:
            return self._frames.pop(0)
        if self._then is not None:
            raise self._then
        await asyncio.sleep(3600)
        raise AssertionError("недостижимо")

    async def close(self) -> None:
        self.closed = True


class FakeConnect:
    """Подделка `websockets.connect`: и awaitable, и менеджер контекста."""

    def __init__(self, sockets):
        self._sockets = list(sockets)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        socket = self._sockets.pop(0) if self._sockets else FakeSocket()
        return _Opening(socket)

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


class _Opening:
    def __init__(self, socket: FakeSocket):
        self.socket = socket

    def __await__(self):
        async def ready():
            return self.socket

        return ready().__await__()

    async def __aenter__(self) -> FakeSocket:
        return self.socket

    async def __aexit__(self, *exc) -> bool:
        await self.socket.close()
        return False


class FakeClient:
    """Клиент REST: выдаёт ключи потока и считает вызовы."""

    def __init__(self, keys=("key-1", "key-2", "key-3")):
        self._keys = list(keys)
        self.calls: list[tuple[str, str]] = []
        self.keepalive_error: Exception | None = None

    async def keyed(self, endpoint: str, method: str = "GET") -> dict:
        self.calls.append((endpoint, method))
        if method == "POST":
            return {"listenKey": self._keys.pop(0) if self._keys else "key-last"}
        if method == "PUT" and self.keepalive_error is not None:
            raise self.keepalive_error
        return {}

    def counted(self, method: str) -> int:
        return sum(1 for _, m in self.calls if m == method)


def order_frame(order_id: int = 1) -> str:
    return (
        '{"e":"ORDER_TRADE_UPDATE","o":{"x":"TRADE","s":"BTCUSDT",'
        f'"t":{order_id},"i":{order_id}}}}}'
    )


async def collect(stream: UserDataStream, count: int, *, timeout: float = 2.0) -> list:
    """Взять `count` событий и остановить поток. Без таймаута тест повис бы."""
    out: list = []

    async def pump() -> None:
        async for event in stream.events():
            out.append(event)
            if len(out) >= count:
                stream.stop()
                return

    await asyncio.wait_for(pump(), timeout=timeout)
    return out


# --- подъём соединения ---


async def test_connection_is_opened_with_the_key_and_with_ping_pong() -> None:
    """Живость соединения — это ping/pong библиотеки, а не наш сторож.

    Утверждение про параметры здесь не формальность: как только их не станет,
    вернётся тот самый случай, когда мёртвый сокет молчит и выглядит живым,
    а живой молчит и выглядит мёртвым.
    """
    client = FakeClient()
    connect = FakeConnect([FakeSocket([order_frame()])])
    stream = UserDataStream(client, ws_base="wss://test/ws", connect=connect)

    await collect(stream, 1)

    url, kwargs = connect.calls[0]
    assert url == "wss://test/ws/key-1"
    assert kwargs["ping_interval"] == ws_mod.PING_INTERVAL_SEC
    assert kwargs["ping_timeout"] == ws_mod.PING_TIMEOUT_SEC


async def test_diagnostic_probe_opens_the_same_way() -> None:
    """Проверка из настроек и рабочая сессия не должны расходиться."""
    connect = FakeConnect([FakeSocket()])
    stream = UserDataStream(FakeClient(), ws_base="wss://test/ws", connect=connect)

    await stream.probe_socket("key-probe")

    url, kwargs = connect.calls[0]
    assert url == "wss://test/ws/key-probe"
    assert kwargs["ping_interval"] == ws_mod.PING_INTERVAL_SEC


# --- тишина ---


async def test_silence_is_not_a_break() -> None:
    """Молчащий сокет остаётся подключённым.

    Здесь стоял сторож «нет сообщений три минуты — переподключаемся». Он рвал
    здоровое соединение каждые три минуты: Binance шлёт ping, библиотека
    отвечает pong сама, и до `recv()` эти кадры не доходят. Тест проверяет то,
    что тогда было неверно: тишина не заставляет открывать соединение заново.
    """
    client = FakeClient()
    socket = FakeSocket([order_frame()])  # один кадр, дальше тишина
    connect = FakeConnect([socket])
    stream = UserDataStream(client, ws_base="wss://test/ws", connect=connect)

    got = await collect(stream, 1)
    assert len(got) == 1

    # Ждём заметно дольше, чем длится обмен кадрами, и смотрим, не полез ли
    # поток открываться заново.
    await asyncio.sleep(0.2)
    assert len(connect.calls) == 1
    assert client.counted("POST") == 1
    assert stream.reconnects == 0


# --- переподключение ---


async def test_pause_does_not_grow_while_connections_succeed() -> None:
    """Пауза сбрасывается по факту открытия, а не по первому событию.

    Раньше она сбрасывалась событием, а у молчащего потока событий нет —
    и окно слепоты между переподключениями росло с двух секунд до минуты.
    В логе живой проверки это видно прямо: 14 с, 44 с, 71 с.
    """
    dropped = OSError("соединение закрыто биржей")
    connect = FakeConnect([FakeSocket(then=dropped) for _ in range(4)])
    stream = UserDataStream(FakeClient(), ws_base="wss://test/ws", connect=connect)

    pauses: list[float] = []

    async def fake_wait(seconds: float) -> None:
        if seconds == ws_mod.KEEPALIVE_SEC:
            await asyncio.sleep(3600)
            return
        pauses.append(seconds)
        if len(pauses) >= 3:
            stream.stop()

    stream._wait = fake_wait  # type: ignore[method-assign]

    await asyncio.wait_for(collect(stream, 99, timeout=2.0), timeout=3.0)

    assert pauses == [ws_mod.RECONNECT_START_SEC] * 3


async def test_pause_grows_while_the_key_is_not_given() -> None:
    """А вот когда соединение не поднимается вовсе — пауза обязана расти.

    Иначе при недоступной бирже сервис бьётся в неё каждые две секунды.
    """

    class Refusing(FakeClient):
        async def keyed(self, endpoint: str, method: str = "GET") -> dict:
            if method == "POST":
                raise OSError("биржа недоступна")
            return await super().keyed(endpoint, method)

    stream = UserDataStream(
        Refusing(), ws_base="wss://test/ws", connect=FakeConnect([])
    )
    pauses: list[float] = []

    async def fake_wait(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) >= 3:
            stream.stop()

    stream._wait = fake_wait  # type: ignore[method-assign]
    await asyncio.wait_for(collect(stream, 99, timeout=2.0), timeout=3.0)

    assert pauses[1] > pauses[0]
    assert stream.last_error is not None
    assert "ключ потока не получен" in stream.last_error


async def test_broken_connection_reconnects_with_a_fresh_key() -> None:
    """Ключ после обрыва берём новый, а старый закрываем.

    Старый ключ живёт ещё час: оставить его висеть значило бы держать на бирже
    поток, который никто не читает.
    """
    dropped = OSError("соединение закрыто")
    connect = FakeConnect(
        [FakeSocket([order_frame(1)], then=dropped), FakeSocket([order_frame(2)])]
    )
    client = FakeClient()
    stream = UserDataStream(client, ws_base="wss://test/ws", connect=connect)

    async def fake_wait(seconds: float) -> None:
        if seconds == ws_mod.KEEPALIVE_SEC:
            await asyncio.sleep(3600)

    stream._wait = fake_wait  # type: ignore[method-assign]

    got = await collect(stream, 2)

    assert len(got) == 2
    assert connect.urls == ["wss://test/ws/key-1", "wss://test/ws/key-2"]
    assert client.counted("DELETE") >= 1
    assert stream.reconnects == 1


# --- ключ потока ---


async def test_expired_key_reconnects_instead_of_waiting() -> None:
    """`listenKeyExpired` — это предупреждение, а не шум.

    Биржа сейчас закроет соединение. Если не разобрать событие самим, поток
    молча перестанет приносить сделки, а в логе останется закрытие без причины.
    """
    connect = FakeConnect(
        [
            FakeSocket([order_frame(1), '{"e":"listenKeyExpired"}']),
            FakeSocket([order_frame(2)]),
        ]
    )
    stream = UserDataStream(FakeClient(), ws_base="wss://test/ws", connect=connect)

    async def fake_wait(seconds: float) -> None:
        if seconds == ws_mod.KEEPALIVE_SEC:
            await asyncio.sleep(3600)

    stream._wait = fake_wait  # type: ignore[method-assign]

    got = await collect(stream, 2)

    # Само событие наружу не уходит: оно про соединение, а не про сделки.
    assert [e["e"] for e in got] == ["ORDER_TRADE_UPDATE", "ORDER_TRADE_UPDATE"]
    assert connect.urls == ["wss://test/ws/key-1", "wss://test/ws/key-2"]


async def test_failed_keepalive_closes_the_socket() -> None:
    """Непродлённый ключ обязан рвать соединение.

    Здесь был `return`, и это была тихая смерть: ключ переставал действовать,
    сокет оставался открытым, и снаружи поток выглядел работающим.
    """
    socket = FakeSocket()
    client = FakeClient()
    client.keepalive_error = OSError("сеть пропала")
    stream = UserDataStream(
        client, ws_base="wss://test/ws", connect=FakeConnect([socket])
    )
    await stream.open_listen_key()

    async def now(_seconds: float) -> None:
        """Продление ждёт полчаса — в тесте ждать нечего."""

    stream._wait = now  # type: ignore[method-assign]
    await stream._keepalive_loop(socket)

    assert socket.closed is True
    assert stream.last_error is not None
    assert "продление ключа потока не прошло" in stream.last_error


@pytest.mark.parametrize("raw", ["не json", "[]", '"строка"', "null"])
async def test_garbage_frames_are_skipped(raw: str) -> None:
    """Мусорный кадр не должен ронять поток: биржа шлёт и служебные ответы."""
    connect = FakeConnect([FakeSocket([raw, order_frame()])])
    stream = UserDataStream(FakeClient(), ws_base="wss://test/ws", connect=connect)

    got = await collect(stream, 1)
    assert got[0]["e"] == "ORDER_TRADE_UPDATE"
