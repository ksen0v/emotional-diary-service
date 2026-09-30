"""Транспорт Telegram: что считается живой связью и что — отказом.

Этот файл — прямое следствие шага «Живое обновление». Там три ошибки подряд
дожили до живого ключа, потому что транспорт не был покрыт тестами: проверялось,
что делать с пришедшим кадром, и ничего — про то, когда соединение считается
мёртвым. Telegram такая же чужая сеть, поэтому здесь проверяется именно это:

- пустой ответ long polling — **признак живой связи**, а не её отсутствия;
- пауза сбрасывается по факту успешного запроса, а не по первому сообщению;
- `offset` двигается после обработки, то есть служит подтверждением;
- 401, 409 и 429 — три разных отказа с тремя разными ответами.

Сети здесь нет. Поддельный клиент поднимает настоящие исключения aiogram —
проверяется наше решение, что с ними делать, а не работа Telegram.
"""

import asyncio

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates

from eds.modules.notifications.telegram import client as client_mod
from eds.modules.notifications.telegram import poller as poller_mod
from eds.modules.notifications.telegram.client import (
    AUTH,
    CONFLICT,
    PERMANENT,
    RETRY,
    WAIT,
    BotClient,
    classify,
    confirm_keyboard,
)
from eds.modules.notifications.telegram.poller import Poller


class FakeApi:
    """Клиент Telegram, который отдаёт заготовленное или падает заготовленным."""

    def __init__(self, batches=()):
        self._batches = list(batches)
        self.calls: list[dict] = []
        self.sent: list[tuple[int, str, dict | None]] = []
        self.me = {"id": 42, "username": "eds_test_bot", "first_name": "EDS"}

    async def get_updates(self, **kwargs):
        self.calls.append(kwargs)
        if not self._batches:
            # Спокойный опрос: сервер додержал запрос и ответил пустотой.
            # Настоящий getUpdates держит соединение до таймаута, поэтому и
            # здесь пауза есть — иначе цикл крутился бы без единой уступки
            # управления, чего в жизни не бывает.
            await asyncio.sleep(0.01)
            return []
        await asyncio.sleep(0)
        item = self._batches.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def get_me(self):
        return self.me

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": len(self.sent)}

    async def answer_callback_query(self, **kwargs):
        return True


def retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=GetUpdates(), message="Too Many Requests", retry_after=seconds
    )


def api_error(kind, message: str):
    return kind(method=GetUpdates(), message=message)


# --- разбор отказов ---


def test_every_failure_has_its_own_verdict() -> None:
    """Пять видов отказа, и путать их нельзя.

    «Друг заблокировал бота» повторять бессмысленно ни сейчас, ни через час.
    «Telegram не ответил» — ровно наоборот. Один общий `except` стоил бы
    либо потерянных уведомлений, либо бесконечных повторов в пустоту.
    """
    assert classify(retry_after(7)).kind == WAIT
    assert classify(retry_after(7)).retry_after == 7.0
    assert classify(api_error(TelegramUnauthorizedError, "Unauthorized")).kind == AUTH
    assert classify(api_error(TelegramConflictError, "terminated by other")).kind == CONFLICT
    assert classify(api_error(TelegramForbiddenError, "blocked")).kind == PERMANENT
    assert classify(api_error(TelegramBadRequest, "chat not found")).kind == PERMANENT
    assert classify(api_error(TelegramServerError, "502")).kind == RETRY
    assert classify(TelegramNetworkError(method=GetUpdates(), message="reset")).kind == RETRY
    assert classify(TimeoutError()).kind == RETRY


def test_conflict_names_the_reason() -> None:
    """409 — не тишина, а «тебя опрашивает кто-то ещё».

    Снаружи это выглядит как «бот не отвечает», и без внятного текста разбор
    занял бы вечер. Поэтому причина названа словами, а не кодом.
    """
    failure = classify(api_error(TelegramConflictError, "terminated by other getUpdates"))
    assert "другой процесс" in failure.message
    assert "webhook" in failure.message


async def test_read_timeout_is_longer_than_poll_timeout() -> None:
    """Свой таймаут чтения заведомо больше, чем держит сервер.

    Если сделать его меньше или равным, каждый спокойный опрос выглядел бы
    обрывом, и сервис переподключался бы на ровном месте — та самая ошибка,
    которая рвала сокет Binance каждые три минуты, только с другой стороны.
    """
    assert client_mod.READ_TIMEOUT_SEC > client_mod.POLL_TIMEOUT_SEC

    api = FakeApi()
    client = BotClient("t", api=api)
    await client.get_updates(offset=None)
    assert api.calls[0]["timeout"] == client_mod.POLL_TIMEOUT_SEC
    assert api.calls[0]["request_timeout"] > client_mod.POLL_TIMEOUT_SEC
    # Лишнего не просим: чужие типы обновлений нам не нужны и не приходят.
    assert api.calls[0]["allowed_updates"] == ["message", "callback_query"]


async def test_client_turns_library_objects_into_plain_dicts() -> None:
    client = BotClient("t", api=FakeApi([[{"update_id": 1, "message": {"text": "/start"}}]]))
    updates = await client.get_updates(offset=None)
    assert updates == [{"update_id": 1, "message": {"text": "/start"}}]


async def test_identity_comes_from_telegram() -> None:
    """Имя бота спрашивается у Telegram, а не придумывается.

    Оно показывается на экране и уходит в ссылку-приглашение: выдуманное имя
    в ссылке — это приглашение, которое никуда не ведёт.
    """
    client = BotClient("t", api=FakeApi())
    identity = await client.me()
    assert identity.username == "eds_test_bot"
    assert identity.id == 42


def test_confirm_button_carries_the_lock() -> None:
    """Кнопка подтверждения относится к конкретной блокировке, а не к «последней».

    Иначе нажатие, пришедшее через час, сняло бы блокировку, о которой друга
    никто не спрашивал.
    """
    keyboard = confirm_keyboard("9f2c")
    button = keyboard["inline_keyboard"][0][0]
    assert button["callback_data"] == "lift:9f2c"


# --- цикл опроса ---


class Recorder:
    """Собирает обработанные обновления и умеет падать на заданных."""

    def __init__(self, fail_on: set[int] | None = None, fail_times: int = 99):
        self.seen: list[int] = []
        self.fail_on = fail_on or set()
        self.fail_times = fail_times
        self.attempts: dict[int, int] = {}

    async def __call__(self, update: dict) -> None:
        update_id = update["update_id"]
        self.attempts[update_id] = self.attempts.get(update_id, 0) + 1
        if update_id in self.fail_on and self.attempts[update_id] <= self.fail_times:
            raise RuntimeError("обработчик упал")
        self.seen.append(update_id)


def make_poller(batches, handler=None) -> tuple[Poller, list[float]]:
    """Поллер с поддельным клиентом и записанными паузами вместо настоящих.

    Заодно записывается состояние на момент паузы: после остановки цикл
    всегда `stopped`, и увидеть, чем он был занят, можно только изнутри.
    """
    poller = Poller(BotClient("t", api=FakeApi(batches)), handler or Recorder())
    slept: list[float] = []
    states: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        states.append(poller.state)
        if len(slept) > 20:  # pragma: no cover — страховка от зацикливания теста
            poller.stop()

    poller._sleep = fake_sleep  # type: ignore[method-assign]
    poller.states_at_pause = states  # type: ignore[attr-defined]
    return poller, slept


async def run_until_idle(poller: Poller, stop_after: float = 0.05) -> None:
    """Дать циклу поработать и остановить его, как это делает выключение сервиса."""
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(stop_after)
    poller.stop()
    await asyncio.wait_for(task, timeout=2)


async def test_empty_answer_is_a_live_connection() -> None:
    """Пустой ответ — это контакт, а не молчание.

    Прикладного сторожа «давно ничего не приходило» здесь нет и быть не
    может: у бота молчание пользователей нормально по протоколу. Ровно такой
    сторож рвал живой сокет биржи каждые три минуты.
    """
    poller, slept = make_poller([[], [], []])
    await run_until_idle(poller)

    # Цикл дожил до остановки сам, ни разу не решив, что связь мертва.
    assert poller.state == poller_mod.STOPPED
    assert poller.last_error is None
    assert poller.last_ok_at is not None
    assert poller.backoff == poller_mod.BACKOFF_START
    assert slept == [], "пустой ответ не должен вызывать паузу"


async def test_pause_resets_on_a_successful_request_not_on_a_message() -> None:
    """Пауза сбрасывается по факту ответа, даже если обновлений ноль.

    На прошлом шаге она сбрасывалась по первому событию, а у молчащего потока
    событий нет — окно слепоты росло: 14 с → 44 с → 71 с. Здесь то же самое
    проверяется наоборот: два отказа подряд растят паузу, а первый же удачный
    запрос — без единого сообщения — возвращает её к началу.
    """
    poller, slept = make_poller(
        [
            api_error(TelegramServerError, "502"),
            api_error(TelegramServerError, "502"),
            [],
        ]
    )
    await run_until_idle(poller)

    assert slept == [poller_mod.BACKOFF_START, poller_mod.BACKOFF_START * 2]
    assert poller.backoff == poller_mod.BACKOFF_START


async def test_offset_is_an_acknowledgement() -> None:
    """`offset` двигается после обработки, а не до.

    Telegram присылает обновление заново, пока его не подтвердили. Сдвинув
    offset заранее, мы бы теряли то, на чём упали, — и терялось бы именно то,
    что не удалось обработать, то есть самое важное.
    """
    handler = Recorder()
    poller, _ = make_poller([[{"update_id": 10}, {"update_id": 11}], []], handler)
    await run_until_idle(poller)

    assert handler.seen == [10, 11]
    assert poller.offset == 12


async def test_failed_update_is_retried_and_then_skipped() -> None:
    """Одно плохое обновление не останавливает бота навсегда.

    Пока попытки не исчерпаны, offset стоит на месте — Telegram пришлёт то же
    самое снова. После трёх неудач обновление пропускается с громкой записью:
    вечный цикл по одному событию — это бот, который не видит ничего другого.
    """
    handler = Recorder(fail_on={7})
    poller, _ = make_poller(
        [[{"update_id": 7}], [{"update_id": 7}], [{"update_id": 7}], []], handler
    )
    await run_until_idle(poller)

    assert handler.attempts[7] == poller_mod.MAX_UPDATE_ATTEMPTS
    assert poller.offset == 8, "после исчерпания попыток обновление пропускается"
    assert poller.errors_total == poller_mod.MAX_UPDATE_ATTEMPTS


async def test_retry_after_is_obeyed_exactly() -> None:
    """429 — это не «повтори когда-нибудь», а «подожди столько-то».

    Свой бэкофф здесь не применяется: Telegram назвал число, и упорство
    вместо ожидания кончается баном.
    """
    poller, slept = make_poller([retry_after(17), []])
    await run_until_idle(poller)

    assert slept == [17.0]
    assert poller.backoff == poller_mod.BACKOFF_START


async def test_bad_token_stops_the_loop() -> None:
    """401 — конец цикла, а не повтор.

    С неверным токеном повторять бессмысленно, а Telegram за упорство
    наказывает. Состояние видно снаружи: карточка настроек должна сказать,
    что токен не принят, а не «бот запускается» вечно.
    """
    poller, slept = make_poller([api_error(TelegramUnauthorizedError, "Unauthorized")])
    await asyncio.wait_for(poller.run(), timeout=2)

    assert poller.state == poller_mod.AUTH_ERROR
    assert slept == []
    assert poller.last_error


async def test_conflict_keeps_trying_but_says_so() -> None:
    """409 не останавливает бота, но перестаёт выглядеть как тишина."""
    poller, slept = make_poller(
        [api_error(TelegramConflictError, "terminated by other getUpdates"), []]
    )
    await run_until_idle(poller)

    assert slept == [poller_mod.BACKOFF_START]
    assert poller.snapshot()["updates"] == 0
    # На момент паузы состояние названо своим именем, а не «повторяем»:
    # снаружи конфликт выглядит как тишина, и отличить его можно только тут.
    assert poller.states_at_pause == [poller_mod.CONFLICT_STATE]
    # И это не конец: следующий запрос прошёл, связь восстановилась.
    assert poller.last_ok_at is not None


async def test_backoff_has_a_ceiling() -> None:
    """Пауза растёт, но не до бесконечности.

    На прошлом шаге она росла и не сбрасывалась, и к концу часа сервис был
    слеп по минуте из каждых трёх. Потолок — то, что делает слепоту
    ограниченной даже в худшем случае.
    """
    poller, slept = make_poller([api_error(TelegramServerError, "502")] * 12)
    await run_until_idle(poller, stop_after=0.1)

    assert max(slept) <= poller_mod.BACKOFF_MAX
    assert poller.backoff <= poller_mod.BACKOFF_MAX
