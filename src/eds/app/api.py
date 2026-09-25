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
from eds.modules.incidents import repo as incidents_repo
from eds.modules.streaks import repo as streaks_repo
from eds.modules.streaks import service as streaks_service
from eds.modules.trades import api as trades_api
from eds.modules.trades import repo as trades_repo
from eds.modules.trades import service as trades_service
from eds.platform import auth, db
from eds.platform.errors import AppError

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

    Этот же путь будет вызываться по расписанию и после обрыва потока;
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


@router.post("/session/close")
async def close_session(
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Закрыть сессию раньше границы дня, чтобы разбор случился сегодня.

    Стоит в оркестрации, а не в daybook: пока идёт блокировка, закрывать
    сессию нельзя — иначе закрытие стало бы способом её снять, — а про
    блокировки знает другой модуль.
    """
    day = today.today_of(prefs)
    lock = await incidents_repo.active_lock(s, user.user_id)
    row = await daybook.close_session(s, user.user_id, day, lock_active=lock is not None)
    await s.commit()
    return {
        "closed_at": row.session_closed_at,
        "review": {"state": row.review_state, "day": day.isoformat()},
    }


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


@router.post("/positions/refresh")
async def refresh_positions(
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Перечитать открытые позиции и досчитать просадку с учётом нереализованного.

    Тот же путь вызывается по расписанию раз в минуту при открытой сессии;
    кнопка остаётся как способ проверить руками — ровно как у сверки.
    """
    out = await pipeline.refresh_positions(s, user.user_id)
    await s.commit()
    return out


# --- своя разметка нарушений ---


class MarkingIn(BaseModel):
    marking: str = Field(pattern="^(violation|clean)$")


@router.put("/trades/{trade_id}/marking")
async def mark_trade(
    trade_id: uuid.UUID,
    body: MarkingIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Отметить сделку «не по системе» — когда источник не отдаёт теги.

    Стоит в оркестрации, потому что задевает четыре модуля: отметка меняет
    сделку, поднимает SR-1, включает блокировку и пересчитывает стрик.

    **Движок вызывается синхронно, в этом же запросе, и ответ несёт
    последствия.** Это требование контракта (Архитектура ч.2 §3.4), и оно
    не про удобство: разметка может мгновенно включить блокировку, и если
    фронт узнает о ней из фонового события, трейдер успеет увидеть обычный
    экран вместо экрана блокировки. Живого обновления у нас ещё нет, так что
    ответ на собственный запрос — единственный способ не соврать.
    """
    provides_tags = await auth.source_provides_tags(s, user.user_id)
    trade = await trades_repo.by_id(s, user.user_id, trade_id)
    if trade is None:
        raise AppError("not_found", "Сделка не найдена.", 404)

    # Историю переписать нельзя (ТЗ 9.2). Снять отметку, из которой уже
    # родился инцидент, — это и есть попытка переписать: инцидент останется,
    # а сделка, его породившая, окажется чистой, и объяснить инцидент будет
    # нечем. Поставить более строгую отметку можно всегда.
    if body.marking == "clean" and trade.marking == "violation":
        born = await _incident_of_trade(s, user.user_id, trade)
        if born is not None:
            raise AppError(
                "already_marked",
                "Из этой отметки уже записан инцидент, и снять её нельзя: "
                "история не переписывается.",
                409,
                {"incident_id": str(born.id), "day": trade.trading_day.isoformat()},
            )

    before = (await streaks_repo.ensure_state(s, user.user_id)).current
    updated, effects = await trades_service.mark_by_user(
        s, user.user_id, trade_id, body.marking, source_provides_tags=provides_tags
    )

    day = updated.trading_day
    report = pipeline.engine.EngineReport()
    if effects.get("changed"):
        await pipeline.engine.run_day(s, user.user_id, prefs, day, report=report)
        await app_streaks.refresh(
            s, user.user_id, prefs, today=today.today_of(prefs), days=[day]
        )

    state = await streaks_repo.ensure_state(s, user.user_id)
    lock = await incidents_repo.active_lock(s, user.user_id)
    born = await _incident_of_trade(s, user.user_id, updated)

    computed, _confidence = await trades_service.marking_metrics(
        s, user.user_id, trades_service.period_days("month", today.today_of(prefs))
    )
    await s.commit()

    tags = await trades_repo.tags_of(s, [updated.id])
    return {
        "trade": trades_api.trade_out(updated, tags.get(updated.id, [])).model_dump(
            mode="json"
        ),
        "effects": {
            "changed": bool(effects.get("changed")),
            "marking_before": effects.get("marking_before"),
            "incident_opened": (
                {
                    "id": str(born.id),
                    "code": born.code,
                    "day": born.day.isoformat(),
                    "outcome": born.outcome,
                }
                if born is not None
                else None
            ),
            "lock_started": (
                {
                    "id": str(lock.id),
                    "window_until": lock.window_until.isoformat(),
                    "timer_until": (
                        lock.timer_until.isoformat() if lock.timer_until else None
                    ),
                }
                if lock is not None and lock.state == "active"
                else None
            ),
            "streak": {"current": state.current, "previous": before},
            "recomputed_days": [day.isoformat()],
            "engine": report.as_dict(),
        },
        "metrics": computed.as_dict(),
    }


async def _incident_of_trade(s: AsyncSession, user_id: uuid.UUID, trade) -> object | None:
    """Инцидент, родившийся из отметки этой сделки.

    Ищем по дню сделки и её идентификатору в `details`, а не по коду: живая
    ветка SR-1 пишет `violation`, ретроветка — `retro_tag`, и оба означают
    «инцидент из этой отметки уже есть».
    """
    for row in await incidents_repo.incidents_of_day(s, user_id, trade.trading_day):
        if str((row.details or {}).get("trade_id")) == str(trade.id):
            return row
    return None
