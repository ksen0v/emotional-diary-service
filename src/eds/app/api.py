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
from eds.modules.daybook import periods
from eds.modules.daybook import service as daybook
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
