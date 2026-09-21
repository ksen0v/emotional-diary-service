"""Перевод данных TMM — самое рискованное место шага 4.

Спецификацию TMM целиком прочитать не удалось, поэтому проверяется не «как
написано в документации», а то, что при любой из двух возможных трактовок поля
мы не получим тихо неверное число: миллисекунды не станут секундами, процент
от депозита не подменится процентом от входа, тег выхода не станет тегом входа.
"""

import datetime as dt
from decimal import Decimal

import pytest

from eds.modules.source.adapters.tmm import mapping

COLUMNS = [
    {"id": 1, "key": "entry_reason", "name": "Entry reasons"},
    {"id": 2, "key": "exit_reason", "name": "Exit reasons"},
    {"id": 10, "name": "Rating"},  # колонка заметок, ключа нет
]

TAGS = [
    {"id": 108554, "name": "НЕ СИСТЕМНАЯ ТОРГОВЛЯ", "columnId": 1},
    {"id": 141017, "name": "Sc. 1.1", "columnId": 1},
    {"id": 700001, "name": "СТОП", "columnId": 2},
    {"id": 136924, "name": "*", "columnId": 10},
]

OPEN_MS = 1758400000000
CLOSE_MS = 1758400300000


def trade_row(**over: object) -> dict:
    row = {
        "id": 900001,
        "api_key_id": 217071,
        "symbol": "btcusdt",
        "side": "SELL",
        "net_profit": -84.2,
        "percent": -1.68,
        "profit_deposit": -0.68,
        "volume": 5000.0,
        "leverage": 10,
        "open_time": OPEN_MS,
        "close_time": CLOSE_MS,
        "duration": 300000,
        "process": False,
        "tags": [{"id": 108554, "name": "НЕ СИСТЕМНАЯ ТОРГОВЛЯ"}],
        "commission": 0.5,
    }
    row.update(over)
    return row


def dictionary() -> mapping.TagDictionary:
    return mapping.tag_dictionary(TAGS, COLUMNS)


def test_milliseconds_are_not_read_as_seconds() -> None:
    assert mapping.to_datetime(OPEN_MS) == dt.datetime(
        2025, 9, 20, 20, 26, 40, tzinfo=dt.UTC
    )
    # Секунды тоже понимаем: порог отделяет одно от другого однозначно.
    assert mapping.to_datetime(OPEN_MS // 1000) == mapping.to_datetime(OPEN_MS)


def test_account_return_pct_is_profit_deposit() -> None:
    """Процент от депозита — основание всех правил, и брать надо именно его.

    В payload есть два процента: `percent` — от цены входа, `profit_deposit` —
    от депозита. Подмена одного другим не сломает ни один запрос и не появится
    в логах: сломаются только пороги правил, причём в несколько раз.
    """
    trade, _ = mapping.trade_from_row(trade_row(), dictionary())
    assert trade.account_return_pct == Decimal("-0.68")
    assert trade.percent == Decimal("-1.68")


def test_missing_profit_deposit_is_an_error_not_zero() -> None:
    row = trade_row()
    del row["profit_deposit"]
    with pytest.raises(mapping.MappingError):
        mapping.trade_from_row(row, dictionary())


def test_side_and_symbol_normalised() -> None:
    trade, _ = mapping.trade_from_row(trade_row(), dictionary())
    assert trade.side == "short"
    assert trade.symbol == "BTCUSDT"
    assert mapping.side_of("BUY") == "long"
    with pytest.raises(mapping.MappingError):
        mapping.side_of("flat")


def test_duration_is_computed_from_timestamps() -> None:
    """Единицы `duration` не подтверждены, поэтому считаем по двум меткам."""
    trade, _ = mapping.trade_from_row(trade_row(duration=300), dictionary())
    assert trade.duration_sec == 300
    trade, _ = mapping.trade_from_row(trade_row(duration=999999), dictionary())
    assert trade.duration_sec == 300


def test_only_entry_tags_reach_marking() -> None:
    row = trade_row(
        tags=[
            {"id": 108554, "name": "НЕ СИСТЕМНАЯ ТОРГОВЛЯ"},
            {"id": 700001, "name": "СТОП"},
            {"id": 136924, "name": "*"},
        ]
    )
    trade, unknown = mapping.trade_from_row(row, dictionary())
    assert [t.name for t in trade.tags] == ["НЕ СИСТЕМНАЯ ТОРГОВЛЯ"]
    assert unknown == []


def test_unknown_tag_is_reported_not_swallowed() -> None:
    """Проглоченный тег входа — это незамеченное нарушение."""
    trade, unknown = mapping.trade_from_row(
        trade_row(tags=[{"id": 999999, "name": "новый"}]), dictionary()
    )
    assert trade.tags == ()
    assert unknown == ["999999"]


def test_tag_ids_accept_all_likely_shapes() -> None:
    assert mapping.tag_ids_of({"tags": [108554]}) == ["108554"]
    assert mapping.tag_ids_of({"tags": ["108554"]}) == ["108554"]
    assert mapping.tag_ids_of({"tags": [{"id": 108554}]}) == ["108554"]
    assert mapping.tag_ids_of({"tag_ids": [108554]}) == ["108554"]
    assert mapping.tag_ids_of({}) == []


def test_columns_without_key_give_no_entry_tags() -> None:
    """Если ключи колонок не пришли, колонка входа не угадывается."""
    blind = mapping.tag_dictionary(TAGS, [{"id": 1, "name": "Entry reasons"}])
    assert blind.entry_tags() == []
    assert blind.knows("108554") is True


def test_open_trade_detected_by_process_and_by_missing_close() -> None:
    assert mapping.is_open(trade_row(process=True)) is True
    row = trade_row()
    row["close_time"] = None
    assert mapping.is_open(row) is True
    assert mapping.is_open(trade_row()) is False


def test_account_row_mapped_from_api_key_record() -> None:
    account = mapping.account_from_row(
        {
            "id": 217071,
            "name": "Binance Main",
            "exchange": "Binance Futures",
            "market": "futures",
            "key": "R3KY********p2a9",
            "state": "connected",
        }
    )
    assert account.external_id == "217071"
    assert account.name == "Binance Main"
    assert account.market == "futures"
