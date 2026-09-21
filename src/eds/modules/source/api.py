"""HTTP модуля source: подключения, словарь тегов, лента фейкового источника."""

import datetime as dt
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.source import repo, service
from eds.modules.source.models import (
    Account,
    Connection,
    RateLimitRow,
    ReconcileRun,
    Tag,
)
from eds.platform import auth, db

router = APIRouter(prefix="/api/v1", tags=["source"])


async def _resolve_capabilities(s: AsyncSession, user_id: uuid.UUID) -> dict:
    """Возможности активного источника — для платформы.

    Так модуль trades узнаёт, отдаёт ли источник теги, не импортируя source.
    """
    connection = await repo.active_connection(s, user_id)
    return dict(connection.capabilities) if connection else {}


auth.register_source(_resolve_capabilities)


class AccountOut(BaseModel):
    id: uuid.UUID
    external_id: str
    name: str
    exchange: str | None
    market: str | None


class ReconcileOut(BaseModel):
    """Последняя сверка. Нужна, чтобы «сделки не приехали» было чем объяснить."""

    started_at: dt.datetime
    finished_at: dt.datetime | None
    kind: str
    status: str
    trades_seen: int
    trades_new: int
    error: str | None


class RateLimitOut(BaseModel):
    limit: int | None
    remaining: int | None
    reset_at: dt.datetime | None


class ConnectionOut(BaseModel):
    id: uuid.UUID
    provider: str
    market: str | None
    key_masked: str | None
    is_active: bool
    state: str
    ingest_from: dt.datetime
    activated_at: dt.datetime | None
    capabilities: dict
    permissions: dict | None
    last_error: str | None
    accounts: list[AccountOut]
    last_reconcile: ReconcileOut | None = None
    rate_limit: RateLimitOut | None = None


class ConnectionsOut(BaseModel):
    connections: list[ConnectionOut]
    active_connection_id: uuid.UUID | None


class TagOut(BaseModel):
    external_id: str
    name: str
    column_key: str
    is_violation: bool


class TagsOut(BaseModel):
    tags: list[TagOut]
    available: bool


class ViolationTagsIn(BaseModel):
    violation_tag_ids: list[str]


class FakeTradeIn(BaseModel):
    """Форма dev-панели: сделка, которую фейковый источник «отдаст» при сверке."""

    symbol: str = Field(default="BTCUSDT", min_length=3, max_length=20)
    side: str = Field(default="long", pattern="^(long|short)$")
    profit_usd: Decimal = Field(default=Decimal("-84.20"))
    account_return_pct: Decimal = Field(default=Decimal("-0.68"))
    tags: list[str] = Field(default_factory=list, max_length=5)
    minutes_ago: int = Field(default=0, ge=0, le=60 * 24 * 30)
    duration_sec: int = Field(default=300, ge=1, le=60 * 60 * 24)


def _connection_out(
    row: Connection,
    accounts: list[Account],
    last_reconcile: ReconcileRun | None = None,
    rate_limit: RateLimitRow | None = None,
) -> ConnectionOut:
    return ConnectionOut(
        id=row.id,
        provider=row.provider,
        market=row.market,
        key_masked=row.key_masked,
        is_active=row.is_active,
        state=row.state,
        ingest_from=row.ingest_from,
        activated_at=row.activated_at,
        capabilities=row.capabilities,
        permissions=row.permissions,
        last_error=row.last_error,
        accounts=[
            AccountOut(
                id=a.id,
                external_id=a.external_id,
                name=a.name,
                exchange=a.exchange,
                market=a.market,
            )
            for a in accounts
        ],
        last_reconcile=(
            None
            if last_reconcile is None
            else ReconcileOut(
                started_at=last_reconcile.started_at,
                finished_at=last_reconcile.finished_at,
                kind=last_reconcile.kind,
                status=last_reconcile.status,
                trades_seen=last_reconcile.trades_seen,
                trades_new=last_reconcile.trades_new,
                error=last_reconcile.error,
            )
        ),
        rate_limit=(
            None
            if rate_limit is None
            else RateLimitOut(
                limit=rate_limit.limit_value,
                remaining=rate_limit.remaining,
                reset_at=rate_limit.reset_at,
            )
        ),
    )


async def _load_connection_out(s: AsyncSession, row: Connection) -> ConnectionOut:
    """Подключение со всем, что к нему приложено: счета, сверка, лимиты."""
    return _connection_out(
        row,
        await repo.accounts_of(s, row.id),
        await repo.last_reconcile_run(s, row.id),
        await repo.rate_limit_of(s, row.id),
    )


def _tag_out(tag: Tag) -> TagOut:
    return TagOut(
        external_id=tag.external_id,
        name=tag.name,
        column_key=tag.column_key,
        is_violation=tag.is_violation,
    )


@router.get("/source/connections", response_model=ConnectionsOut)
async def connections(
    user: auth.CurrentUser = Depends(auth.current_user),
    s: AsyncSession = Depends(db.session),
) -> ConnectionsOut:
    rows = await repo.connections_of(s, user.user_id)
    out = []
    active_id = None
    for row in rows:
        out.append(await _load_connection_out(s, row))
        if row.is_active:
            active_id = row.id
    return ConnectionsOut(connections=out, active_connection_id=active_id)


@router.post("/source/connections/fake", response_model=ConnectionOut)
async def connect_fake(
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ConnectionOut:
    """Подключить тестовый источник. Настоящие появятся на шагах 4 и 14."""
    connection = await service.connect_fake(s, user.user_id)
    accounts = await repo.accounts_of(s, connection.id)
    await s.commit()
    return _connection_out(connection, accounts)


class ActivateIn(BaseModel):
    """Подтверждение переключения источника (Архитектура ч.2 §3.3)."""

    confirm: bool = False


@router.post("/source/connections/{connection_id}/activate", response_model=ConnectionOut)
async def activate(
    connection_id: uuid.UUID,
    body: ActivateIn | None = None,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ConnectionOut:
    """Сделать источник активным.

    Переключение с одного источника на другой требует confirm: расчёты после
    него начинаются заново, и случайный клик не должен этого делать.
    """
    connection = await service.activate(
        s, user.user_id, connection_id, confirm=bool(body and body.confirm)
    )
    await s.commit()
    return await _load_connection_out(s, connection)


class CapabilitiesIn(BaseModel):
    provides_tags: bool


@router.patch("/source/connections/fake/capabilities", response_model=ConnectionOut)
async def set_fake_capabilities(
    body: CapabilitiesIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ConnectionOut:
    """Только для тестового источника: изображать источник без тегов."""
    connection = await service.set_fake_capabilities(
        s, user.user_id, provides_tags=body.provides_tags
    )
    accounts = await repo.accounts_of(s, connection.id)
    await s.commit()
    return _connection_out(connection, accounts)


@router.get("/source/tags", response_model=TagsOut)
async def tags(
    user: auth.CurrentUser = Depends(auth.current_user),
    s: AsyncSession = Depends(db.session),
) -> TagsOut:
    connection = await repo.active_connection(s, user.user_id)
    if connection is None:
        return TagsOut(tags=[], available=False)
    provides_tags = bool(connection.capabilities.get("provides_tags", False))
    rows = await repo.tags_of(s, connection.id) if provides_tags else []
    return TagsOut(tags=[_tag_out(row) for row in rows], available=provides_tags)


@router.put("/source/tags/violations", response_model=TagsOut)
async def set_violations(
    body: ViolationTagsIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> TagsOut:
    """Отметить теги нарушения. Переразметку сделок сделает консьюмер шины."""
    await service.set_violation_tags(s, user.user_id, body.violation_tag_ids)
    await s.commit()

    connection = await repo.active_connection(s, user.user_id)
    rows = await repo.tags_of(s, connection.id) if connection else []
    return TagsOut(tags=[_tag_out(row) for row in rows], available=True)


@router.post("/source/dev/trade")
async def push_fake_trade(
    body: FakeTradeIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    payload = await service.push_fake_trade(
        s,
        user.user_id,
        symbol=body.symbol,
        side=body.side,
        profit_usd=body.profit_usd,
        account_return_pct=body.account_return_pct,
        tags=body.tags,
        minutes_ago=body.minutes_ago,
        duration_sec=body.duration_sec,
    )
    await s.commit()
    return {"queued": payload["external_id"], "close_time": payload["close_time"]}


# --- подключение настоящего источника ключом ---


class ConnectIn(BaseModel):
    """Тело подключения (контракт §3.3). У TMM нет ни секрета, ни рынка."""

    provider: str = Field(pattern="^(tmm|binance)$")
    market: str | None = None
    key: str = Field(min_length=8, max_length=512)
    secret: str | None = None


class ProbeOut(BaseModel):
    """Что провайдер показал при подключении. Ничего из этого не сохраняется."""

    accounts: list[dict]
    entry_tags: list[dict]
    trades_seen: int
    sample: list[dict]
    tags_available: bool
    tags_problem: str | None
    accounts_from_trades: bool
    window_filter_honored: bool | None
    mapping_errors: list[str]


class WarningOut(BaseModel):
    code: str
    message: str


class ConnectOut(BaseModel):
    connection: ConnectionOut
    accounts: list[AccountOut]
    probe: ProbeOut
    warnings: list[WarningOut]


async def _connect_out(s: AsyncSession, result: dict) -> ConnectOut:
    connection = result["connection"]
    out = await _load_connection_out(s, connection)
    return ConnectOut(
        connection=out,
        accounts=out.accounts,
        probe=ProbeOut(**result["probe"].as_dict()),
        warnings=[WarningOut(**w) for w in result["warnings"]],
    )


@router.post("/source/connections", response_model=ConnectOut, status_code=201)
async def connect(
    body: ConnectIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ConnectOut:
    """Подключить источник по ключу.

    Ключ проверяется до создания подключения: если провайдер его отклонил,
    в настройках не остаётся источника, который никогда не заработает.
    """
    result = await service.connect(
        s,
        user.user_id,
        provider=body.provider,
        key=body.key,
        market=body.market,
        secret=body.secret,
    )
    await s.commit()
    return await _connect_out(s, result)


@router.post("/source/connections/{connection_id}/verify", response_model=ConnectOut)
async def verify(
    connection_id: uuid.UUID,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ConnectOut:
    """Проверить подключение. Точку отсчёта не двигает."""
    try:
        result = await service.verify(s, user.user_id, connection_id)
    except Exception:
        # Состояние «ошибка» и её текст должны сохраниться, иначе экран
        # источника покажет «подключено» у неработающего ключа.
        await s.commit()
        raise
    await s.commit()
    return await _connect_out(s, result)


@router.delete("/source/connections/{connection_id}", status_code=204)
async def delete(
    connection_id: uuid.UUID,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> None:
    """Удалить подключение. Сделки остаются: история неизменяема (ТЗ 9.2)."""
    await service.delete(s, user.user_id, connection_id)
    await s.commit()
