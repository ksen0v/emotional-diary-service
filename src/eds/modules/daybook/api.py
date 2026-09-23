"""HTTP модуля daybook: состав чека, прохождение чека, закрытие сессии."""

import datetime as dt

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.trading_time import trading_day
from eds.modules.daybook import presets, service
from eds.platform import auth, db

router = APIRouter(prefix="/api/v1", tags=["daybook"])


def today_of(prefs: auth.UserPrefs) -> dt.date:
    return trading_day(dt.datetime.now(dt.UTC), prefs.timezone, prefs.day_cutoff)


class QuestionOut(BaseModel):
    id: str
    text: str
    short: str
    min: int
    max: int
    labels: dict[str, str]
    hint: str
    inverted: bool


class QuestionsOut(BaseModel):
    questions: list[QuestionOut]
    pass_score: int
    min_score: int
    max_score: int
    already_done: bool


class CheckIn(BaseModel):
    answers: dict[str, int] = Field(min_length=1, max_length=20)


class WeakOut(BaseModel):
    id: str
    short: str
    points: int
    max: int


class CheckOut(BaseModel):
    day: dt.date
    score: int
    max_score: int
    pass_score: int
    min_score: int
    verdict: str
    session_opened_at: dt.datetime | None
    message: str
    restrictions: dict | None
    weak: list[WeakOut]


class SessionClosedOut(BaseModel):
    closed_at: dt.datetime
    review: dict


@router.get("/premarket/questions", response_model=QuestionsOut)
async def premarket_questions(
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> QuestionsOut:
    """Состав чека и пороги. Вопросы отдаёт сервер, а не хардкодит фронт."""
    data = await service.catalog(
        s,
        user.user_id,
        today_of(prefs),
        pass_score=prefs.pass_score,
        min_score=prefs.min_score,
    )
    return QuestionsOut(**data)


@router.post("/premarket/checks", response_model=CheckOut)
async def submit_check(
    body: CheckIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> CheckOut:
    """Пройти чек. Один раз в день — перепройти нельзя (ТЗ 5.2)."""
    day = today_of(prefs)
    # Сессии прошедших дней закрываем до чека: день, который кончился, не должен
    # оставаться открытым только потому, что трейдер не заходил.
    await service.close_finished_days(
        s, user.user_id, day, timezone=prefs.timezone, cutoff=prefs.day_cutoff
    )
    result = await service.submit(
        s,
        user.user_id,
        day,
        body.answers,
        pass_score=prefs.pass_score,
        min_score=prefs.min_score,
    )
    await s.commit()
    return CheckOut(**result)


@router.post("/session/close", response_model=SessionClosedOut)
async def close_session(
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> SessionClosedOut:
    """Закрыть сессию раньше границы дня, чтобы разбор случился сегодня."""
    day = today_of(prefs)
    row = await service.close_session(s, user.user_id, day)
    await s.commit()
    return SessionClosedOut(
        closed_at=row.session_closed_at,
        review={"state": row.review_state, "day": day.isoformat()},
    )


# --- дневник и разбор ---


class PresetsOut(BaseModel):
    statuses: list[str]
    tags: list[str]


@router.get("/diary/presets", response_model=PresetsOut)
async def diary_presets(
    _: auth.CurrentUser = Depends(auth.current_user),
) -> PresetsOut:
    """Пресеты статусов и ментальных тегов (ТЗ 5.1, 5.3).

    Подсказки, а не справочник: статус и теги — свободный текст, и трейдер
    пишет своими словами, если наши не подходят.
    """
    return PresetsOut(**presets.as_dict())
