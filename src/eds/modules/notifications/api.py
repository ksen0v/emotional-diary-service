"""HTTP модуля notifications: бот, привязка, доверенное лицо, тексты.

Контракты — Архитектура ч.2 §3.9 и §3.10, экраны — прототипы `Settings.dc.html`
(карточка «Telegram и доверенное лицо») и `NotifyTexts.dc.html`.

Одно отличие от §3.9, названное вслух: переключателей каналов здесь нет.
Прототип показывает один тумблер «Уведомления в Telegram», и он уже живёт
в настройках как `telegram_enabled` — решение Влада от 30.09. Экран блокировки
каналом не считается: он не отправляется, а показывается, и выключать его
нечем.
"""

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.notifications import repo, service
from eds.platform import auth, db
from eds.platform.errors import not_found

router = APIRouter(prefix="/api/v1", tags=["notify"])


# --- резолверы для платформы ---
# Так модуль rules узнаёт про подтверждённое доверенное лицо, а модуль
# notifications — ничего не узнаёт про правила.


async def _resolve_notify(s: AsyncSession, user_id: uuid.UUID) -> dict:
    link = await service.link_state(s, user_id)
    bot = await service.bot_state()
    return {
        "bot_installed": bool(bot["installed"]),
        "linked": link["state"] == "linked",
        "buddy": await service.buddy_state(s, user_id),
    }


auth.register_notify(_resolve_notify)


# --- формы ---


class BotIn(BaseModel):
    token: str


class BotOut(BaseModel):
    bot: dict[str, Any]


class LinkOut(BaseModel):
    code: str
    bot_url: str
    bot_username: str
    expires_at: dt.datetime


class SettingsOut(BaseModel):
    bot: dict[str, Any]
    telegram: dict[str, Any]
    contact: dict[str, Any] | None
    signals_this_month: int
    notes: dict[str, str]


class ContactIn(BaseModel):
    handle: str
    display_name: str | None = None


class ContactOut(BaseModel):
    contact: dict[str, Any]


class RemovalOut(BaseModel):
    contact: dict[str, Any]


class ContactTemplateIn(BaseModel):
    body: str | None = None


class TemplatesOut(BaseModel):
    templates: list[dict[str, Any]]


class TemplateOut(BaseModel):
    template: dict[str, Any]


class TemplateIn(BaseModel):
    body: str


class PreviewOut(BaseModel):
    rendered: str
    sample: dict[str, Any]
    to: str


def _contact_out(row, *, invite_url: str | None, now: dt.datetime) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "handle": row.handle,
        "display_name": row.display_name,
        "status": row.status,
        "consent_at": row.consent_at,
        "invited_at": row.invited_at,
        "invite_url": invite_url,
        "removal_effective_at": row.removal_effective_at,
        "template": row.template,
        "awaiting_consent": row.status != service.CONFIRMED,
    }


# --- настройки ---


@router.get("/notify/settings", response_model=SettingsOut)
async def notify_settings(
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> SettingsOut:
    now = dt.datetime.now(dt.UTC)
    contact = await repo.contact_of(s, user.user_id)
    bot = await service.bot_state()
    link = await service.link_state(s, user.user_id)

    # Экран не врёт: если канала нет, он должен сказать, куда уходят алерты
    # сейчас, а не молчать. Строку собирает сервер — она же стоит в правилах.
    if not bot["installed"]:
        state_note = (
            "Токен бота не задан в окружении сервиса: "
            "алерты пишутся в журнал."
        )
    elif not prefs.telegram_enabled:
        state_note = "Уведомления в Telegram выключены: алерты видны только на экране."
    elif link["state"] != "linked":
        state_note = "Бот не привязан: алерты пишутся в журнал сервиса."
    elif prefs.shadow_mode:
        state_note = "Режим наблюдения: уведомления не отправляются."
    else:
        state_note = "Алерты уходят в Telegram."

    return SettingsOut(
        bot=bot,
        telegram={**link, "enabled": prefs.telegram_enabled},
        contact=(
            None
            if contact is None
            else _contact_out(
                contact,
                invite_url=await service.invite_url(s, contact),
                now=now,
            )
        ),
        signals_this_month=await service.signals_this_month(s, user.user_id, now),
        notes={
            "state": state_note,
            "removal": (
                "Отключение контакта вступает в силу через 24 часа. "
                "Во время активного инцидента снять его нельзя."
            ),
            "privacy": (
                "Друг видит только факт срабатывания правила. "
                "PnL, сделки и записи дневника ему не отправляются."
            ),
        },
    )


# --- привязка ---


@router.post("/notify/telegram/link", response_model=LinkOut)
async def start_link(
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> LinkOut:
    data = await service.start_link(s, user.user_id)
    await s.commit()
    return LinkOut(**data)


@router.delete("/notify/telegram", status_code=status.HTTP_204_NO_CONTENT)
async def unlink(
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> Response:
    await service.unlink(s, user.user_id)
    await s.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- доверенное лицо ---


@router.post(
    "/notify/contacts", response_model=ContactOut, status_code=status.HTTP_201_CREATED
)
async def invite_contact(
    body: ContactIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ContactOut:
    """Пригласить доверенное лицо: сервис выдаёт ссылку, пересылает трейдер.

    Бот не может написать первым тому, кто его не запускал, — поэтому
    приглашение и выглядит как ссылка, а не как отправленное сообщение.
    """
    row = await service.invite(s, user.user_id, body.handle, body.display_name)
    url = await service.invite_url(s, row)
    await s.commit()
    return ContactOut(
        contact=_contact_out(row, invite_url=url, now=dt.datetime.now(dt.UTC))
    )


@router.post("/notify/contacts/{contact_id}/invite", response_model=ContactOut)
async def resend_invite(
    contact_id: uuid.UUID,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ContactOut:
    """Новая ссылка взамен старой — кнопка «Отправить снова» из прототипа."""
    current = await repo.contact_by_id(s, user.user_id, contact_id)
    if current is None:
        raise not_found("Доверенное лицо не найдено.")
    row = await service.invite(s, user.user_id, current.handle, current.display_name)
    url = await service.invite_url(s, row)
    await s.commit()
    return ContactOut(
        contact=_contact_out(row, invite_url=url, now=dt.datetime.now(dt.UTC))
    )


@router.delete(
    "/notify/contacts/{contact_id}",
    response_model=RemovalOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def remove_contact(
    contact_id: uuid.UUID,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> RemovalOut:
    """Не удаляет сразу: сутки задержки из ТЗ 6.8."""
    blocked = await auth.has_active_lock(s, user.user_id)
    row = await service.request_removal(
        s, user.user_id, contact_id, blocked=blocked
    )
    await s.commit()
    return RemovalOut(
        contact=_contact_out(row, invite_url=None, now=dt.datetime.now(dt.UTC))
    )


@router.delete("/notify/contacts/{contact_id}/removal", response_model=RemovalOut)
async def cancel_removal(
    contact_id: uuid.UUID,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> RemovalOut:
    row = await service.cancel_removal(s, user.user_id, contact_id)
    await s.commit()
    return RemovalOut(
        contact=_contact_out(row, invite_url=None, now=dt.datetime.now(dt.UTC))
    )


@router.put("/notify/contacts/{contact_id}/template", response_model=ContactOut)
async def put_contact_template(
    contact_id: uuid.UUID,
    body: ContactTemplateIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ContactOut:
    row = await service.set_contact_template(s, user.user_id, body.body)
    await s.commit()
    return ContactOut(
        contact=_contact_out(row, invite_url=None, now=dt.datetime.now(dt.UTC))
    )


# --- тексты ---


@router.get("/notify/templates", response_model=TemplatesOut)
async def list_templates(
    user: auth.CurrentUser = Depends(auth.current_user),
    s: AsyncSession = Depends(db.session),
) -> TemplatesOut:
    return TemplatesOut(templates=await service.templates_out(s, user.user_id))


@router.put("/notify/templates/{key}", response_model=TemplateOut)
async def put_template(
    key: str,
    body: TemplateIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> TemplateOut:
    out = await service.put_template(s, user.user_id, key, body.body)
    await s.commit()
    return TemplateOut(template=out)


@router.delete("/notify/templates/{key}", response_model=TemplateOut)
async def reset_template(
    key: str,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> TemplateOut:
    """Вернуть стандартный текст."""
    out = await service.reset_template(s, user.user_id, key)
    await s.commit()
    return TemplateOut(template=out)


@router.post("/notify/templates/{key}/preview", response_model=PreviewOut)
async def preview_template(
    key: str,
    body: TemplateIn,
    _user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    __: None = Depends(auth.check_csrf),
) -> PreviewOut:
    return PreviewOut(**service.preview_out(key, body.body, prefs.trader_name))
