"""HTTP модуля identity (Архитектура ч.2 §3.2)."""

import datetime as dt
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.identity import repo, service
from eds.modules.identity.models import Session, Settings, User
from eds.platform import auth, db, security
from eds.platform.errors import AppError, unauthenticated

router = APIRouter(prefix="/api/v1", tags=["identity"])

# csrf-проверка общая для всего API и живёт в платформе
check_csrf = auth.check_csrf


# --- зависимости ---


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def current(
    request: Request, s: AsyncSession = Depends(db.session)
) -> service.Identity:
    token = request.cookies.get(security.SESSION_COOKIE)
    if not token:
        raise unauthenticated()
    identity = await service.identity_by_token(s, token)
    await s.commit()
    return identity


# --- резолверы для платформы ---
# Так другие модули получают пользователя и его настройки, не импортируя identity.


async def _resolve_user(s: AsyncSession, token: str) -> auth.CurrentUser:
    identity = await service.identity_by_token(s, token)
    return auth.CurrentUser(
        user_id=identity.user.id,
        email=identity.user.email,
        session_id=identity.session.id,
    )


async def _resolve_prefs(s: AsyncSession, user_id: uuid.UUID) -> auth.UserPrefs:
    row = await repo.settings_of(s, user_id)
    if row is None:
        raise AppError("not_found", "Настройки не найдены.", status.HTTP_404_NOT_FOUND)
    return auth.UserPrefs(
        timezone=row.timezone,
        day_cutoff=row.day_cutoff,
        significance_pct=row.significance_pct,
        shadow_mode=row.shadow_mode,
        pass_score=row.pass_score,
        min_score=row.min_score,
    )


auth.register(_resolve_user, _resolve_prefs)


# --- формы запросов и ответов ---


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class SettingsPatch(BaseModel):
    timezone: str | None = None
    day_cutoff: dt.time | None = None
    pass_score: int | None = None
    min_score: int | None = None
    significance_pct: Decimal | None = None
    telegram_enabled: bool | None = None
    shadow_mode: bool | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    created_at: dt.datetime


class SettingsOut(BaseModel):
    timezone: str
    day_cutoff: dt.time
    pass_score: int
    min_score: int
    significance_pct: Decimal
    active_account_id: uuid.UUID | None
    telegram_enabled: bool
    shadow_mode: bool


class MeOut(BaseModel):
    user: UserOut
    settings: SettingsOut
    modules_disabled: list[str]
    onboarding: dict


class SettingsPatched(BaseModel):
    settings: SettingsOut
    notice: str | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    created_at: dt.datetime
    last_seen_at: dt.datetime
    user_agent: str | None
    ip: str | None
    current: bool


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, created_at=user.created_at)


def _settings_out(row: Settings) -> SettingsOut:
    return SettingsOut(
        timezone=row.timezone,
        day_cutoff=row.day_cutoff,
        pass_score=row.pass_score,
        min_score=row.min_score,
        significance_pct=row.significance_pct,
        active_account_id=row.active_account_id,
        telegram_enabled=row.telegram_enabled,
        shadow_mode=row.shadow_mode,
    )


def _session_out(row: Session, current_id: uuid.UUID) -> SessionOut:
    return SessionOut(
        id=row.id,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        user_agent=row.user_agent,
        ip=str(row.ip) if row.ip else None,
        current=row.id == current_id,
    )


async def _me_body(s: AsyncSession, user: User) -> MeOut:
    settings_row = await repo.settings_of(s, user.id)
    if settings_row is None:
        raise AppError("not_found", "Настройки не найдены.", status.HTTP_404_NOT_FOUND)
    return MeOut(
        user=_user_out(user),
        settings=_settings_out(settings_row),
        modules_disabled=await repo.disabled_modules(s, user.id),
        # Шаги 2 и 4 наполнят это реальными признаками; пока подключать нечего.
        onboarding={"source_connected": False, "tags_mapped": False, "first_rule": False},
    )


# --- эндпоинты ---


@router.post("/auth/register", response_model=MeOut, status_code=status.HTTP_201_CREATED)
async def register(
    form: Credentials,
    request: Request,
    response: Response,
    s: AsyncSession = Depends(db.session),
) -> MeOut:
    identity, session_token, csrf_token = await service.register(
        s,
        email=str(form.email),
        password=form.password,
        user_agent=request.headers.get("user-agent"),
        ip=_client_ip(request),
    )
    body = await _me_body(s, identity.user)
    await s.commit()
    security.set_session_cookies(
        response, session_token, csrf_token, secure=security.is_https(request.url.scheme)
    )
    return body


@router.post("/auth/login", response_model=MeOut)
async def login(
    form: Credentials,
    request: Request,
    response: Response,
    s: AsyncSession = Depends(db.session),
) -> MeOut:
    identity, session_token, csrf_token = await service.login(
        s,
        email=str(form.email),
        password=form.password,
        user_agent=request.headers.get("user-agent"),
        ip=_client_ip(request),
    )
    body = await _me_body(s, identity.user)
    await s.commit()
    security.set_session_cookies(
        response, session_token, csrf_token, secure=security.is_https(request.url.scheme)
    )
    return body


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    identity: service.Identity = Depends(current),
    _: None = Depends(check_csrf),
    s: AsyncSession = Depends(db.session),
) -> Response:
    await service.logout(s, identity.session.id)
    await s.commit()
    security.clear_session_cookies(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeOut)
async def me(
    identity: service.Identity = Depends(current),
    s: AsyncSession = Depends(db.session),
) -> MeOut:
    return await _me_body(s, identity.user)


@router.patch("/me/settings", response_model=SettingsPatched)
async def patch_settings(
    patch: SettingsPatch,
    identity: service.Identity = Depends(current),
    _: None = Depends(check_csrf),
    s: AsyncSession = Depends(db.session),
) -> SettingsPatched:
    changes = patch.model_dump(exclude_unset=True, exclude_none=True)
    row, notice = await service.update_settings(s, identity.user.id, changes)
    await s.commit()
    return SettingsPatched(settings=_settings_out(row), notice=notice)


@router.get("/me/sessions", response_model=list[SessionOut])
async def sessions(
    identity: service.Identity = Depends(current),
    s: AsyncSession = Depends(db.session),
) -> list[SessionOut]:
    rows = await repo.live_sessions_of(s, identity.user.id)
    return [_session_out(row, identity.session.id) for row in rows]


@router.delete("/me/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID,
    identity: service.Identity = Depends(current),
    _: None = Depends(check_csrf),
    s: AsyncSession = Depends(db.session),
) -> Response:
    await service.revoke(s, identity.user.id, session_id)
    await s.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
