"""Клиент Binance: подпись, два базовых адреса, вес запросов, баны.

Проверяется то, что на живом API по требованию не воспроизвести: 429 с паузой,
418 с баном адреса, расхождение часов и разрезание окна на семидневные куски.
"""

import datetime as dt
import hashlib
import hmac
import urllib.parse

import httpx
import respx

from eds.modules.source.adapters.binance.rest import (
    FAPI,
    SAPI,
    BinanceBanned,
    BinanceClient,
    windows,
)
from eds.platform.errors import AppError

NOW = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC)
NOW_MS = int(NOW.timestamp() * 1000)
KEY = "binance-key-123456"
SECRET = "binance-secret-abcdef"


def client(**over) -> BinanceClient:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    kwargs = dict(sleep=sleep, clock=lambda: NOW, jitter=lambda: 0.0)
    kwargs.update(over)
    binance = BinanceClient(KEY, SECRET, **kwargs)
    binance.slept = slept  # type: ignore[attr-defined]
    return binance


def mock_time(offset_ms: int = 0) -> None:
    respx.get(f"{FAPI}/fapi/v1/time").mock(
        return_value=httpx.Response(200, json={"serverTime": NOW_MS + offset_ms})
    )


@respx.mock
async def test_request_is_signed_with_the_secret() -> None:
    mock_time()
    route = respx.get(f"{FAPI}/fapi/v1/userTrades").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with client() as binance:
        await binance.signed("/fapi/v1/userTrades", {"symbol": "BTCUSDT"})

    request = route.calls.last.request
    assert request.headers["X-MBX-APIKEY"] == KEY
    query = dict(urllib.parse.parse_qsl(request.url.query.decode()))
    signature = query.pop("signature")
    raw = urllib.parse.urlencode(query)
    expected = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    assert signature == expected


@respx.mock
async def test_clock_offset_goes_into_the_timestamp() -> None:
    """Метка времени, разошедшаяся с биржей больше recvWindow, отклоняется целиком."""
    mock_time(offset_ms=8000)
    route = respx.get(f"{FAPI}/fapi/v2/balance").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with client() as binance:
        await binance.signed("/fapi/v2/balance")

    query = dict(urllib.parse.parse_qsl(route.calls.last.request.url.query.decode()))
    assert int(query["timestamp"]) == NOW_MS + 8000


@respx.mock
async def test_unreadable_server_time_does_not_stop_the_request() -> None:
    """Вспомогательный запрос не имеет права уронить основной."""
    respx.get(f"{FAPI}/fapi/v1/time").mock(return_value=httpx.Response(503))
    respx.get(f"{FAPI}/fapi/v2/balance").mock(
        return_value=httpx.Response(200, json=[{"asset": "USDT", "balance": "1"}])
    )
    async with client() as binance:
        rows = await binance.signed("/fapi/v2/balance")
    assert rows == [{"asset": "USDT", "balance": "1"}]


@respx.mock
async def test_restrictions_live_on_the_other_base_url() -> None:
    """`apiRestrictions` — это SAPI, а не fapi. Перепутать их стоит часа."""
    mock_time()
    route = respx.get(f"{SAPI}/sapi/v1/account/apiRestrictions").mock(
        return_value=httpx.Response(200, json={"enableReading": True})
    )
    async with client() as binance:
        await binance.signed("/sapi/v1/account/apiRestrictions", sapi=True)
    assert route.called


@respx.mock
async def test_bad_key_gets_its_own_text() -> None:
    mock_time()
    respx.get(f"{FAPI}/fapi/v2/balance").mock(
        return_value=httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key"})
    )
    async with client() as binance:
        try:
            await binance.signed("/fapi/v2/balance")
        except AppError as exc:
            assert exc.code == "key_rejected"
            assert "секрет" in exc.message or "ключ" in exc.message
        else:
            raise AssertionError("ожидали отказ по ключу")


@respx.mock
async def test_bad_signature_says_about_the_secret() -> None:
    mock_time()
    respx.get(f"{FAPI}/fapi/v2/balance").mock(
        return_value=httpx.Response(400, json={"code": -1022, "msg": "Signature invalid"})
    )
    async with client() as binance:
        try:
            await binance.signed("/fapi/v2/balance")
        except AppError as exc:
            assert exc.code == "key_rejected"
            assert "секрет" in exc.message
        else:
            raise AssertionError("ожидали отказ по подписи")


@respx.mock
async def test_429_pauses_and_says_when_to_retry() -> None:
    mock_time()
    respx.get(f"{FAPI}/fapi/v1/income").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "12"}, json={})
    )
    async with client() as binance:
        try:
            await binance.signed("/fapi/v1/income")
        except AppError as exc:
            assert exc.code == "rate_limited"
            assert exc.details["retry_after_sec"] == 12
        else:
            raise AssertionError("ожидали ограничение частоты")


@respx.mock
async def test_418_bans_the_connection_instead_of_retrying() -> None:
    """Упорство после 418 продлевает бан адреса — повторять нельзя."""
    mock_time()
    respx.get(f"{FAPI}/fapi/v1/income").mock(return_value=httpx.Response(418, json={}))
    async with client() as binance:
        try:
            await binance.signed("/fapi/v1/income")
        except BinanceBanned:
            pass
        else:
            raise AssertionError("ожидали бан")
        assert binance.banned is True
        # Следующий запрос даже не уходит в сеть.
        try:
            await binance.signed("/fapi/v1/income")
        except AppError as exc:
            assert exc.code == "provider_banned"
        else:
            raise AssertionError("после бана запросы не отправляются")


@respx.mock
async def test_high_weight_slows_down_the_next_request() -> None:
    mock_time()
    respx.get(f"{FAPI}/fapi/v1/income").mock(
        return_value=httpx.Response(
            200, json=[], headers={"X-MBX-USED-WEIGHT-1M": "2000"}
        )
    )
    async with client() as binance:
        await binance.signed("/fapi/v1/income")
        assert binance.weight.high is True
        await binance.signed("/fapi/v1/income")
    assert binance.slept  # type: ignore[attr-defined]


def test_window_is_cut_into_pieces_binance_accepts() -> None:
    """Окно `userTrades` не больше семи дней — иначе часть сделок потерялась бы."""
    since = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    until = dt.datetime(2026, 9, 20, tzinfo=dt.UTC)
    pieces = windows(since, until)
    assert len(pieces) == 3
    assert pieces[0][0] == since
    assert pieces[-1][1] == until
    # Куски идут встык: дыра между ними — это потерянные сделки.
    for left, right in zip(pieces, pieces[1:], strict=False):
        assert left[1] == right[0]


def test_empty_window_is_empty() -> None:
    moment = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    assert windows(moment, moment) == []
