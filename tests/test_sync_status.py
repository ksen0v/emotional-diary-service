"""Что экран говорит про связь с источником.

Проверяется различие, на котором сервис однажды уже соврал сам себе: тишина
живого потока — это «сделок не было», а не «связи нет». Прежний экран знал
только время последней сверки, и поток, не поднявшийся ни разу, выглядел
на нём ровно как рабочий; прежний транспорт, наоборот, считал тишину обрывом
и рвал здоровое соединение каждые три минуты. Обе ошибки об одном.
"""

import datetime as dt
import uuid
from types import SimpleNamespace

from eds.app import stream as app_stream
from eds.app import today as app_today


class FakeRegistry:
    def __init__(self, row=None):
        self._row = row

    def state_of(self, _connection_id):
        return self._row


def connection(**caps) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), capabilities=caps)


def use_registry(monkeypatch, row) -> None:
    monkeypatch.setattr(app_stream, "registry", FakeRegistry(row))


# --- состояние потока ---


def test_source_without_a_stream_has_no_stream_block(monkeypatch) -> None:
    """У источника без потока отсутствие потока — норма, а не новость."""
    use_registry(monkeypatch, None)
    assert app_today._stream_block(connection(provides_stream=False)) is None


def test_stream_that_was_never_lifted_says_so(monkeypatch) -> None:
    use_registry(monkeypatch, None)
    block = app_today._stream_block(connection(provides_stream=True))
    assert block == {
        "expected": True,
        "connected": False,
        "opened_at": None,
        "last_event_at": None,
        "reconnects": 0,
        "last_error": "поток ещё не поднят",
    }


def test_a_running_stream_counts_even_without_the_capability(monkeypatch) -> None:
    """Подключения, созданные до появления флага, не должны выглядеть мёртвыми.

    В их `capabilities` флага нет, и без этой поблажки статус пришлось бы
    «оживлять» пересохранением ключа — то есть просить трейдера сделать
    бессмысленное действие из-за нашей миграции.
    """
    use_registry(
        monkeypatch,
        {
            "connection_id": "x",
            "connected": True,
            "opened_at": "2026-09-25T10:31:39+00:00",
            "last_event_at": None,
            "reconnects": 0,
            "last_error": None,
        },
    )
    block = app_today._stream_block(connection())
    assert block is not None
    assert block["expected"] is True
    assert block["connected"] is True


# --- когда сервис в последний раз что-то знал ---


def test_open_stream_counts_as_contact() -> None:
    """Открытое соединение — это связь, даже если событий по нему не было."""
    opened = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30)
    run = SimpleNamespace(started_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2))
    contact = app_today._last_contact(
        run,
        {
            "expected": True,
            "connected": True,
            "opened_at": opened.isoformat(),
            "last_event_at": None,
            "reconnects": 0,
            "last_error": None,
        },
    )
    assert contact is not None
    assert (dt.datetime.now(dt.UTC) - contact).total_seconds() < 60


def test_closed_stream_does_not_count_as_contact() -> None:
    """Неподнятое соединение связью не считается, сколько бы его ни открывали."""
    run_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=20)
    contact = app_today._last_contact(
        SimpleNamespace(started_at=run_at),
        {
            "expected": True,
            "connected": False,
            "opened_at": dt.datetime.now(dt.UTC).isoformat(),
            "last_event_at": None,
            "reconnects": 3,
            "last_error": "поток оборвался",
        },
    )
    assert contact == run_at


def test_no_contact_at_all_is_unknown_not_zero() -> None:
    assert app_today._last_contact(None, None) is None


# --- алерт про простой (ТЗ 9.6) ---


def source(idle: int | None, *, connected: bool | None) -> dict:
    stream = (
        None
        if connected is None
        else {
            "expected": True,
            "connected": connected,
            "opened_at": None,
            "last_event_at": None,
            "reconnects": 0,
            "last_error": None,
        }
    )
    return {"stream": stream, "idle_sec": idle}


def test_idle_longer_than_five_minutes_is_an_alert() -> None:
    alert = app_today._idle_alert(source(7 * 60, connected=False), True)
    assert alert is not None
    assert alert["code"] == "source_idle"
    # Число в тексте согласовано: «7 минут», а не «7 минуту».
    assert "7 минут" in alert["message"]


def test_quiet_but_connected_stream_is_not_an_alert() -> None:
    """Главное различие. Иначе алерт горел бы весь день у любого, кто торгует
    не каждую минуту, и его перестали бы читать."""
    assert app_today._idle_alert(source(3 * 3600, connected=True), True) is None


def test_no_alert_outside_a_session() -> None:
    """Вне сессии тишина ничего не значит — трейдер не торгует (ТЗ 9.6)."""
    assert app_today._idle_alert(source(7 * 60, connected=False), False) is None


def test_short_silence_is_not_an_alert() -> None:
    assert app_today._idle_alert(source(60, connected=False), True) is None


def test_unknown_idle_is_not_an_alert() -> None:
    """Ничего не знаем — молчим. Тревога «на всякий случай» обесценивает все."""
    assert app_today._idle_alert(source(None, connected=False), True) is None
