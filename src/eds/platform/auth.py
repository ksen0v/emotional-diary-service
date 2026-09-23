"""Кто пришёл и какие у него настройки — без импорта чужого модуля.

Задача: модулю trades нужны таймзона и граница дня, чтобы понять, какой сейчас
торговый день. Эти данные принадлежат модулю identity, а импортировать его
модулю trades нельзя — иначе граница между модулями перестаёт быть границей.

Решение: identity при запуске регистрирует здесь две функции-резолвера,
а остальные модули берут готовые зависимости из платформы. Когда монолит
разрежут, резолвер станет вызовом по сети, и ни один модуль не изменится.
"""

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from eds.platform import db, security
from eds.platform.errors import csrf_invalid, unauthenticated

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class CurrentUser:
    user_id: uuid.UUID
    email: str
    session_id: uuid.UUID


@dataclass(frozen=True)
class UserPrefs:
    """Настройки, которые нужны другим модулям для расчётов."""

    timezone: str
    day_cutoff: dt.time
    significance_pct: Decimal
    shadow_mode: bool
    # Пороги допуска живут в настройках трейдера (ТЗ 5.2), а считает по ним
    # модуль daybook. Он не имеет права читать схему identity, поэтому пороги
    # приходят сюда тем же путём, что таймзона и порог значимости.
    pass_score: int = 19
    min_score: int = 13


UserResolver = Callable[[AsyncSession, str], Awaitable[CurrentUser]]
PrefsResolver = Callable[[AsyncSession, uuid.UUID], Awaitable[UserPrefs]]
CapabilitiesResolver = Callable[[AsyncSession, uuid.UUID], Awaitable[dict]]

_user_resolver: UserResolver | None = None
_prefs_resolver: PrefsResolver | None = None
_capabilities_resolver: CapabilitiesResolver | None = None


def register(user: UserResolver, prefs: PrefsResolver) -> None:
    """Вызывается модулем identity. Больше никем."""
    global _user_resolver, _prefs_resolver
    _user_resolver = user
    _prefs_resolver = prefs


def register_source(capabilities: CapabilitiesResolver) -> None:
    """Вызывается модулем source. Больше никем.

    Через это модуль trades узнаёт, отдаёт ли активный источник теги, не зная
    о существовании модуля source.
    """
    global _capabilities_resolver
    _capabilities_resolver = capabilities


async def check_csrf(request: Request) -> None:
    """Double submit: значение куки должно совпасть с заголовком.

    GET свободны сознательно — ни один GET в этом API ничего не меняет.
    """
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(security.CSRF_COOKIE)
    header = request.headers.get(security.CSRF_HEADER)
    if not cookie or not header or cookie != header:
        raise csrf_invalid()


async def current_user(
    request: Request, s: AsyncSession = Depends(db.session)
) -> CurrentUser:
    if _user_resolver is None:  # pragma: no cover — значит identity не подключён
        raise RuntimeError("резолвер пользователя не зарегистрирован")
    token = request.cookies.get(security.SESSION_COOKIE)
    if not token:
        raise unauthenticated()
    user = await _user_resolver(s, token)
    await s.commit()
    return user


async def current_prefs(
    user: CurrentUser = Depends(current_user), s: AsyncSession = Depends(db.session)
) -> UserPrefs:
    if _prefs_resolver is None:  # pragma: no cover
        raise RuntimeError("резолвер настроек не зарегистрирован")
    return await _prefs_resolver(s, user.user_id)


async def prefs_of(s: AsyncSession, user_id: uuid.UUID) -> UserPrefs:
    """Настройки пользователя вне запроса — для фоновых консьюмеров.

    Тот же резолвер, что и у зависимости current_prefs: у фонового процесса
    нет запроса, но правило «модуль не читает чужую схему» от этого не меняется.
    """
    if _prefs_resolver is None:  # pragma: no cover
        raise RuntimeError("резолвер настроек не зарегистрирован")
    return await _prefs_resolver(s, user_id)


async def source_capabilities(s: AsyncSession, user_id: uuid.UUID) -> dict:
    """Возможности активного источника. Пусто — источник не подключён."""
    if _capabilities_resolver is None:  # pragma: no cover — source не подключён
        raise RuntimeError("резолвер возможностей источника не зарегистрирован")
    return await _capabilities_resolver(s, user_id)


async def source_provides_tags(s: AsyncSession, user_id: uuid.UUID) -> bool:
    caps = await source_capabilities(s, user_id)
    return bool(caps.get("provides_tags", False))
