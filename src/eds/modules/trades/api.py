"""HTTP модуля trades: лента и кривая дня (Архитектура ч.2 §3.4)."""

import base64
import binascii
import datetime as dt
import json
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.trades import repo, service
from eds.modules.trades.models import Trade, TradeTag
from eds.platform import auth, db
from eds.platform.errors import AppError, not_found

router = APIRouter(prefix="/api/v1", tags=["trades"])

MAX_LIMIT = 200


class TagOut(BaseModel):
    external_id: str
    name: str
    column_key: str


class TradeOut(BaseModel):
    id: uuid.UUID
    external_id: str
    source: str
    symbol: str
    side: str
    profit_usd: Decimal
    percent: Decimal | None
    account_return_pct: Decimal
    size_usd: Decimal | None
    leverage: Decimal | None
    duration_sec: int | None
    open_time: dt.datetime
    close_time: dt.datetime | None
    trading_day: dt.date
    is_significant: bool
    marking: str
    marked_by: str
    tags: list[TagOut]


class TotalsOut(BaseModel):
    count: int
    significant_count: int
    violations_count: int
    unmarked_count: int
    profit_usd: Decimal
    account_return_pct: Decimal
    coverage_pct: Decimal


class FeedOut(BaseModel):
    items: list[TradeOut]
    next_cursor: str | None
    has_more: bool
    totals: TotalsOut
    period: dict


class MarkingIn(BaseModel):
    marking: str


class MarkingEffects(BaseModel):
    changed: bool
    marking_before: str | None = None


class MarkedOut(BaseModel):
    trade: TradeOut
    effects: MarkingEffects
    metrics: dict


class MetricsOut(BaseModel):
    period: dict
    marking: dict
    confidence: dict


class CurvePointOut(BaseModel):
    at: dt.datetime
    trade_id: str
    equity_pct: Decimal
    peak_pct: Decimal
    drawdown_pct: Decimal


class CurveOut(BaseModel):
    day: dt.date
    points: list[CurvePointOut]
    unrealized: dict
    close: dict


def _encode_cursor(trade: Trade) -> str:
    raw = json.dumps({"ct": trade.close_time.isoformat(), "id": str(trade.id)})
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[dt.datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        return dt.datetime.fromisoformat(data["ct"]), uuid.UUID(data["id"])
    except (ValueError, KeyError, binascii.Error) as exc:
        raise AppError("bad_cursor", "Курсор списка повреждён.", 400) from exc


def trade_out(trade: Trade, tags: list[TradeTag]) -> TradeOut:
    return TradeOut(
        id=trade.id,
        external_id=trade.external_id,
        source=trade.source,
        symbol=trade.symbol,
        side=trade.side,
        profit_usd=service.quantize_money(trade.profit_usd),
        percent=service.quantize_pct(trade.percent),
        account_return_pct=service.quantize_pct(trade.account_return_pct),
        size_usd=service.quantize_money(trade.size_usd),
        leverage=service.quantize_pct(trade.leverage),
        duration_sec=trade.duration_sec,
        open_time=trade.open_time,
        close_time=trade.close_time,
        trading_day=trade.trading_day,
        is_significant=trade.is_significant,
        marking=trade.marking,
        marked_by=trade.marked_by,
        tags=[
            TagOut(external_id=t.external_id, name=t.name, column_key=t.column_key)
            for t in sorted(tags, key=lambda t: t.name)
        ],
    )


def _today(prefs: auth.UserPrefs) -> dt.date:
    """Текущий торговый день — тем же правилом, что и у сделки при приёме."""
    from eds.modules.trades.normalize import trading_day

    return trading_day(dt.datetime.now(dt.UTC), prefs.timezone, prefs.day_cutoff)


@router.get("/trades", response_model=FeedOut)
async def feed(
    period: str = Query(default="today", pattern="^(today|week|month|all)$"),
    filter: str = Query(default="all", pattern="^(all|unmarked|violations)$"),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> FeedOut:
    today = _today(prefs)
    days = service.period_days(period, today)
    marking = None if filter == "all" else filter

    rows, tags, has_more = await service.feed(
        s, user.user_id, days, marking, limit, _decode_cursor(cursor)
    )
    totals = await service.totals(s, user.user_id, days, marking)

    return FeedOut(
        items=[trade_out(row, tags.get(row.id, [])) for row in rows],
        next_cursor=_encode_cursor(rows[-1]) if rows and has_more else None,
        has_more=has_more,
        totals=TotalsOut(
            count=totals["count"],
            significant_count=totals["significant_count"],
            violations_count=totals["violations_count"],
            unmarked_count=totals["unmarked_count"],
            profit_usd=service.quantize_money(totals["profit_usd"]),
            account_return_pct=service.quantize_pct(totals["account_return_pct"]),
            coverage_pct=service.quantize_pct(totals["coverage_pct"]),
        ),
        period={
            "level": period,
            "from": days[0].isoformat() if days else None,
            "to": days[1].isoformat() if days else None,
            "today": today.isoformat(),
        },
    )


@router.get("/trades/day-curve", response_model=CurveOut)
async def day_curve(
    day: dt.date | None = None,
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> CurveOut:
    target = day or _today(prefs)
    points = await service.curve(s, user.user_id, target)
    last = points[-1] if points else None
    # Открытые позиции спрашиваем у платформы: модуль trades не должен знать,
    # что модуль source существует. Пунктир открытой позиции на кривой рисуется
    # по этому значению — и только за сегодня: у прошлого дня открытой позиции
    # уже нет, и подставить туда сегодняшнюю значило бы переписать историю.
    positions = (
        await auth.open_positions(s, user.user_id)
        if target == _today(prefs)
        else {"available": False, "pct": None}
    )
    return CurveOut(
        day=target,
        points=[
            CurvePointOut(
                at=p.at,
                trade_id=p.trade_id,
                equity_pct=service.quantize_pct(p.equity_pct),
                peak_pct=service.quantize_pct(p.peak_pct),
                drawdown_pct=service.quantize_pct(p.drawdown_pct),
            )
            for p in points
        ],
        # Источник без открытых позиций не даёт нереализованного убытка.
        # null, а не ноль: ноль означал бы «позиций нет», а это другое.
        unrealized={
            "available": bool(positions.get("available")),
            "pct": (
                service.quantize_pct(positions["pct"])
                if positions.get("pct") is not None
                else None
            ),
        },
        close={
            "equity_pct": service.quantize_pct(last.equity_pct) if last else Decimal("0"),
            "peak_pct": service.quantize_pct(last.peak_pct) if last else Decimal("0"),
            "max_drawdown_pct": (
                service.quantize_pct(max(p.drawdown_pct for p in points))
                if points
                else Decimal("0")
            ),
        },
    )


@router.get("/trades/metrics", response_model=MetricsOut)
async def marking_metrics(
    period: str = Query(default="month", pattern="^(today|week|month|all)$"),
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> MetricsOut:
    """Метрики разметки за период: покрытие, дисциплина, цена эмоций."""
    today = _today(prefs)
    days = service.period_days(period, today)
    computed, confidence = await service.marking_metrics(s, user.user_id, days)
    return MetricsOut(
        period={
            "level": period,
            "from": days[0].isoformat() if days else None,
            "to": days[1].isoformat() if days else None,
            "today": today.isoformat(),
        },
        marking=computed.as_dict(),
        confidence=confidence,
    )


# Эндпоинт разметки живёт в оркестрации (`app/api.py`), а не здесь.
# Причина не в удобстве: отметка «не по системе» мгновенно поднимает SR-1,
# блокировку и пересчёт стрика, то есть задевает rules, incidents и streaks.
# Собрать такой ответ из модуля trades нельзя, не узнав о трёх чужих схемах.
# Формы `MarkingIn`, `MarkedOut` и `trade_out` остаются здесь: это форма
# сделки, и она принадлежит этому модулю.


@router.get("/trades/{trade_id}", response_model=TradeOut)
async def one(
    trade_id: uuid.UUID,
    user: auth.CurrentUser = Depends(auth.current_user),
    s: AsyncSession = Depends(db.session),
) -> TradeOut:
    trade = await repo.by_id(s, user.user_id, trade_id)
    if trade is None:
        raise not_found("Сделка не найдена.")
    tags = await repo.tags_of(s, [trade.id])
    return trade_out(trade, tags.get(trade.id, []))
