"""HTTP модуля incidents: лента, активная блокировка и её разбор (ч.2 §3.7)."""

import base64
import datetime as dt
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.trading_time import trading_day
from eds.modules.incidents import repo, service
from eds.platform import auth, db
from eds.platform.errors import AppError

router = APIRouter(prefix="/api/v1", tags=["incidents"])

LIMIT_MAX = 200


class ActiveLockOut(BaseModel):
    lock: dict[str, Any] | None


class IncidentsOut(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None
    has_more: bool
    totals: dict[str, Any]
    period: dict[str, Any]


class ReviewIn(BaseModel):
    q1: str = ""
    q2: str = ""
    q3: str = ""


class ReviewOut(BaseModel):
    satisfied: dict[str, bool | None]
    lock_state: str
    lifted: bool
    message: str


def _encode(row) -> str:
    """Курсор непрозрачный: его формат — дело сервера (ч.2 §1.5)."""
    raw = json.dumps({"at": row.opened_at.isoformat(), "id": str(row.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return dt.datetime.fromisoformat(data["at"]), uuid.UUID(data["id"])
    except Exception as exc:
        raise AppError("validation_failed", "Курсор не разобран.", 400) from exc


def _window(period: str, today: dt.date) -> tuple[dt.date, dt.date, str]:
    """Окно ленты и подпись к сводке.

    По умолчанию месяц: в прототипе над лентой стоит «За сентябрь», то есть
    сводка считается за календарный месяц, а не за скользящие 30 дней.
    """
    if period == "week":
        since = today - dt.timedelta(days=today.weekday())
        return since, today, "За неделю"
    if period == "all":
        return dt.date(2000, 1, 1), today, "За всё время"
    since = today.replace(day=1)
    return since, today, f"За {service.MONTHS_NOM[today.month - 1]}"


@router.get("/incidents", response_model=IncidentsOut)
async def incidents(
    period: str = Query("month", pattern="^(week|month|all)$"),
    outcome: str = Query("all", pattern="^(all|kept|breached)$"),
    limit: int = Query(50, ge=1, le=LIMIT_MAX),
    cursor: str | None = None,
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> IncidentsOut:
    """Лента инцидентов (Архитектура ч.2 §3.7 + прототип `Incidents.dc.html`).

    Сводка считается по всей выборке фильтра, а не по странице: иначе полоса
    над лентой менялась бы при прокрутке.
    """
    today = trading_day(dt.datetime.now(dt.UTC), prefs.timezone, prefs.day_cutoff)
    since, until, label = _window(period, today)

    rows = await repo.page(
        s,
        user.user_id,
        since=since,
        until=until,
        outcome=outcome,
        limit=limit,
        cursor=_decode(cursor) if cursor else None,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]

    locks = await repo.locks_of(s, user.user_id, [row.id for row in rows])
    totals = await repo.totals(
        s, user.user_id, since=since, until=until, outcome=outcome
    )

    # Коэффициент дисциплины по инцидентам: доля соблюдённых среди
    # закончившихся (ТЗ 5.1, «Compliance блокировок»). Идущие инциденты в него
    # не входят — исход у них ещё не известен, и считать их соблюдёнными
    # заранее значило бы завышать показатель.
    closed = totals["kept"] + totals["breached"]
    totals["discipline_pct"] = (
        None if closed == 0 else round(totals["kept"] * 100 / closed, 1)
    )

    return IncidentsOut(
        items=[
            service.incident_out(row, locks.get(row.id), prefs.timezone)
            for row in rows
        ],
        next_cursor=_encode(rows[-1]) if has_more and rows else None,
        has_more=has_more,
        totals=totals,
        period={"label": label, "from": since.isoformat(), "to": until.isoformat()},
    )


@router.get("/locks/active", response_model=ActiveLockOut)
async def active_lock(
    user: auth.CurrentUser = Depends(auth.current_user),
    s: AsyncSession = Depends(db.session),
) -> ActiveLockOut:
    """Активная блокировка или null.

    Чтение, которое пишет: по дороге снимает блокировку, у которой всё
    выполнено, и закрывает ту, что пережила границу дня. Отдельного процесса
    границы дня пока нет — он появится вместе с уведомлениями, — а до тех пор
    ленивое закрытие даёт то же состояние данных.
    """
    now = dt.datetime.now(dt.UTC)
    lock = await repo.active_lock(s, user.user_id)
    if lock is not None:
        lock = await service.settle(s, user.user_id, lock, now)
    await s.commit()

    if lock is None or lock.state != "active":
        return ActiveLockOut(lock=None)

    review = await repo.review_of(s, lock.id)
    incident = await repo.by_id(s, user.user_id, lock.incident_id)
    return ActiveLockOut(
        lock=service.lock_out(
            lock, review is not None, now, service.breach_of(incident)
        )
    )


@router.post("/locks/{lock_id}/review", response_model=ReviewOut)
async def submit_lock_review(
    lock_id: uuid.UUID,
    body: ReviewIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> ReviewOut:
    """Заполнить разбор блокировки. Если это было последнее условие — снимаем."""
    now = dt.datetime.now(dt.UTC)
    lock, satisfied = await service.submit_review(
        s, user.user_id, lock_id, body.model_dump(), now
    )
    await s.commit()

    lifted = lock.state != "active"
    return ReviewOut(
        satisfied=satisfied,
        lock_state=lock.state,
        lifted=lifted,
        message=_message(lock, satisfied, lifted, now),
    )


def _message(lock, satisfied: dict[str, bool | None], lifted: bool, now) -> str:
    """Текст собирает сервер: фронт своих формулировок не сочиняет (ч.2 §1.3)."""
    if lifted:
        return "Разбор принят. Блокировка снята."
    if satisfied.get("timer") is False and lock.timer_until is not None:
        local = lock.timer_until.astimezone(dt.UTC)
        left = max(0, int((lock.timer_until - now).total_seconds()))
        minutes, seconds = divmod(left, 60)
        return (
            f"Разбор принят. Осталось: таймер {minutes:02d}:{seconds:02d}."
            if minutes or seconds
            else f"Разбор принят. Таймер до {local:%H:%M}."
        )
    return "Разбор принят."
