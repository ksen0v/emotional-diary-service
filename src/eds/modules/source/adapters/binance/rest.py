"""HTTP-клиент к Binance USDⓈ-M Futures: подпись, два адреса, вес запросов.

Ответственность та же, что у клиента TMM: донести запрос и вернуть разобранный
JSON либо ошибку с готовым текстом. Отличий от TMM три, и все три существенные.

**Подпись.** Каждый запрос подписывается HMAC-SHA256 от строки параметров
секретом ключа. Секрет не покидает процесс: он приходит расшифрованным,
подписывает и остаётся здесь.

**Два базовых адреса.** Почти всё живёт на `fapi.binance.com`, но проверка прав
ключа — SAPI-эндпоинт на `api.binance.com`. Это не опечатка и не запасной
адрес: два разных сервиса Binance, и перепутать их стоит часа (Архитектура
ч.1 §5.3).

**Часы.** Подписанный запрос с меткой времени, разошедшейся с биржей больше
чем на `recvWindow`, отклоняется целиком. Поэтому при первом обращении читаем
`/fapi/v1/time` и держим поправку: это дешевле, чем объяснять трейдеру, почему
подключение не работает на его компьютере с уплывшими часами.

Вес запросов Binance считает на IP и отвечает `429`, а на упорство — `418`
и временным баном адреса, что хуже, чем у TMM. Поэтому притормаживаем заранее.
"""

import asyncio
import datetime as dt
import hashlib
import hmac
import logging
import random
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from eds.platform.errors import AppError

log = logging.getLogger("eds.source.binance")

FAPI = "https://fapi.binance.com"
# Проверка прав ключа живёт не на fapi. Это SAPI, другой базовый адрес.
SAPI = "https://api.binance.com"
WS_BASE = "wss://fstream.binance.com/ws"

ENDPOINT_TIME = "/fapi/v1/time"
ENDPOINT_RESTRICTIONS = "/sapi/v1/account/apiRestrictions"
ENDPOINT_BALANCE = "/fapi/v2/balance"
ENDPOINT_ACCOUNT = "/fapi/v2/account"
ENDPOINT_POSITIONS = "/fapi/v3/positionRisk"
ENDPOINT_INCOME = "/fapi/v1/income"
ENDPOINT_USER_TRADES = "/fapi/v1/userTrades"
ENDPOINT_LISTEN_KEY = "/fapi/v1/listenKey"

RECV_WINDOW_MS = 5000
# Выше этой доли использованного веса импорт притормаживает (§5.6). Семьдесят
# процентов, а не девяносто: между нашим запросом и учётом Binance есть зазор,
# и упереться в `418` дороже, чем подождать.
THROTTLE_ABOVE = 0.7
THROTTLE_PAUSE_SEC = 1.0
# Документированный лимит веса в минуту. Заголовок отдаёт использованное,
# а не предел, поэтому предел приходится знать заранее.
WEIGHT_LIMIT_1M = 2400
BLIND_PAUSE_SEC = 30.0
MAX_WAIT_SEC = 20.0

# Окно `userTrades` ограничено семью днями (§5.4).
MAX_WINDOW = dt.timedelta(days=7)
PAGE_LIMIT = 1000

# Коды ошибок Binance, у которых должен быть свой текст, а не «код −2015».
CODE_BAD_KEY = {-2014, -2015}
CODE_BAD_SIGNATURE = {-1022}
CODE_TIMESTAMP = {-1021}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class Weight:
    """Использованный вес за минуту — то, что Binance пишет в заголовок."""

    used_1m: int | None = None
    limit_1m: int = WEIGHT_LIMIT_1M

    @property
    def high(self) -> bool:
        if self.used_1m is None:
            return False
        return self.used_1m > self.limit_1m * THROTTLE_ABOVE


class BinanceBanned(Exception):
    """Ответ 418: адрес временно забанен. Подключение надо гасить, а не повторять."""


class BinanceClient:
    """Клиент к Binance Futures. Один экземпляр на одно подключение."""

    def __init__(
        self,
        api_key: str,
        secret: str,
        *,
        http: httpx.AsyncClient | None = None,
        fapi: str = FAPI,
        sapi: str = SAPI,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], dt.datetime] = now_utc,
        jitter: Callable[[], float] = lambda: random.uniform(0.5, 2.0),
        timeout: float = 15.0,
    ):
        self._key = api_key.strip()
        self._secret = secret.strip().encode("utf-8")
        self._fapi = fapi.rstrip("/")
        self._sapi = sapi.rstrip("/")
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._timeout = timeout
        self._http = http
        self._own_http = http is None
        self._time_offset_ms: int | None = None
        self.weight = Weight()
        self.blocked_until: dt.datetime | None = None
        self.banned = False

    async def __aenter__(self) -> "BinanceClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
            self._own_http = True
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- часы ---

    async def sync_clock(self) -> int:
        """Поправка к нашим часам в миллисекундах.

        Читается один раз на клиента. Если сервер времени недоступен, работаем
        без поправки: подпись, скорее всего, пройдёт, а падать из-за
        вспомогательного запроса нельзя.
        """
        if self._time_offset_ms is not None:
            return self._time_offset_ms
        try:
            payload = await self._request("GET", ENDPOINT_TIME, base=self._fapi)
            server_ms = int(payload["serverTime"])
        except (AppError, KeyError, TypeError, ValueError) as exc:
            log.warning("Binance: время сервера не прочитано (%s), идём без поправки", exc)
            self._time_offset_ms = 0
            return 0
        self._time_offset_ms = server_ms - self._now_ms()
        if abs(self._time_offset_ms) > RECV_WINDOW_MS:
            log.warning(
                "Binance: часы разошлись на %s мс — подписи идут с поправкой",
                self._time_offset_ms,
            )
        return self._time_offset_ms

    def _now_ms(self) -> int:
        return int(self._clock().timestamp() * 1000)

    # --- запросы ---

    async def signed(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        sapi: bool = False,
    ) -> Any:
        """Подписанный запрос. `sapi=True` — второй базовый адрес."""
        offset = await self.sync_clock()
        query = dict(params or {})
        query["timestamp"] = self._now_ms() + offset
        query["recvWindow"] = RECV_WINDOW_MS
        raw = urllib.parse.urlencode(query, doseq=True)
        query["signature"] = hmac.new(
            self._secret, raw.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        base = self._sapi if sapi else self._fapi
        return await self._request(method, path, params=query, base=base)

    async def keyed(self, path: str, *, method: str = "POST") -> Any:
        """Запрос, которому нужен только ключ: ключ потока не подписывается."""
        return await self._request(method, path, base=self._fapi)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        base: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
            self._own_http = True

        await self._respect_weight()

        try:
            response = await self._http.request(
                method,
                base + path,
                params=params,
                headers={"X-MBX-APIKEY": self._key, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            log.warning("Binance недоступен: %s", type(exc).__name__)
            raise AppError(
                "provider_unavailable",
                "Binance не отвечает. Попробуй позже.",
                502,
                {"error": type(exc).__name__},
            ) from exc

        self._read_weight(response)
        return self._interpret(response)

    # --- вес и паузы ---

    async def _respect_weight(self) -> None:
        if self.banned:
            raise AppError(
                "provider_banned",
                "Binance временно закрыл доступ с нашего адреса. "
                "Подключение отключено, повторим позже.",
                503,
            )
        if self.blocked_until is not None:
            wait = (self.blocked_until - self._clock()).total_seconds()
            if wait > MAX_WAIT_SEC:
                raise AppError(
                    "rate_limited",
                    f"Binance ограничил частоту запросов. Повтор через {int(wait)} с.",
                    429,
                    {"retry_after_sec": int(wait)},
                )
            if wait > 0:
                await self._sleep(wait)
            self.blocked_until = None
        if self.weight.high:
            await self._sleep(THROTTLE_PAUSE_SEC)

    def _read_weight(self, response: httpx.Response) -> None:
        raw = response.headers.get("x-mbx-used-weight-1m")
        try:
            self.weight = Weight(used_1m=int(raw) if raw is not None else None)
        except ValueError:
            self.weight = Weight()

    # --- разбор ответа ---

    def _interpret(self, response: httpx.Response) -> Any:
        code = response.status_code

        if code == 418:
            # Бан адреса. Повторять нельзя: упорство продлевает бан.
            self.banned = True
            log.error("Binance: 418, адрес забанен")
            raise BinanceBanned(str(response.url))

        if code == 429:
            retry = _int(response.headers.get("retry-after"))
            seconds = retry if retry is not None else BLIND_PAUSE_SEC
            self.blocked_until = self._clock() + dt.timedelta(
                seconds=seconds + self._jitter()
            )
            wait = max(0, int((self.blocked_until - self._clock()).total_seconds()))
            raise AppError(
                "rate_limited",
                f"Binance ограничил частоту запросов. Повтор через {wait} с.",
                429,
                {"retry_after_sec": wait},
            )

        payload = _json(response)

        if code >= 400:
            raise self._error(code, payload)
        return payload

    def _error(self, status: int, payload: Any) -> AppError:
        api_code = payload.get("code") if isinstance(payload, dict) else None
        message = payload.get("msg") if isinstance(payload, dict) else None

        if api_code in CODE_BAD_KEY or status in (401, 403):
            return AppError(
                "key_rejected",
                "Binance отклонил ключ: проверь, что скопированы и ключ, и секрет "
                "целиком и ключ не отозван.",
                400,
                {"binance_code": api_code},
            )
        if api_code in CODE_BAD_SIGNATURE:
            return AppError(
                "key_rejected",
                "Binance не принял подпись: скорее всего, секрет скопирован не целиком.",
                400,
                {"binance_code": api_code},
            )
        if api_code in CODE_TIMESTAMP:
            return AppError(
                "clock_skew",
                "Binance отклонил запрос из-за расхождения часов. "
                "Проверь время на компьютере и повтори.",
                502,
                {"binance_code": api_code},
            )
        return AppError(
            "provider_rejected",
            f"Binance отклонил запрос: {message or f'код {status}'}.",
            502,
            {"status": status, "binance_code": api_code},
        )


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def windows(
    since: dt.datetime, until: dt.datetime, *, span: dt.timedelta = MAX_WINDOW
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Разбить окно на куски, которые Binance согласится отдать.

    `userTrades` и `income` принимают окно не больше семи дней. Для сверки
    за сутки это один кусок, но код обязан быть общим: после долгого простоя
    окно вырастает, и тихо усечённое до семи дней окно означало бы потерянные
    сделки — ровно то, что нечем будет заметить.
    """
    if until <= since:
        return []
    out: list[tuple[dt.datetime, dt.datetime]] = []
    left = since
    while left < until:
        right = min(left + span, until)
        out.append((left, right))
        left = right
    return out


def ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def at(milliseconds: int | str) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(milliseconds) / 1000, tz=dt.UTC)
