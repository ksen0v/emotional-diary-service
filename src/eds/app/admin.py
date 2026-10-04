"""Админка: вход через бота, пользователи, чистка, сервисные настройки.

**Почему это оркестрация, а не модуль.** Админка по определению смотрит
поперёк всего сервиса: кто зарегистрирован (identity), сколько у него сделок
(trades), инцидентов (incidents), записей (daybook). Ни один модуль не имеет
права читать чужую схему, поэтому собрать такую картину может только `app/` —
то же правило, по которому здесь живут `today.py` и `notify.py`.

**Почему вход через Telegram, а не пароль.** Решение Влада от 04.10. Пароль
от админки — это ещё один секрет, который можно потерять, подобрать или
оставить в браузере на чужой машине. Ссылка `t.me/бот?start=A-…` переносит
опознание туда, где оно уже есть: чат привязан к пользователю, у пользователя
стоит признак админа. Формы с паролем здесь нет вовсе.

**Что это значит на практике.** Код выдаётся кому угодно — страница админки
открыта, и выдать код не значит пустить. Пускает открытие ссылки из чата,
который привязан к админу: пока этого не произошло, код не стоит ничего.
Поэтому `user_id` у кода пуст до подтверждения, и поэтому код одноразовый
и живёт минуты.

**Чего здесь нет.** Второго админа, ролей с разными правами, журнала
действий админа. Если понадобятся — это отдельное решение, а не «заодно».
"""

import datetime as dt
import logging
import secrets
import uuid
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.daybook import repo as daybook_repo
from eds.modules.identity import repo as identity_repo
from eds.modules.identity import service as identity_service
from eds.modules.incidents import repo as incidents_repo
from eds.modules.notifications import repo as notify_repo
from eds.modules.notifications import service as notify_service
from eds.modules.rules import repo as rules_repo
from eds.modules.source import repo as source_repo
from eds.modules.streaks import repo as streaks_repo
from eds.modules.trades import repo as trades_repo
from eds.platform.errors import UNPROCESSABLE, AppError

log = logging.getLogger("eds.admin")

# Код входа живёт недолго: он лежит на экране, который мог остаться открытым
# на чужом мониторе. Десяти минут хватает открыть Telegram и нажать кнопку.
LOGIN_TTL_MIN = 10
PREFIX = "A-"

# Что можно вычистить по частям. Правила в список не входят: это то, что
# трейдер написал про себя, а не история торговли, и уносить их вместе
# со сделками значило бы решить за него.
PARTS = ("trades", "incidents", "diary", "streak", "notifications", "source")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- вход ---


async def start_login(s: AsyncSession) -> dict[str, Any]:
    """Выдать одноразовый код и ссылку на бота.

    Без бота ссылки нет и входа нет: отвечаем прямо, а не выдаём код,
    который некуда отнести.
    """
    username = await notify_service.bot_username()
    if username is None:
        raise AppError(
            "bot_not_installed",
            "Бот не подключён, войти в админку нечем. "
            "Токен бота задаётся переменной EDS_BOT_TOKEN в окружении сервиса.",
            status.HTTP_409_CONFLICT,
        )

    await identity_repo.drop_expired_admin_logins(s, _now())
    code = PREFIX + secrets.token_urlsafe(12)
    row = await identity_repo.create_admin_login(
        s, code, _now() + dt.timedelta(minutes=LOGIN_TTL_MIN)
    )
    return {
        "code": row.code,
        "bot_url": f"https://t.me/{username}?start={row.code}",
        "expires_at": row.expires_at,
    }


async def confirm_from_bot(s: AsyncSession, code: str, chat_id: int) -> str | None:
    """Кто-то открыл ссылку. Решаем, админ ли это.

    Возвращает текст ответа боту, или `None`, если такого кода нет вовсе —
    тогда отвечать нечего, это не наш `/start`.
    """
    row = await identity_repo.admin_login_by_code(s, code)
    if row is None:
        return None
    if row.used_at is not None or _now() > row.expires_at:
        return "Код входа уже использован или просрочен. Запроси новый на странице входа."

    link = await notify_repo.link_by_chat(s, chat_id)
    if link is None:
        # Намеренно одинаковый текст с отказом по правам: по ответу бота
        # нельзя понять, существует ли админ и кто он.
        return "Нет доступа."
    user = await identity_repo.user_by_id(s, link.user_id)
    if user is None or not user.is_admin:
        log.warning("попытка входа в админку из чата %s", chat_id)
        return "Нет доступа."

    row.user_id = user.id
    row.chat_id = chat_id
    row.confirmed_at = _now()
    await s.flush()
    log.info("вход в админку подтверждён: %s", user.email)
    return "Вход в админку подтверждён. Вернись на страницу — она уже открыта."


async def claim(
    s: AsyncSession, code: str, user_agent: str | None, ip: str | None
) -> dict[str, Any]:
    """Страница спрашивает: подтвердили уже или нет.

    Пока нет — отвечаем «ждём». Как только да — открываем обычную сессию
    и гасим код: он одноразовый, и второй раз этим же кодом не войти.
    """
    row = await identity_repo.admin_login_by_code(s, code)
    if row is None:
        raise AppError("not_found", "Код входа не найден.", status.HTTP_404_NOT_FOUND)
    if row.used_at is not None:
        raise AppError(
            "already_done",
            "Этот код уже использован. Запроси новый.",
            status.HTTP_409_CONFLICT,
        )
    if _now() > row.expires_at:
        raise AppError(
            "login_expired",
            "Код входа просрочен. Запроси новый.",
            status.HTTP_409_CONFLICT,
        )
    if row.confirmed_at is None or row.user_id is None:
        return {"state": "waiting", "tokens": None}

    user = await identity_repo.user_by_id(s, row.user_id)
    if user is None or not user.is_admin:  # pragma: no cover — признак сняли между делом
        raise AppError("forbidden", "Нет доступа.", status.HTTP_403_FORBIDDEN)

    _, session_token, csrf_token = await identity_service.open_session_for(
        s, user, user_agent, ip
    )
    row.used_at = _now()
    await s.flush()
    return {
        "state": "ready",
        "tokens": (session_token, csrf_token),
        "email": user.email,
    }


async def require_admin(s: AsyncSession, user_id: uuid.UUID) -> None:
    """Пускать в админские ручки только админа.

    Отдельная проверка, а не зависимость в роутере: признак живёт в identity,
    и спрашивать его должен тот, кто и так ходит в несколько модулей.
    """
    user = await identity_repo.user_by_id(s, user_id)
    if user is None or not user.is_admin:
        # 404, а не 403: по ответу не должно быть видно, что такие ручки есть.
        raise AppError("not_found", "Не найдено.", status.HTTP_404_NOT_FOUND)


# --- пользователи ---


async def users(s: AsyncSession) -> list[dict[str, Any]]:
    """Список пользователей со счётчиками из каждого модуля."""
    rows = await identity_repo.all_users(s)
    out: list[dict[str, Any]] = []
    for user in rows:
        out.append(
            {
                "id": str(user.id),
                "email": user.email,
                "created_at": user.created_at,
                "is_admin": user.is_admin,
                "trades": await trades_repo.count_of(s, user.id),
                "incidents": await incidents_repo.count_of(s, user.id),
                "entries": await daybook_repo.count_entries(s, user.id),
                "rules": await rules_repo.count_rules(s, user.id),
            }
        )
    return out


async def user_detail(s: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    user = await identity_repo.user_by_id(s, user_id)
    if user is None:
        raise AppError("not_found", "Пользователь не найден.", status.HTTP_404_NOT_FOUND)
    settings = await identity_repo.settings_of(s, user_id)
    connection = await source_repo.active_connection(s, user_id)
    notify = await notify_repo.link_of(s, user_id)
    contact = await notify_repo.contact_of(s, user_id)
    return {
        "id": str(user.id),
        "email": user.email,
        "created_at": user.created_at,
        "is_admin": user.is_admin,
        "timezone": settings.timezone if settings else None,
        "shadow_mode": settings.shadow_mode if settings else None,
        "source": connection.provider if connection else None,
        "telegram_linked": bool(notify and notify.chat_id),
        "buddy": contact.display_name if contact else None,
        "counts": {
            "trades": await trades_repo.count_of(s, user_id),
            "incidents": await incidents_repo.count_of(s, user_id),
            "entries": await daybook_repo.count_entries(s, user_id),
            "rules": await rules_repo.count_rules(s, user_id),
        },
    }


async def purge(
    s: AsyncSession, user_id: uuid.UUID, parts: list[str]
) -> dict[str, int]:
    """Вычистить выбранное. Пользователь остаётся.

    **Это исключение из ТЗ 9.2**, где инциденты, нарушения и блокировки
    объявлены неудаляемыми без оговорок. Решение Влада от 04.10: админ
    может чистить. Цена названа вслух — трейдер, знающий, что историю можно
    стереть, теряет то трение, ради которого сервис существует, — и поэтому
    исключение записано в ТЗ, а не спрятано в коде.
    """
    unknown = [p for p in parts if p not in PARTS]
    if unknown:
        raise AppError(
            "validation_failed",
            f"Неизвестно, что чистить: {', '.join(unknown)}.",
            UNPROCESSABLE,
        )
    user = await identity_repo.user_by_id(s, user_id)
    if user is None:
        raise AppError("not_found", "Пользователь не найден.", status.HTTP_404_NOT_FOUND)

    done: dict[str, int] = {}
    # Порядок важен: инциденты ссылаются на правила, сделки — ни на что,
    # поэтому сначала производное, потом исходное.
    if "incidents" in parts:
        done["incidents"] = await incidents_repo.wipe_user(s, user_id)
        await rules_repo.wipe_user(s, user_id, keep_rules=True)
    if "streak" in parts:
        done["streak"] = await streaks_repo.wipe_user(s, user_id)
    if "trades" in parts:
        done["trades"] = await trades_repo.wipe_user(s, user_id)
        await rules_repo.wipe_user(s, user_id, keep_rules=True)
    if "diary" in parts:
        done["diary"] = await daybook_repo.wipe_user(s, user_id)
    if "notifications" in parts:
        await notify_repo.wipe_user(s, user_id)
        done["notifications"] = 1
    if "source" in parts:
        await source_repo.wipe_user(s, user_id)
        done["source"] = 1
    log.warning("админ вычистил у %s: %s", user.email, ", ".join(parts))
    return done


async def delete_user(s: AsyncSession, user_id: uuid.UUID) -> str:
    """Удалить пользователя целиком, со всем, что на нём висит."""
    user = await identity_repo.user_by_id(s, user_id)
    if user is None:
        raise AppError("not_found", "Пользователь не найден.", status.HTTP_404_NOT_FOUND)
    if user.is_admin:
        # Снести себя из своей же админки — это потерять вход в неё.
        raise AppError(
            "forbidden",
            "Админа удалить нельзя. Сними признак админа и повтори.",
            status.HTTP_403_FORBIDDEN,
        )

    email = user.email
    await incidents_repo.wipe_user(s, user_id)
    await rules_repo.wipe_user(s, user_id, keep_rules=False)
    await streaks_repo.wipe_user(s, user_id)
    await trades_repo.wipe_user(s, user_id)
    await daybook_repo.wipe_user(s, user_id)
    await notify_repo.wipe_user(s, user_id)
    await source_repo.wipe_user(s, user_id)
    await identity_repo.delete_user(s, user_id)
    log.warning("админ удалил пользователя %s", email)
    return email
