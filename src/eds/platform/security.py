"""Пароли, сессионные токены и куки.

Решение из Архитектуры ч.1: куки-сессии вместо JWT. В куке — случайные 32 байта,
в базе только их sha256, поэтому дамп базы не даёт войти в чужую сессию.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Response

SESSION_COOKIE = "eds_session"
CSRF_COOKIE = "eds_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_DAYS = 30

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        # битый хеш в базе — это не «неверный пароль», но пускать нельзя
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(days=SESSION_DAYS)


def set_session_cookies(
    response: Response, session_token: str, csrf_token: str, *, secure: bool
) -> None:
    """Поставить куки сессии.

    `secure` определяется схемой запроса, а не именем окружения: по http
    (локальная разработка) браузер Secure-куку просто не вернёт, а по https
    она обязательна. За TLS-терминирующим прокси нужен uvicorn с proxy-headers,
    иначе схема придёт как http и флаг не выставится.
    """
    max_age = SESSION_DAYS * 24 * 3600
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    # CSRF-кука читается из JavaScript — так работает double submit
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def is_https(scheme: str) -> bool:
    return scheme == "https"


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
