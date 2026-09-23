"""HTTP оркестрации: то, что задевает несколько модулей.

Сверка стоит здесь, а не в source и не в trades: она соединяет источник,
настройки пользователя и приём сделок, то есть по определению не принадлежит
ни одному модулю.
"""

import contextlib
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import diary, pipeline, today
from eds.app import streaks as app_streaks
from eds.modules.daybook import periods
from eds.modules.daybook import repo as daybook_repo
from eds.modules.daybook import service as daybook
from eds.modules.streaks import repo as streaks_repo
from eds.modules.streaks import service as streaks_service
from eds.modules.trades import repo as trades_repo
from eds.platform import auth, db

router = APIRouter(prefix="/api/v1", tags=["sync"])


@router.get("/today")
async def today_screen(
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Весь главный экран одним запросом (Архитектура ч.2 §3.5).

    Без response_model: половина блоков появляется на следующих шагах, и
    описывать их схемой сейчас значило бы фиксировать форму того, чего нет.
    """
    data = await today.build(s, user.user_id, prefs)
    # Чтение, которое пишет: закрывает сессии дней, которые уже кончились.
    # Это не побочный эффект ради удобства, а работа процесса границы дня,
    # которого пока нет, поэтому коммит здесь обязателен.
    await s.commit()
    return data


@router.post("/sync")
async def sync(
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Забрать сделки у активного источника и принять их.

    На шаге 4 этот же путь будет вызываться по расписанию и после обрыва потока;
    кнопка в интерфейсе остаётся как способ проверить руками.
    """
    try:
        report = await pipeline.sync(s, user.user_id)
    except Exception:
        # Неудачную сверку тоже надо сохранить: без записи в журнале
        # на вопрос «почему сделки не приехали» нечем ответить.
        with contextlib.suppress(Exception):
            await s.commit()
        raise
    await s.commit()
    return report.as_dict()


# --- дневник ---


class EntryIn(BaseModel):
    """Запись дневника. Все поля необязательны: запись можно начать с одной оценки."""

    score: int | None = Field(default=None, ge=1, le=5)
    status: str | None = None
    tags: list[str] | None = None
    body: str | None = None


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


def _default_range(level: str, today: dt.date) -> tuple[dt.date, dt.date]:
    """Диапазон по умолчанию: столько, сколько влезает в один экран уровня.

    Считает сервер, потому что границы периодов — его правило: фронт, считающий
    «последние двенадцать недель» сам, начнёт неделю не с того дня.
    """
    if level == periods.DAY:
        start = today.replace(day=1)
        return start, periods.last_day_of_month(start)
    if level == periods.WEEK:
        return today - dt.timedelta(weeks=11), today
    return (today.replace(day=1) - dt.timedelta(days=334)).replace(day=1), today


@router.get("/entries")
async def entries(
    level: str = Query(default="day", pattern="^(day|week|month)$"),
    since: dt.date | None = Query(default=None, alias="from"),
    until: dt.date | None = Query(default=None, alias="to"),
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Записи дневника с фактами периода (Архитектура ч.2 §3.5)."""
    day = today.today_of(prefs)
    default_since, default_until = _default_range(level, day)
    return await diary.list_entries(
        s,
        user.user_id,
        prefs,
        level=level,
        since=since or default_since,
        until=until or default_until,
        today=day,
    )


@router.put("/entries/{level}/{period_start}")
async def put_entry(
    level: str,
    period_start: dt.date,
    body: EntryIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Создать или обновить запись. Через 48 часов — только комментарий (ТЗ 9.2)."""
    row = await daybook.upsert_entry(
        s,
        user.user_id,
        level,
        period_start,
        score=body.score,
        status=body.status,
        tags=body.tags,
        body=body.body,
        timezone=prefs.timezone,
        cutoff=prefs.day_cutoff,
        today=today.today_of(prefs),
    )
    # Запись дня — одно из условий зачёта (ТЗ 7.1), поэтому пересчитываем
    # стрик сразу за этот день: иначе «зачтён» в дневнике появлялся бы
    # только после следующего открытия главной.
    if row.level == periods.DAY:
        await app_streaks.refresh(
            s, user.user_id, prefs, today=today.today_of(prefs), days=[row.period_start]
        )
    await s.commit()
    return await diary.entry_with_facts(s, user.user_id, row)


@router.post("/entries/{entry_id}/comments", status_code=201)
async def comment_entry(
    entry_id: uuid.UUID,
    body: CommentIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Дописать к записи. Комментарии не ограничены по времени и не правятся."""
    comment = await daybook.comment_entry(s, user.user_id, entry_id, body.body)
    await s.commit()
    return {
        "id": str(comment.id),
        "body": comment.body,
        "created_at": comment.created_at.isoformat(),
    }


# --- пост-сессионный разбор и стрик ---


class ReviewIn(BaseModel):
    day: dt.date
    plan_followed: str = Field(pattern="^(yes|partial|no)$")
    pull_text: str | None = None
    execution_score: int | None = Field(default=None, ge=1, le=5)
    takeaway: str | None = None


class FreezeIn(BaseModel):
    day: dt.date


@router.get("/reviews/{day}")
async def get_review(
    day: dt.date,
    user: auth.CurrentUser = Depends(auth.current_user),
    s: AsyncSession = Depends(db.session),
) -> dict:
    row = await daybook_repo.review_of(s, user.user_id, day)
    day_row = await daybook_repo.day_of(s, user.user_id, day)
    return {
        "day": day.isoformat(),
        "review": daybook.review_out(row),
        "state": day_row.review_state if day_row else "none",
        "session_closed_at": day_row.session_closed_at if day_row else None,
    }


@router.post("/reviews", status_code=201)
async def post_review(
    body: ReviewIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Заполнить разбор. Пока он не заполнен, новый чек не выдаётся (ТЗ 5.4).

    Ответ показывает, как изменился стрик: разбор входит в условия зачёта дня,
    и увидеть последствие сразу — единственный способ связать одно с другим.
    """
    day = today.today_of(prefs)
    before = (await streaks_repo.ensure_state(s, user.user_id)).current
    review = await daybook.submit_review(
        s,
        user.user_id,
        body.day,
        plan_followed=body.plan_followed,
        pull_text=body.pull_text,
        execution_score=body.execution_score,
        takeaway=body.takeaway,
    )
    state = await app_streaks.refresh(s, user.user_id, prefs, today=day)
    await s.commit()
    return {
        "review": daybook.review_out(review),
        "streak": {"current": state.current, "previous": before, "best": state.best},
    }


@router.get("/streak")
async def streak(
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Серия дисциплины и полоска за 30 дней (Архитектура ч.2 §3.8)."""
    day = today.today_of(prefs)
    await app_streaks.refresh(s, user.user_id, prefs, today=day)
    out = await streaks_service.state_out(s, user.user_id, day)
    await s.commit()
    return out


@router.post("/streak/freeze")
async def freeze_day(
    body: FreezeIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Заморозить день: он не рвёт серию и не удлиняет её (ТЗ 7.2)."""
    day = today.today_of(prefs)
    summary = (
        await trades_repo.day_summaries(s, user.user_id, body.day, body.day)
    ).get(body.day)
    await streaks_service.freeze(
        s,
        user.user_id,
        body.day,
        day,
        has_violations=bool(summary and summary["violations"] > 0),
    )
    await s.commit()
    return await streaks_service.state_out(s, user.user_id, day)
