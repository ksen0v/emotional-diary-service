"""HTTP модуля source: подключения, словарь тегов, лента фейкового источника."""

import datetime as dt
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.source import repo, service
from eds.modules.source.models import Account, Connection, Tag
from eds.platform import auth, db

router = APIRouter(prefix="/api/v1", tags=["source"])


class AccountOut(BaseModel):
    id: uuid.UUID
    external_id: str
    name: str
    exchange: str | None
    market: str | None


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
    last_error: str | None
    accounts: list[AccountOut]


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


def _connection_out(row: Connection, accounts: list[Account]) -> ConnectionOut:
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
        accounts = await repo.accounts_of(s, row.id)
        out.append(_connection_out(row, accounts))
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


@router.post("/source/connections/{connection_id}/activate", response_model=ConnectionOut)
async def activate(
    connection_id: uuid.UUID,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ConnectionOut:
    connection = await service.activate(s, user.user_id, connection_id)
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
