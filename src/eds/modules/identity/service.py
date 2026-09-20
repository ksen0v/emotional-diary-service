"""Логика identity: регистрация, вход, сессии, настройки.

Здесь же валидация настроек. Она обязана быть на сервере, потому что на фронте
её обойдут, а неверная граница дня или порог чека ломают расчёты во всех модулях.
"""

import datetime as dt
import uuid
import zoneinfo
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.identity import repo
from eds.modules.identity.models import Session, Settings, User
from eds.platform import security
from eds.platform.errors import UNPROCESSABLE, AppError

MIN_PASSWORD = 10
LOGIN_WINDOW = dt.timedelta(minutes=15)
LOGIN_ATTEMPTS = 5

# Счётчик попыток входа в памяти процесса. Для одного процесса api этого достаточно;
# при нескольких процессах счётчик станет общим только через таблицу — отдельная
# задача, и до неё это честное ограничение, а не забытая деталь.
_attempts: dict[str, list[dt.datetime]] = {}


@dataclass(frozen=True)
class Identity:
    """Кто пришёл: пользователь и его текущая сессия."""

    user: User
    session: Session


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _too_many_attempts(email: str) -> bool:
    now = dt.datetime.now(dt.UTC)
    fresh = [t for t in _attempts.get(email, []) if now - t < LOGIN_WINDOW]
    _attempts[email] = fresh
    return len(fresh) >= LOGIN_ATTEMPTS


def _remember_attempt(email: str) -> None:
    _attempts.setdefault(email, []).append(dt.datetime.now(dt.UTC))


def _forget_attempts(email: str) -> None:
    _attempts.pop(email, None)


async def register(
    s: AsyncSession, email: str, password: str, user_agent: str | None, ip: str | None
) -> tuple[Identity, str, str]:
    email = normalize_email(email)
    if len(password) < MIN_PASSWORD:
        raise AppError(
            "weak_password",
            f"Пароль короче {MIN_PASSWORD} символов.",
            status.HTTP_400_BAD_REQUEST,
        )
    if await repo.user_by_email(s, email) is not None:
        raise AppError(
            "email_taken",
            "Этот адрес уже зарегистрирован.",
            status.HTTP_409_CONFLICT,
        )

    user = await repo.create_user(s, email, security.hash_password(password))
    await repo.create_default_settings(s, user.id)
    identity, session_token, csrf_token = await _open_session(s, user, user_agent, ip)
    return identity, session_token, csrf_token


async def login(
    s: AsyncSession, email: str, password: str, user_agent: str | None, ip: str | None
) -> tuple[Identity, str, str]:
    email = normalize_email(email)
    if _too_many_attempts(email):
        raise AppError(
            "too_many_attempts",
            "Слишком много попыток входа. Подожди 15 минут.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = await repo.user_by_email(s, email)
    # Текст одинаковый и для несуществующего адреса, и для неверного пароля:
    # иначе по ответу можно перебирать, кто зарегистрирован.
    if user is None or not security.verify_password(password, user.password_hash):
        _remember_attempt(email)
        raise AppError(
            "bad_credentials",
            "Неверный адрес или пароль.",
            status.HTTP_401_UNAUTHORIZED,
        )

    _forget_attempts(email)
    return await _open_session(s, user, user_agent, ip)


async def _open_session(
    s: AsyncSession, user: User, user_agent: str | None, ip: str | None
) -> tuple[Identity, str, str]:
    session_token = security.new_token()
    csrf_token = security.new_token()
    session = await repo.create_session(
        s,
        user_id=user.id,
        token_hash=security.token_hash(session_token),
        expires_at=security.expires_at(),
        user_agent=user_agent,
        ip=ip,
    )
    return Identity(user=user, session=session), session_token, csrf_token


async def identity_by_token(s: AsyncSession, token: str) -> Identity:
    session = await repo.live_session_by_hash(s, security.token_hash(token))
    if session is None:
        raise AppError(
            "unauthenticated",
            "Сессия не найдена или истекла. Войди заново.",
            status.HTTP_401_UNAUTHORIZED,
        )
    user = await repo.user_by_id(s, session.user_id)
    if user is None:
        raise AppError(
            "unauthenticated",
            "Сессия не найдена или истекла. Войди заново.",
            status.HTTP_401_UNAUTHORIZED,
        )
    await repo.touch_session(s, session.id)
    return Identity(user=user, session=session)


async def logout(s: AsyncSession, session_id: uuid.UUID) -> None:
    await repo.revoke_session(s, session_id)


async def revoke(s: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    session = await repo.session_of_user(s, user_id, session_id)
    if session is None:
        raise AppError("not_found", "Сессия не найдена.", status.HTTP_404_NOT_FOUND)
    await repo.revoke_session(s, session_id)


# --- настройки ---

TIMEZONE_HINT = "Например: Europe/Moscow, Asia/Almaty, UTC."


def _validate(current: Settings, changes: dict) -> dict:
    clean: dict = {}

    if "timezone" in changes:
        tz = str(changes["timezone"]).strip()
        try:
            zoneinfo.ZoneInfo(tz)
        except Exception as exc:
            raise AppError(
                "unknown_timezone",
                f"Неизвестная таймзона «{tz}». {TIMEZONE_HINT}",
                UNPROCESSABLE,
            ) from exc
        clean["timezone"] = tz

    if "day_cutoff" in changes:
        clean["day_cutoff"] = changes["day_cutoff"]

    pass_score = changes.get("pass_score", current.pass_score)
    min_score = changes.get("min_score", current.min_score)
    if not (1 <= int(min_score) <= 25 and 1 <= int(pass_score) <= 25):
        raise AppError(
            "value_out_of_range",
            "Пороги чека считаются из 25 баллов: допустимо от 1 до 25.",
            UNPROCESSABLE,
        )
    if int(pass_score) <= int(min_score):
        raise AppError(
            "pass_not_above_min",
            "Проходной балл должен быть выше минимального: иначе «под риском» "
            "не существует как состояние.",
            UNPROCESSABLE,
        )
    if "pass_score" in changes:
        clean["pass_score"] = int(pass_score)
    if "min_score" in changes:
        clean["min_score"] = int(min_score)

    if "significance_pct" in changes:
        try:
            value = Decimal(str(changes["significance_pct"])).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise AppError(
                "validation_failed",
                "Порог значимости — число в процентах от депозита.",
                status.HTTP_400_BAD_REQUEST,
            ) from exc
        if not (Decimal("0") <= value <= Decimal("10")):
            raise AppError(
                "value_out_of_range",
                "Порог значимости — от 0 до 10 процентов депозита.",
                UNPROCESSABLE,
            )
        clean["significance_pct"] = value

    for flag in ("telegram_enabled", "shadow_mode"):
        if flag in changes:
            clean[flag] = bool(changes[flag])

    if not clean:
        raise AppError(
            "validation_failed",
            "Нечего менять: не передано ни одной настройки.",
            status.HTTP_400_BAD_REQUEST,
        )
    return clean


TIME_NOTICE = (
    "Новая граница дня действует со следующего торгового дня. "
    "Прошлые дни остаются как записаны."
)


async def update_settings(
    s: AsyncSession, user_id: uuid.UUID, changes: dict
) -> tuple[Settings, str | None]:
    current = await repo.settings_of(s, user_id)
    if current is None:
        raise AppError("not_found", "Настройки не найдены.", status.HTTP_404_NOT_FOUND)

    clean = _validate(current, changes)
    time_changed = (
        clean.get("timezone", current.timezone) != current.timezone
        or clean.get("day_cutoff", current.day_cutoff) != current.day_cutoff
    )
    row = await repo.save_settings(s, current, clean)
    # История не пересчитывается (решение ч.1): trading_day фиксируется при приёме.
    return row, (TIME_NOTICE if time_changed else None)
