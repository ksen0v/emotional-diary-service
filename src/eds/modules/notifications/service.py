"""Логика уведомлений: бот, привязка, доверенное лицо, тексты, очередь.

Модуль отвечает на вопрос «как сообщить», и только на него. О чём сообщать,
решают incidents и оркестрация (Архитектура ч.1 §2), поэтому здесь нет ни
одного знания о правилах, блокировках и стриках — сюда приходят ключ события
и готовые значения подстановок.

**Гейт стоит на постановке в очередь, а не на отправке.** Причина в режиме
наблюдения: уведомление, не отправленное потому, что блокировки выключены,
не должно улететь через час, когда трейдер режим выключит. Поэтому решение
«не отправляем» принимается один раз, в момент события, и записывается
в очередь строкой `skipped` с причиной. «Не смогли» и «не стали» — разные
вещи, и в журнале они выглядят по-разному.
"""

import datetime as dt
import logging
import secrets
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.notifications import repo, templates
from eds.modules.notifications.models import (
    BUDDY,
    CONFIRMED,
    FAILED,
    LINKED,
    PENDING,
    QUEUED,
    SELF,
    SENT,
    SKIPPED,
    ContactRow,
    LinkRow,
    OutboundRow,
)
from eds.modules.notifications.telegram.client import (
    AUTH,
    PERMANENT,
    WAIT,
    BotClient,
    TelegramFailure,
)
from eds.platform import auth, crypto
from eds.platform.errors import UNPROCESSABLE, AppError, not_found

log = logging.getLogger("eds.notify")

# Код привязки живёт недолго: он же уходит в ссылку, а ссылка может попасть
# куда угодно. Пятнадцати минут хватает, чтобы дойти до телефона.
LINK_TTL_MIN = 15
# Приглашение другу живёт дольше: его пересылают, и друг может открыть ссылку
# вечером. Сутки — разумный предел, дальше проще выдать новую.
INVITE_TTL_HOURS = 24
# Сутки задержки на отключение контакта (ТЗ 6.8).
REMOVAL_DELAY_HOURS = 24
# Сколько ждать между просьбами подтвердить снятие (Архитектура ч.2 §3.7):
# друга нельзя завалить просьбами в тильте.
BUDDY_COOLDOWN_SEC = 600
# Сколько раз пробуем отправить, прежде чем признать неудачу.
MAX_SEND_ATTEMPTS = 5

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _code() -> str:
    """Код вида 4F2C-91AB — как в прототипе: его диктуют и набирают руками."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


# --- бот ---


async def bot_state(s: AsyncSession) -> dict[str, Any]:
    row = await repo.bot(s)
    if row is None:
        return {"installed": False, "username": None, "updated_at": None}
    return {
        "installed": True,
        "username": row.username,
        "updated_at": row.updated_at,
    }


async def bot_username(s: AsyncSession) -> str | None:
    row = await repo.bot(s)
    return row.username if row else None


async def bot_token(s: AsyncSession) -> str | None:
    """Токен для клиента. Наружу не отдаётся никогда — только в процесс."""
    row = await repo.bot(s)
    if row is None:
        return None
    return crypto.decrypt(row.token_encrypted)


async def save_token(
    s: AsyncSession, user_id: uuid.UUID, token: str, *, api: Any | None = None
) -> dict[str, Any]:
    """Сохранить токен бота, предварительно спросив у Telegram, чей он.

    Проверка обязательна, и не ради аккуратности: имя бота показывается
    на экране и уходит в ссылку-приглашение. Выдуманное имя в ссылке —
    это приглашение, которое никуда не ведёт.
    """
    cleaned = (token or "").strip()
    if not cleaned:
        raise AppError("validation_failed", "Токен бота пустой.", UNPROCESSABLE)
    if not crypto.available():
        raise AppError(
            "secret_key_missing",
            "Ключ шифрования не задан, поэтому хранить токен бота нельзя. "
            "Он лежит в переменной EDS_SECRET_KEY.",
            409,
        )

    client = BotClient(cleaned, api=api)
    try:
        identity = await client.me()
    except TelegramFailure as failure:
        if failure.kind == AUTH:
            raise AppError(
                "bot_token_rejected",
                "Telegram не принял токен: проверь, что скопирован весь токен "
                "и он не отозван в @BotFather.",
                UNPROCESSABLE,
            ) from failure
        raise AppError(
            "telegram_unavailable",
            f"Telegram не ответил: {failure.message}",
            503,
        ) from failure
    finally:
        await client.close()

    await repo.save_bot(
        s,
        token_encrypted=crypto.encrypt(cleaned),
        key_version=crypto.KEY_VERSION,
        username=identity.username,
        bot_id=identity.id,
        installed_by=user_id,
    )
    return await bot_state(s)


async def delete_token(s: AsyncSession) -> None:
    await repo.delete_bot(s)


# --- привязка аккаунта ---


async def start_link(s: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """Выдать код привязки и ссылку на бота."""
    username = await bot_username(s)
    if username is None:
        raise AppError(
            "bot_not_installed",
            "Токен бота не вставлен, поэтому привязывать нечего.",
            409,
        )
    row = await repo.link_of(s, user_id)
    if row is None:
        row = LinkRow(user_id=user_id)
    row.link_code = _code()
    row.code_expires_at = _now() + dt.timedelta(minutes=LINK_TTL_MIN)
    row.state = PENDING
    row.chat_id = None
    row.linked_at = None
    await repo.save_link(s, row)
    return {
        "code": row.link_code,
        "bot_url": f"https://t.me/{username}?start={row.link_code}",
        "bot_username": username,
        "expires_at": row.code_expires_at,
    }


async def complete_link(
    s: AsyncSession, code: str, chat_id: int
) -> uuid.UUID | None:
    """Бот получил `/start <код>`: привязать чат к пользователю.

    Идемпотентно: повтор того же кода после привязки ничего не меняет,
    потому что код гасится. Telegram присылает обновление заново, пока мы
    не подтвердили его сдвигом offset, и второй раз эта ветка обязана
    отработать спокойно.
    """
    row = await repo.link_by_code(s, code.strip())
    if row is None:
        return None
    if row.code_expires_at is not None and _now() > row.code_expires_at:
        return None
    row.chat_id = chat_id
    row.state = LINKED
    row.linked_at = _now()
    row.link_code = None
    row.code_expires_at = None
    await repo.save_link(s, row)
    return row.user_id


async def unlink(s: AsyncSession, user_id: uuid.UUID) -> None:
    await repo.delete_link(s, user_id)


async def link_state(s: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    row = await repo.link_of(s, user_id)
    if row is None:
        return {"state": "unlinked", "linked_at": None, "code": None}
    return {
        "state": row.state if row.state == LINKED else "unlinked",
        "linked_at": row.linked_at,
        "code": row.link_code,
        "code_expires_at": row.code_expires_at,
    }


# --- доверенное лицо ---


async def invite(
    s: AsyncSession, user_id: uuid.UUID, handle: str, display_name: str | None
) -> ContactRow:
    """Пригласить доверенное лицо.

    **Почему приглашение не отправляет сервис.** Бот Telegram не может
    написать первым тому, кто его не запускал, и не умеет искать человека
    по @username. Поэтому сервис выдаёт ссылку, а пересылает её трейдер сам —
    решение Влада от 30.09. Прототип показывает кнопку «Пригласить», и она
    остаётся на месте: меняется не экран, а то, что происходит по нажатию.
    """
    cleaned = (handle or "").strip()
    if not cleaned.startswith("@"):
        cleaned = "@" + cleaned.lstrip("@")
    if len(cleaned) < 3:
        raise AppError(
            "validation_failed", "Укажи телеграм доверенного лица.", UNPROCESSABLE
        )

    existing = await repo.contact_of(s, user_id)
    if existing is not None and existing.status == CONFIRMED:
        raise AppError(
            "already_done",
            "Доверенное лицо уже подтвердило согласие. "
            "Чтобы заменить его, сначала удали текущее.",
            409,
        )

    row = existing or ContactRow(id=uuid.uuid4(), user_id=user_id)
    row.handle = cleaned
    row.display_name = (display_name or "").strip() or cleaned.lstrip("@")
    row.status = PENDING
    row.invite_code = "B-" + _code()
    row.invited_at = _now()
    row.consent_at = None
    row.chat_id = None
    row.removal_effective_at = None
    return await repo.save_contact(s, row)


async def invite_url(s: AsyncSession, contact: ContactRow) -> str | None:
    username = await bot_username(s)
    if username is None or contact.invite_code is None:
        return None
    return f"https://t.me/{username}?start={contact.invite_code}"


async def confirm_contact(
    s: AsyncSession, code: str, chat_id: int
) -> ContactRow | None:
    """Друг открыл ссылку и нажал «Подтверждаю» — двойное согласие получено."""
    row = await repo.contact_by_invite(s, code.strip())
    if row is None:
        return None
    if _now() > row.invited_at + dt.timedelta(hours=INVITE_TTL_HOURS):
        return None
    row.chat_id = chat_id
    row.status = CONFIRMED
    row.consent_at = _now()
    row.invite_code = None
    return await repo.save_contact(s, row)


async def request_removal(
    s: AsyncSession, user_id: uuid.UUID, contact_id: uuid.UUID, *, blocked: bool
) -> ContactRow:
    """Отключить контакт — через сутки, и не во время активного инцидента.

    Обе границы из ТЗ 6.8 и обе по одной причине: в тильте первым желанием
    будет отключить того, кто может остановить.
    """
    row = await repo.contact_by_id(s, user_id, contact_id)
    if row is None:
        raise not_found("Доверенное лицо не найдено.")
    if blocked:
        raise AppError(
            "lock_active",
            "Идёт инцидент. Пока он не закончится, отключить доверенное лицо нельзя.",
            409,
        )
    row.removal_effective_at = _now() + dt.timedelta(hours=REMOVAL_DELAY_HOURS)
    return await repo.save_contact(s, row)


async def cancel_removal(
    s: AsyncSession, user_id: uuid.UUID, contact_id: uuid.UUID
) -> ContactRow:
    """Отмена отключения разрешена мгновенно — она работает в нужную сторону."""
    row = await repo.contact_by_id(s, user_id, contact_id)
    if row is None:
        raise not_found("Доверенное лицо не найдено.")
    row.removal_effective_at = None
    return await repo.save_contact(s, row)


async def apply_due_removals(s: AsyncSession, now: dt.datetime) -> int:
    """Отложенные отключения, у которых истекли сутки."""
    rows = await repo.due_removals(s, now)
    for row in rows:
        await repo.delete_contact(s, row.id)
    return len(rows)


async def set_contact_template(
    s: AsyncSession, user_id: uuid.UUID, body: str | None
) -> ContactRow:
    """Персональный текст для конкретного друга.

    Перебивает общий `buddy_signal`: двум разным людям пишут по-разному.
    Проверяется тем же правилом ТЗ 9.5 — сумм в нём нет.
    """
    row = await repo.contact_of(s, user_id)
    if row is None:
        raise not_found("Доверенное лицо не найдено.")
    if body is None or not body.strip():
        row.template = None
        return await repo.save_contact(s, row)
    template = templates.BY_KEY["buddy_signal"]
    row.template = templates.validate_body(
        body, allowed=template.placeholders, buddy=True
    )
    return await repo.save_contact(s, row)


async def buddy_state(s: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """Резолвер для платформы: есть ли контакт и подтверждено ли согласие."""
    row = await repo.contact_of(s, user_id)
    if row is None:
        return {"exists": False, "confirmed": False, "display_name": None}
    return {
        "exists": True,
        "confirmed": row.status == CONFIRMED,
        "display_name": row.display_name or row.handle,
    }


# --- тексты ---


async def template_body(
    s: AsyncSession, user_id: uuid.UUID, key: str, *, contact: ContactRow | None = None
) -> str:
    """Текст, который сейчас действует: свой, персональный или дефолт."""
    template = templates.BY_KEY[key]
    if key == "buddy_signal" and contact is not None and contact.template:
        return contact.template
    row = await repo.template_of(s, user_id, key)
    return row.body if row is not None else template.default


async def templates_out(s: AsyncSession, user_id: uuid.UUID) -> list[dict[str, Any]]:
    saved = await repo.templates_of(s, user_id)
    out: list[dict[str, Any]] = []
    for template in templates.CATALOG:
        row = saved.get(template.key)
        body = row.body if row is not None else template.default
        out.append(
            {
                "key": template.key,
                "name": template.name,
                "when": template.when,
                "body": body,
                "default_body": template.default,
                "is_customized": row is not None,
                "placeholders": list(template.placeholders),
                "channel": template.channel,
                "to": template.to,
                "buddy": template.buddy,
                "preview": templates.preview(body)[0],
                "max_length": templates.BODY_MAX,
            }
        )
    return out


async def put_template(
    s: AsyncSession, user_id: uuid.UUID, key: str, body: str
) -> dict[str, Any]:
    cleaned = templates.validate(key, body)
    template = templates.BY_KEY[key]
    if cleaned == template.default:
        # Ровно дефолт — значит своей строки нет. Иначе улучшенный дефолт
        # в будущем до этого трейдера не доедет, хотя он ничего не менял.
        await repo.delete_template(s, user_id, key)
    else:
        await repo.upsert_template(s, user_id, key, cleaned)
    return await template_out(s, user_id, key)


async def reset_template(
    s: AsyncSession, user_id: uuid.UUID, key: str
) -> dict[str, Any]:
    if key not in templates.BY_KEY:
        raise not_found("Такого события нет.")
    await repo.delete_template(s, user_id, key)
    return await template_out(s, user_id, key)


async def template_out(
    s: AsyncSession, user_id: uuid.UUID, key: str
) -> dict[str, Any]:
    template = templates.BY_KEY[key]
    row = await repo.template_of(s, user_id, key)
    body = row.body if row is not None else template.default
    return {
        "key": key,
        "name": template.name,
        "when": template.when,
        "body": body,
        "default_body": template.default,
        "is_customized": row is not None,
        "placeholders": list(template.placeholders),
        "channel": template.channel,
        "to": template.to,
        "buddy": template.buddy,
        "preview": templates.preview(body)[0],
        "max_length": templates.BODY_MAX,
    }


def preview_out(key: str, body: str, trader_name: str = "") -> dict[str, Any]:
    """Предпросмотр на подставных данных.

    Рендерит сервер, а не экран: подстановки форматируются по нашим правилам
    (время в таймзоне трейдера, деньги с двумя знаками), и предпросмотр обязан
    показывать именно то, что придёт (Архитектура ч.2 §3.10).
    """
    template = templates.BY_KEY.get(key)
    if template is None:
        raise not_found("Такого события нет.")
    cleaned = templates.validate(key, body)
    extra = {"trader_name": trader_name} if trader_name else None
    rendered, used = templates.preview(cleaned, extra=extra)
    return {"rendered": rendered, "sample": used, "to": template.to}


# --- очередь ---


async def notify(
    s: AsyncSession,
    *,
    user_id: uuid.UUID,
    key: str,
    values: dict[str, Any],
    prefs: auth.UserPrefs,
    dedup_key: str | None = None,
    keyboard: dict[str, Any] | None = None,
) -> OutboundRow | None:
    """Поставить уведомление в очередь — или честно записать, почему нет."""
    template = templates.BY_KEY.get(key)
    if template is None:  # pragma: no cover — ключи приходят из кода
        raise not_found("Такого события нет.")

    contact = await repo.contact_of(s, user_id) if template.buddy else None
    body = await template_body(s, user_id, key, contact=contact)
    try:
        rendered = templates.render(body, values)
    except templates.PlaceholderMissing as missing:
        # Ошибка кода, а не трейдера: шаблон с неизвестной подстановкой
        # в базу не попадает. Не отправляем, но и не молчим — строка ложится
        # в очередь как неудачная, иначе уведомление исчезнет бесследно.
        log.error("нет значения для подстановки {%s} в шаблоне %s", missing, key)
        return await repo.enqueue(
            s,
            user_id=user_id,
            channel=template.channel,
            template=key,
            payload=values,
            body=body,
            chat_id=None,
            dedup_key=dedup_key,
            state=FAILED,
            error=f"нет значения для подстановки {{{missing}}}",
        )

    reason = await _gate(s, user_id, template.channel, prefs, contact)
    chat_id: int | None = None
    if reason is None:
        if template.channel == SELF:
            link = await repo.link_of(s, user_id)
            chat_id = link.chat_id if link else None
        else:
            chat_id = contact.chat_id if contact else None

    return await repo.enqueue(
        s,
        user_id=user_id,
        channel=template.channel,
        template=key,
        payload=values,
        body=rendered,
        chat_id=chat_id,
        keyboard=keyboard,
        dedup_key=dedup_key,
        state=SKIPPED if reason else QUEUED,
        error=reason,
    )


async def _gate(
    s: AsyncSession,
    user_id: uuid.UUID,
    channel: str,
    prefs: auth.UserPrefs,
    contact: ContactRow | None,
) -> str | None:
    """Причина не отправлять — или None.

    Режим наблюдения читается ровно здесь и в применении блокировки, и больше
    нигде (Архитектура ч.2 §5.10). Это и делает удаление флага дешёвым.
    """
    if prefs.shadow_mode:
        return "режим наблюдения: уведомления не отправляются"
    if not prefs.telegram_enabled:
        return "уведомления в Telegram выключены в настройках"
    if await repo.bot(s) is None:
        return "токен бота не вставлен"
    if channel == SELF:
        link = await repo.link_of(s, user_id)
        if link is None or link.state != LINKED or link.chat_id is None:
            return "Telegram не привязан"
        return None
    if contact is None:
        return "доверенного лица нет"
    if contact.status != CONFIRMED or contact.chat_id is None:
        return "доверенное лицо не подтвердило согласие"
    return None


async def dispatch(
    s: AsyncSession, client: BotClient, now: dt.datetime, limit: int = 20
) -> dict[str, int]:
    """Разослать то, что готово. Возвращает счётчики для журнала."""
    counts = {"sent": 0, "retry": 0, "failed": 0}
    for row in await repo.due(s, now, limit=limit):
        if row.chat_id is None:
            row.state = FAILED
            row.error = "некуда отправлять: чат неизвестен"
            await repo.save_outbound(s, row)
            counts["failed"] += 1
            continue
        try:
            await client.send(row.chat_id, row.body, row.keyboard)
        except TelegramFailure as failure:
            _after_failure(row, failure, now)
            counts["failed" if row.state == FAILED else "retry"] += 1
            continue
        row.state = SENT
        row.sent_at = now
        row.error = None
        row.attempts += 1
        await repo.save_outbound(s, row)
        counts["sent"] += 1
    return counts


def _after_failure(
    row: OutboundRow, failure: TelegramFailure, now: dt.datetime
) -> None:
    """Что делать с неудачной отправкой — по виду отказа, а не по счётчику.

    Разница существенная: «друг заблокировал бота» повторять бессмысленно
    ни сейчас, ни через час, а «Telegram не ответил» — ровно наоборот.
    """
    row.attempts += 1
    row.error = failure.message
    if failure.kind in (PERMANENT, AUTH):
        row.state = FAILED
        return
    if row.attempts >= MAX_SEND_ATTEMPTS:
        row.state = FAILED
        return
    pause = failure.retry_after if failure.kind == WAIT else min(5 * 2**row.attempts, 300)
    row.next_attempt_at = now + dt.timedelta(seconds=pause or 5)


async def signals_this_month(
    s: AsyncSession, user_id: uuid.UUID, now: dt.datetime
) -> int:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return await repo.buddy_signals_since(s, user_id, start)


__all__ = [
    "BUDDY",
    "BUDDY_COOLDOWN_SEC",
    "CONFIRMED",
    "SELF",
    "SENT",
    "apply_due_removals",
    "bot_state",
    "bot_token",
    "bot_username",
    "buddy_state",
    "cancel_removal",
    "complete_link",
    "confirm_contact",
    "delete_token",
    "dispatch",
    "invite",
    "invite_url",
    "link_state",
    "notify",
    "preview_out",
    "put_template",
    "request_removal",
    "reset_template",
    "save_token",
    "set_contact_template",
    "signals_this_month",
    "start_link",
    "template_body",
    "template_out",
    "templates_out",
    "unlink",
]
