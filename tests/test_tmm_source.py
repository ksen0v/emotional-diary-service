"""Источник TMM: счета, словарь тегов, окно сверки.

Проверяется в том числе поведение при отсутствии адреса списка ключей: путь
взят из общего описания API, живым ответом не подтверждён, и подключение
не должно ломаться, если его нет.
"""

import datetime as dt

import httpx
import respx

from eds.modules.source.adapters.tmm.rest import BASE_URLS, TmmClient
from eds.modules.source.adapters.tmm.source import PAGE_SIZE, TmmSource
from tests.test_tmm_mapping import COLUMNS, TAGS, trade_row

PRIMARY = BASE_URLS[0]


def mock_dictionary(tags: list[dict] | None = None, columns: list[dict] | None = None) -> None:
    respx.get(f"{PRIMARY}/trades/tags").mock(
        return_value=httpx.Response(200, json=tags if tags is not None else TAGS)
    )
    respx.get(f"{PRIMARY}/trades/tag-categories").mock(
        return_value=httpx.Response(
            200, json=columns if columns is not None else COLUMNS
        )
    )


def source() -> tuple[TmmSource, TmmClient]:
    client = TmmClient("test-key-123456")
    return TmmSource(client), client


def ms(when: dt.datetime) -> int:
    return int(when.timestamp() * 1000)


@respx.mock
async def test_accounts_from_key_list() -> None:
    respx.get(f"{PRIMARY}/api-key").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 217071,
                    "name": "Binance Main",
                    "exchange": "Binance Futures",
                    "market": "futures",
                }
            ],
        )
    )
    tmm, client = source()
    async with client:
        accounts = await tmm.fetch_accounts()
    assert [a.external_id for a in accounts] == ["217071"]
    assert tmm.accounts_from_trades is False


@respx.mock
async def test_accounts_fall_back_to_trades_when_endpoint_missing() -> None:
    respx.get(f"{PRIMARY}/api-key").mock(return_value=httpx.Response(404))
    mock_dictionary()
    respx.get(f"{PRIMARY}/trades/").mock(
        return_value=httpx.Response(200, json=[trade_row(), trade_row(id=2)])
    )
    tmm, client = source()
    async with client:
        accounts = await tmm.fetch_accounts()
    assert [a.external_id for a in accounts] == ["217071"]
    assert tmm.accounts_from_trades is True


@respx.mock
async def test_tags_available_only_when_column_key_known() -> None:
    mock_dictionary()
    tmm, client = source()
    async with client:
        tags = await tmm.fetch_tags()
    assert sorted(t.name for t in tags) == ["Sc. 1.1", "НЕ СИСТЕМНАЯ ТОРГОВЛЯ"]
    assert tmm.capabilities().provides_tags is True


@respx.mock
async def test_without_column_keys_source_declares_itself_without_tags() -> None:
    """Смешивать колонки нельзя: тег выхода сделал бы сделку «по системе»."""
    mock_dictionary(columns=[{"id": 1, "name": "Entry reasons"}])
    tmm, client = source()
    async with client:
        assert await tmm.fetch_tags() == []
    assert tmm.capabilities().provides_tags is False
    assert tmm.tags_problem is not None


@respx.mock
async def test_window_is_checked_on_our_side() -> None:
    """Провайдер может фильтр проигнорировать — окно всё равно соблюдается."""
    now = dt.datetime.now(dt.UTC)
    inside = trade_row(
        id=1, open_time=ms(now - dt.timedelta(minutes=20)), close_time=ms(now)
    )
    outside = trade_row(
        id=2,
        open_time=ms(now - dt.timedelta(days=9)),
        close_time=ms(now - dt.timedelta(days=9)),
    )
    mock_dictionary()
    respx.get(f"{PRIMARY}/trades/").mock(
        return_value=httpx.Response(200, json=[inside, outside])
    )
    tmm, client = source()
    async with client:
        trades = await tmm.fetch_trades(since=now - dt.timedelta(hours=1))
    assert [t.external_id for t in trades] == ["1"]
    assert tmm.window_filter_honored is False


@respx.mock
async def test_second_page_is_read_when_first_is_full() -> None:
    now = dt.datetime.now(dt.UTC)
    full = [
        trade_row(id=i, open_time=ms(now), close_time=ms(now))
        for i in range(PAGE_SIZE)
    ]
    mock_dictionary()
    respx.get(f"{PRIMARY}/trades/").mock(
        side_effect=[
            httpx.Response(200, json=full),
            httpx.Response(200, json=[trade_row(id=9999, open_time=ms(now), close_time=ms(now))]),
        ]
    )
    tmm, client = source()
    async with client:
        trades = await tmm.fetch_trades(since=now - dt.timedelta(hours=1))
    assert len(trades) == PAGE_SIZE + 1
    assert tmm.pages_read == 2


@respx.mock
async def test_unknown_tag_makes_dictionary_reread_once() -> None:
    now = dt.datetime.now(dt.UTC)
    fresh = TAGS + [{"id": 999999, "name": "НОВЫЙ ТЕГ", "columnId": 1}]
    respx.get(f"{PRIMARY}/trades/tags").mock(
        side_effect=[
            httpx.Response(200, json=TAGS),
            httpx.Response(200, json=fresh),
        ]
    )
    respx.get(f"{PRIMARY}/trades/tag-categories").mock(
        return_value=httpx.Response(200, json=COLUMNS)
    )
    respx.get(f"{PRIMARY}/trades/").mock(
        return_value=httpx.Response(
            200,
            json=[
                trade_row(
                    open_time=ms(now),
                    close_time=ms(now),
                    tags=[{"id": 999999, "name": "НОВЫЙ ТЕГ"}],
                )
            ],
        )
    )
    tmm, client = source()
    async with client:
        trades = await tmm.fetch_trades(since=now - dt.timedelta(hours=1))
    assert [t.name for t in trades[0].tags] == ["НОВЫЙ ТЕГ"]


@respx.mock
async def test_truncated_payload_is_read_again() -> None:
    now = dt.datetime.now(dt.UTC)
    short = trade_row(
        id=55, open_time=ms(now), close_time=ms(now), payload_truncated=True
    )
    full = trade_row(id=55, open_time=ms(now), close_time=ms(now), symbol="ETHUSDT")
    mock_dictionary()
    respx.get(f"{PRIMARY}/trades/").mock(return_value=httpx.Response(200, json=[short]))
    respx.get(f"{PRIMARY}/trades/55").mock(return_value=httpx.Response(200, json=full))
    tmm, client = source()
    async with client:
        trades = await tmm.fetch_trades(since=now - dt.timedelta(hours=1))
    assert trades[0].symbol == "ETHUSDT"


@respx.mock
async def test_probe_does_not_page_and_sorts_newest_first() -> None:
    now = dt.datetime.now(dt.UTC)
    older = trade_row(
        id=1,
        open_time=ms(now - dt.timedelta(hours=5)),
        close_time=ms(now - dt.timedelta(hours=5)),
    )
    newer = trade_row(id=2, open_time=ms(now), close_time=ms(now))
    mock_dictionary()
    route = respx.get(f"{PRIMARY}/trades/").mock(
        return_value=httpx.Response(200, json=[older, newer])
    )
    tmm, client = source()
    async with client:
        trades = await tmm.probe_trades(days=30)
    assert [t.external_id for t in trades] == ["2", "1"]
    assert route.call_count == 1
