"""HTTP модуля incidents: активная блокировка и её разбор (Архитектура ч.2 §3.7)."""

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.incidents import repo, service
from eds.platform import auth, db

router = APIRouter(prefix="/api/v1", tags=["incidents"])


class ActiveLockOut(BaseModel):
    lock: dict[str, Any] | None


class ReviewIn(BaseModel):
    q1: str = ""
    q2: str = ""
    q3: str = ""


class ReviewOut(BaseModel):
    satisfied: dict[str, bool | None]
    lock_state: str
    lifted: bool
    message: str


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
