"""HTTP-клиент к TMM API v2: ключ в заголовке, три адреса, лимиты.

Ответственность клиента ровно одна — донести запрос до живого адреса и вернуть
разобранный JSON либо ошибку с готовым текстом. Ни о сделках, ни о базе он
не знает: перевод данных живёт в mapping.py, сохранение — в service.py.
"""

import asyncio
import datetime as dt
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from eds.platform.errors import AppError

log = logging.getLogger("eds.source.tmm")

# Три адреса — готовый failover (Архитектура ч.1 §4.1). Порядок значим:
# основной первый, при сетевой ошибке или 5xx идём по зеркалам, следующий
# запрос снова начинает с основного.
BASE_URLS: tuple[str, ...] = (
    "https://tradermake.money/api/v2",
    "https://tradermakemoney.com/api/v2",
    "https://tradersmakemoney.com/api/v2",
)

# Ниже этой доли остатка лимита сверка притормаживает, уступая потоку (§4.6).
THROTTLE_BELOW = 0.2
THROTTLE_PAUSE_SEC = 1.0

# Сколько ждём после 429, если заголовок с моментом сброса не пришёл.
BLIND_PAUSE_SEC = 30.0
# Дольше не ждём даже по просьбе провайдера: запрос должен вернуть ответ,
# а не висеть. Вместо ожидания отдаём ошибку с временем повтора.
MAX_WAIT_SEC = 20.0

ENDPOINT_TRADES = "/trades/"
ENDPOINT_TAGS = "/trades/tags"
ENDPOINT_TAG_COLUMNS = "/trades/tag-categories"
ENDPOINT_KEYS = "/api-key"


@dataclass
class RateLimit:
    """То, что прочитали из заголовков x-ratelimit-*. Числа не документированы."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: dt.datetime | None = None

    @property
    def low(self) -> bool:
        if self.limit in (None, 0) or self.remaining is None:
            return False
        return self.remaining < self.limit * THROTTLE_BELOW


class EndpointMissing(Exception):
    """Адрес вернул 404: у адаптера есть обходной путь, это не ошибка ключа."""


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def rows_of(payload: Any) -> list[dict[str, Any]]:
    """Список записей из ответа, каким бы конвертом он ни был обёрнут."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "result", "results", "items", "trades", "rows", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                nested = value.get("items") or value.get("data")
                if isinstance(nested, list):
                    return [row for row in nested if isinstance(row, dict)]
        # Одиночный объект — тоже ответ.
        if any(key in payload for key in ("id", "symbol")):
            return [payload]
    return []


class TmmClient:
    """Клиент к API TMM. Один экземпляр на одно подключение."""

    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        bases: tuple[str, ...] = BASE_URLS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], dt.datetime] = now_utc,
        jitter: Callable[[], float] = lambda: random.uniform(0.5, 2.0),
        timeout: float = 15.0,
    ):
        self._key = api_key.strip()
        self._bases = bases
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._timeout = timeout
        self._http = http
        self._own_http = http is None
        self.rate_limit = RateLimit()
        self.blocked_until: dt.datetime | None = None
        self.consecutive_429 = 0
        self.base_in_use: str | None = None

    async def __aenter__(self) -> "TmmClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET к TMM с перебором адресов. Возвращает разобранный JSON."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
            self._own_http = True

        await self._respect_limits()

        failures: list[str] = []
        for base in self._bases:
            url = base + path
            try:
                response = await self._http.get(
                    url,
                    params=params,
                    headers={"API-KEY": self._key, "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                failures.append(f"{base}: {type(exc).__name__}")
                continue

            self._read_limits(response)

            if response.status_code >= 500:
                failures.append(f"{base}: {response.status_code}")
                continue

            self.base_in_use = base
            return self._interpret(response)

        log.warning("TMM недоступен на всех адресах: %s", "; ".join(failures))
        raise AppError(
            "provider_unavailable",
            "Провайдер не отвечает. Попробуй позже.",
            502,
            {"attempts": failures},
        )

    # --- лимиты ---

    async def _respect_limits(self) -> None:
        """Пауза перед запросом: после 429 и при остатке лимита ниже 20%."""
        if self.blocked_until is not None:
            wait = (self.blocked_until - self._clock()).total_seconds()
            if wait > MAX_WAIT_SEC:
                raise AppError(
                    "rate_limited",
                    "TMM ограничил частоту запросов. Повтор возможен через "
                    f"{int(wait)} с.",
                    429,
                    {"retry_after_sec": int(wait)},
                )
            if wait > 0:
                await self._sleep(wait)
            self.blocked_until = None

        if self.rate_limit.low:
            # Остаток лимита отдаём потоку: сверка — страховка, поток — основное.
            await self._sleep(THROTTLE_PAUSE_SEC)

    def _read_limits(self, response: httpx.Response) -> None:
        headers = response.headers
        self.rate_limit = RateLimit(
            limit=_int(headers.get("x-ratelimit-limit")) or self.rate_limit.limit,
            remaining=_int(headers.get("x-ratelimit-remaining")),
            reset_at=_reset_at(headers.get("x-ratelimit-reset")),
        )

    # --- разбор ответа ---

    def _interpret(self, response: httpx.Response) -> Any:
        code = response.status_code

        if code == 429:
            self.consecutive_429 += 1
            reset = self.rate_limit.reset_at
            until = reset or self._clock() + dt.timedelta(seconds=BLIND_PAUSE_SEC)
            self.blocked_until = until + dt.timedelta(seconds=self._jitter())
            wait = max(0, int((self.blocked_until - self._clock()).total_seconds()))
            raise AppError(
                "rate_limited",
                f"TMM ограничил частоту запросов. Повтор возможен через {wait} с.",
                429,
                {"retry_after_sec": wait, "in_a_row": self.consecutive_429},
            )

        self.consecutive_429 = 0

        if code in (401, 403):
            raise AppError(
                "key_rejected",
                "TMM отклонил ключ: проверь, что скопирован весь ключ и он не отозван.",
                400,
            )
        if code == 404:
            raise EndpointMissing(str(response.url))
        if code >= 400:
            raise AppError(
                "provider_rejected",
                f"TMM отклонил запрос (код {code}).",
                502,
                {"status": code},
            )

        try:
            return response.json()
        except ValueError as exc:
            raise AppError(
                "provider_bad_response",
                "TMM ответил не тем, что мы умеем читать.",
                502,
            ) from exc


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _reset_at(value: str | None) -> dt.datetime | None:
    """Момент сброса лимита. В заголовке unix-секунды (§4.1)."""
    number = _int(value)
    if number is None:
        return None
    if number < 10**9:
        # Некоторые реализации присылают «через сколько секунд». Отличаем
        # по величине: unix-время меньше 10^9 закончилось в 2001 году.
        return now_utc() + dt.timedelta(seconds=number)
    return dt.datetime.fromtimestamp(number, tz=dt.UTC)
