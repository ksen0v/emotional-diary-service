"""Клиент TMM: зеркала, лимиты, коды ошибок.

Проверяется поведение в тех случаях, которые на живом API воспроизвести нельзя
по требованию: падение основного адреса, 429 и остаток лимита на нуле.
"""

import datetime as dt

import httpx
import pytest
import respx

from eds.modules.source.adapters.tmm.rest import (
    BASE_URLS,
    TmmClient,
    rows_of,
)
from eds.platform.errors import AppError

PRIMARY, MIRROR_1, MIRROR_2 = BASE_URLS
NOW = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC)


def client(**over) -> TmmClient:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    kwargs = dict(sleep=sleep, clock=lambda: NOW, jitter=lambda: 0.0)
    kwargs.update(over)
    tmm = TmmClient("test-key-123456", **kwargs)
    tmm.slept = slept  # type: ignore[attr-defined]
    return tmm


@respx.mock
async def test_key_goes_into_header() -> None:
    route = respx.get(f"{PRIMARY}/trades/").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with client() as tmm:
        await tmm.get("/trades/")
    assert route.calls.last.request.headers["API-KEY"] == "test-key-123456"


@respx.mock
async def test_mirror_takes_over_on_5xx() -> None:
    respx.get(f"{PRIMARY}/trades/").mock(return_value=httpx.Response(502))
    respx.get(f"{MIRROR_1}/trades/").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 1}]})
    )
    async with client() as tmm:
        payload = await tmm.get("/trades/")
    assert rows_of(payload) == [{"id": 1}]


@respx.mock
async def test_mirror_takes_over_on_network_error() -> None:
    respx.get(f"{PRIMARY}/trades/").mock(side_effect=httpx.ConnectError("нет сети"))
    respx.get(f"{MIRROR_1}/trades/").mock(return_value=httpx.Response(200, json=[]))
    async with client() as tmm:
        assert await tmm.get("/trades/") == []
        assert tmm.base_in_use == MIRROR_1


@respx.mock
async def test_all_addresses_down_is_provider_unavailable() -> None:
    for base in BASE_URLS:
        respx.get(f"{base}/trades/").mock(return_value=httpx.Response(503))
    async with client() as tmm:
        with pytest.raises(AppError) as exc:
            await tmm.get("/trades/")
    assert exc.value.code == "provider_unavailable"
    assert exc.value.http_status == 502


@respx.mock
async def test_bad_key_is_key_rejected() -> None:
    respx.get(f"{PRIMARY}/trades/").mock(return_value=httpx.Response(401))
    async with client() as tmm:
        with pytest.raises(AppError) as exc:
            await tmm.get("/trades/")
    assert exc.value.code == "key_rejected"
    # Зеркала не перебираем: ключ отклонён везде, и лишние запросы только
    # приближают блокировку аккаунта.
    assert respx.calls.call_count == 1


@respx.mock
async def test_rate_limit_headers_are_read() -> None:
    reset = int((NOW + dt.timedelta(seconds=45)).timestamp())
    respx.get(f"{PRIMARY}/trades/").mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={
                "x-ratelimit-limit": "120",
                "x-ratelimit-remaining": "97",
                "x-ratelimit-reset": str(reset),
            },
        )
    )
    async with client() as tmm:
        await tmm.get("/trades/")
    assert tmm.rate_limit.limit == 120
    assert tmm.rate_limit.remaining == 97
    assert tmm.rate_limit.reset_at == dt.datetime.fromtimestamp(reset, tz=dt.UTC)
    assert tmm.rate_limit.low is False


@respx.mock
async def test_low_remaining_throttles_next_request() -> None:
    """Остаток ниже 20% — сверка притормаживает, уступая потоку (§4.6)."""
    respx.get(f"{PRIMARY}/trades/").mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={"x-ratelimit-limit": "100", "x-ratelimit-remaining": "5"},
        )
    )
    async with client() as tmm:
        await tmm.get("/trades/")
        assert tmm.slept == []  # первый запрос идёт без паузы
        await tmm.get("/trades/")
        assert tmm.slept == [1.0]


@respx.mock
async def test_429_blocks_until_reset() -> None:
    reset = int((NOW + dt.timedelta(seconds=300)).timestamp())
    respx.get(f"{PRIMARY}/trades/").mock(
        return_value=httpx.Response(429, headers={"x-ratelimit-reset": str(reset)})
    )
    async with client() as tmm:
        with pytest.raises(AppError) as first:
            await tmm.get("/trades/")
        assert first.value.code == "rate_limited"
        assert first.value.details["retry_after_sec"] == 300

        # Повторов до момента сброса нет: второй вызов даже не уходит в сеть.
        calls_before = respx.calls.call_count
        with pytest.raises(AppError) as second:
            await tmm.get("/trades/")
        assert second.value.code == "rate_limited"
        assert respx.calls.call_count == calls_before


@respx.mock
async def test_short_pause_is_waited_out_not_raised() -> None:
    reset = int((NOW + dt.timedelta(seconds=3)).timestamp())
    respx.get(f"{PRIMARY}/trades/").mock(
        side_effect=[
            httpx.Response(429, headers={"x-ratelimit-reset": str(reset)}),
            httpx.Response(200, json=[]),
        ]
    )
    async with client() as tmm:
        with pytest.raises(AppError):
            await tmm.get("/trades/")
        assert await tmm.get("/trades/") == []
        assert tmm.slept == [3.0]


def test_rows_of_unwraps_any_envelope() -> None:
    assert rows_of([{"id": 1}]) == [{"id": 1}]
    assert rows_of({"data": [{"id": 1}]}) == [{"id": 1}]
    assert rows_of({"result": {"items": [{"id": 1}]}}) == [{"id": 1}]
    assert rows_of({"id": 1, "symbol": "BTCUSDT"}) == [{"id": 1, "symbol": "BTCUSDT"}]
    assert rows_of({"meta": 1}) == []
    assert rows_of(None) == []
