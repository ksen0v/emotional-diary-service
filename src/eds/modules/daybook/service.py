"""Пре-маркет чек, допуск и состояния торгового дня.

Главное правило модуля: сессия не существует, пока чек не пройден (ТЗ 5.2).
Второе: чек за день проходится один раз. Оба держатся не на уговоре с фронтом,
а здесь и в ограничениях базы — «перепройти нельзя» это и есть весь смысл
допуска, и обход этого правила обесценивает сервис целиком.
"""

import datetime as dt
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts import events as ev
from eds.contracts.trading_time import day_bounds
from eds.modules.daybook import periods, presets, questions, repo
from eds.modules.daybook.models import TradingDay
from eds.platform import bus
from eds.platform.errors import UNPROCESSABLE, AppError

GREEN = "green"
RED = "red"
DENIED = "denied"

# Состояния дня из дизайна (машина состояний, §2). locked появится вместе
# с блокировками (шаг 9): пока его некому выставить, и возвращать его было бы
# обещанием, которого сервис не выполняет.
STATE_NO_SOURCE = "no_source"
STATE_NO_CHECK = "no_check"
STATE_CHECK_FAILED = "check_failed"
STATE_TRADING = "trading"
STATE_SESSION_CLOSED = "session_closed"
STATE_REVIEW_PENDING = "review_pending"

MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def human_date(day: dt.date) -> str:
    """Дата словами — для текстов, которые читает трейдер."""
    return f"{day.day} {MONTHS_GENITIVE[day.month - 1]}"


def verdict_of(score: int, pass_score: int, min_score: int) -> str:
    """Три исхода по порогам из настроек трейдера (ТЗ 5.2)."""
    if score >= pass_score:
        return GREEN
    if score >= min_score:
        return RED
    return DENIED


def state_of(
    day: TradingDay | None,
    *,
    has_source: bool,
    pending_review_day: dt.date | None = None,
) -> str:
    """Состояние дня — одно слово, по которому фронт выбирает экран.

    Считает сервер: правила переходов — часть предметной логики, и
    продублированные во фронте они разойдутся при первой правке.

    Незакрытый разбор стоит первым сознательно: пока он не заполнен, других
    путей нет — ни чека, ни работы. Жёсткость в этом и есть смысл (ОВ-15).
    """
    if pending_review_day is not None:
        return STATE_REVIEW_PENDING
    if not has_source:
        return STATE_NO_SOURCE
    if day is None or day.admission is None:
        return STATE_NO_CHECK
    if day.admission == DENIED:
        return STATE_CHECK_FAILED
    if day.session_closed_at is not None:
        return STATE_SESSION_CLOSED
    return STATE_TRADING


def validate_answers(raw: dict[str, int]) -> dict[str, int]:
    """Ответы должны быть на все вопросы и в пределах шкалы.

    Неполный чек не имеет смысла: балл сравнивается с порогом, а порог задан
    для полного набора. Пропуск вопроса означал бы тихое занижение балла.
    """
    expected = set(questions.BY_ID)
    given = set(raw)
    if given != expected:
        missing = sorted(expected - given)
        extra = sorted(given - expected)
        raise AppError(
            "validation_failed",
            "Чек заполнен не полностью.",
            400,
            {"missing": missing, "unknown": extra},
        )
    clean: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise AppError("validation_failed", f"Ответ «{key}» не число.", 400)
        if not questions.MIN_ANSWER <= value <= questions.MAX_ANSWER:
            raise AppError(
                "validation_failed",
                f"Ответ «{key}» вне шкалы {questions.MIN_ANSWER}–{questions.MAX_ANSWER}.",
                UNPROCESSABLE,
            )
        clean[key] = value
    return clean


async def catalog(
    s: AsyncSession,
    user_id: uuid.UUID,
    day: dt.date,
    *,
    pass_score: int,
    min_score: int,
) -> dict:
    existing = await repo.check_of(s, user_id, day)
    return {
        "questions": questions.as_dicts(),
        "pass_score": pass_score,
        "min_score": min_score,
        "max_score": questions.MAX_SCORE,
        "already_done": existing is not None,
    }


async def submit(
    s: AsyncSession,
    user_id: uuid.UUID,
    day: dt.date,
    raw_answers: dict[str, int],
    *,
    pass_score: int,
    min_score: int,
) -> dict:
    answers = validate_answers(raw_answers)

    # Не разобрал вчера — нет допуска сегодня (ТЗ 5.4). Смотрим на прошедшие
    # дни, а не на сегодняшний: разбор за сегодня появляется только после
    # закрытия сессии, то есть когда чек уже пройден и мешать ему не может.
    blocked = await pending_review(s, user_id, day)
    if blocked is not None:
        raise AppError(
            "review_pending",
            f"Разбор за {human_date(blocked)} не заполнен. "
            "Допуск не выдаётся, пока он не закрыт.",
            409,
            {"day": blocked.isoformat()},
        )

    row = await repo.ensure_day(s, user_id, day)
    if row.admission is not None:
        raise AppError(
            "already_done",
            "Чек за сегодня уже пройден. Перепройти его нельзя — в этом и смысл допуска.",
            409,
            {"verdict": row.admission, "score": row.check_score},
        )

    score = questions.score_of(answers)
    verdict = verdict_of(score, pass_score, min_score)
    now = dt.datetime.now(dt.UTC)

    try:
        await repo.save_check(
            s, user_id, day, answers=answers, score=score, verdict=verdict
        )
    except IntegrityError as exc:
        # Уникальность по (user_id, day) — второй запрос в ту же секунду.
        # Проверка выше его не поймала бы, поэтому запрет стоит и в базе.
        raise AppError(
            "already_done",
            "Чек за сегодня уже пройден. Перепройти его нельзя — в этом и смысл допуска.",
            409,
        ) from exc

    row.admission = verdict
    row.check_score = score
    if verdict != DENIED:
        row.session_opened_at = now
    await s.flush()

    await bus.publish(
        s,
        ev.DAYBOOK_ADMISSION_DECIDED,
        {
            "user_id": str(user_id),
            "day": day.isoformat(),
            "verdict": verdict,
            "score": score,
            "pass_score": pass_score,
            "min_score": min_score,
        },
        dedup_key=f"admission:{user_id}:{day.isoformat()}",
    )
    if verdict != DENIED:
        await bus.publish(
            s,
            ev.DAYBOOK_SESSION_OPENED,
            {
                "user_id": str(user_id),
                "day": day.isoformat(),
                "verdict": verdict,
                "opened_at": now.isoformat(),
            },
            dedup_key=f"session-open:{user_id}:{day.isoformat()}",
        )

    return {
        "day": day,
        "score": score,
        "max_score": questions.MAX_SCORE,
        "pass_score": pass_score,
        "min_score": min_score,
        "verdict": verdict,
        "session_opened_at": row.session_opened_at,
        "message": _message(verdict, score, min_score),
        "restrictions": _restrictions(verdict),
        "weak": questions.weak_answers(answers),
    }


def _message(verdict: str, score: int, min_score: int) -> str:
    total = questions.MAX_SCORE
    if verdict == GREEN:
        return f"Допуск получен. {score} из {total}."
    if verdict == RED:
        return f"Допуск под риском. {score} из {total}."
    return f"Допуска на сегодня нет. {score} из {total}, порог допуска — {min_score}."


def _restrictions(verdict: str) -> dict | None:
    """Ограничения при красном допуске — текст, а не механика.

    Урезать размер позиции сервис не может: он читает дневник и ничего
    не отправляет на биржу. Обещать урезание значило бы врать в интерфейсе.
    """
    if verdict != RED:
        return None
    return {
        "text": "Рекомендация: половина обычного размера позиции.",
        "note": "Сервис не урезает размер сам — он только читает сделки. "
        "Это напоминание, а не ограничение.",
    }


async def close_session(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date, at: dt.datetime | None = None
) -> TradingDay:
    """Ручное закрытие сессии раньше границы дня.

    Нужно, чтобы разбор случился сегодня, а не завтра. Блокировка закрыть сессию
    не даст (шаг 9): иначе закрытие стало бы способом снять блокировку.
    """
    row = await repo.day_of(s, user_id, day)
    if row is None or row.session_opened_at is None:
        raise AppError("no_session", "Сессия за сегодня не открыта.", 409)
    if row.session_closed_at is not None:
        raise AppError("already_closed", "Сессия за сегодня уже закрыта.", 409)
    return await _close(s, user_id, row, at or dt.datetime.now(dt.UTC), reason="manual")


async def close_finished_days(
    s: AsyncSession,
    user_id: uuid.UUID,
    today: dt.date,
    *,
    timezone: str,
    cutoff: dt.time,
) -> list[dt.date]:
    """Закрыть сессии дней, которые уже кончились.

    Делается на чтении, а не по расписанию: отдельный процесс границы дня
    появится вместе с уведомлениями — им нужно сработать в момент границы,
    даже если трейдер не открыл страницу. До тех пор ленивое закрытие даёт
    то же состояние данных без процесса, который некому перезапустить.
    """
    closed: list[dt.date] = []
    for row in await repo.days_with_open_session(s, user_id, before=today):
        _, end = day_bounds(row.day, timezone, cutoff)
        await _close(s, user_id, row, end, reason="day_boundary")
        closed.append(row.day)
    return closed


async def _close(
    s: AsyncSession,
    user_id: uuid.UUID,
    row: TradingDay,
    at: dt.datetime,
    *,
    reason: str,
) -> TradingDay:
    row.session_closed_at = at
    # Сессия закрылась — разбор становится задачей, которая ждёт трейдера
    # (ТЗ 5.4). Ночью он не показывается: до следующего чека времени хватает,
    # а разбуженный разбор в три часа ночи никто не заполняет.
    if row.review_state == "none":
        row.review_state = "pending"
    await s.flush()
    await bus.publish(
        s,
        ev.DAYBOOK_DAY_CLOSED,
        {
            "user_id": str(user_id),
            "day": row.day.isoformat(),
            "closed_at": at.isoformat(),
            "reason": reason,
            "admission": row.admission,
        },
        dedup_key=f"day-closed:{user_id}:{row.day.isoformat()}",
    )
    return row


def admission_out(row: TradingDay | None, check) -> dict | None:
    """Допуск дня вместе с тем, какие ответы просадили балл.

    Просадившие ответы нужны экрану «нет допуска»: он открывается и после
    перезагрузки страницы, а не только сразу после чека, и без них там
    остаётся одно число без объяснения.
    """
    if row is None or row.admission is None:
        return None
    return {
        "verdict": row.admission,
        "score": row.check_score,
        "max_score": questions.MAX_SCORE,
        "checked_at": check.created_at if check else None,
        "weak": questions.weak_answers(check.answers) if check else [],
    }


# --- дневник ---

EntryOut = tuple["object", list[str], list["object"]]


def _clean_text(value: str | None, limit: int, field: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise AppError(
            "validation_failed", f"{field}: слишком длинный текст.", UNPROCESSABLE
        )
    return text


def _clean_tags(tags: list[str] | None) -> list[str]:
    """Теги — свободный текст, но без дублей и мусора.

    Пресеты сервис предлагает и не ограничивает ими: словарь состояний
    у каждого свой, а справочник превратил бы «допиши своё» в правку схемы.
    """
    if not tags:
        return []
    seen: list[str] = []
    for raw in tags:
        tag = raw.strip()
        if not tag:
            continue
        if len(tag) > presets.MAX_TAG_LENGTH:
            raise AppError(
                "validation_failed", "Тег слишком длинный.", UNPROCESSABLE
            )
        if tag not in seen:
            seen.append(tag)
    if len(seen) > presets.MAX_TAGS:
        raise AppError(
            "validation_failed",
            f"Тегов не больше {presets.MAX_TAGS}.",
            UNPROCESSABLE,
        )
    return seen


async def upsert_entry(
    s: AsyncSession,
    user_id: uuid.UUID,
    level: str,
    period_start: dt.date,
    *,
    score: int | None,
    status: str | None,
    tags: list[str] | None,
    body: str | None,
    timezone: str,
    cutoff: dt.time,
    today: dt.date,
):
    """Создать или обновить запись дневника.

    Через 48 часов после конца периода правка закрывается и остаётся только
    комментарий (ТЗ 9.2). Это не техническое ограничение, а смысл дневника:
    запись должна остаться тем, что трейдер думал тогда, а не тем, что он
    думает об этом сейчас.
    """
    periods.check_level(level)
    start, end = periods.bounds(level, period_start)

    if start > today:
        raise AppError(
            "period_in_future",
            "Запись за будущий период не имеет смысла.",
            UNPROCESSABLE,
        )
    if score is not None and not 1 <= score <= 5:
        raise AppError("validation_failed", "Оценка — от 1 до 5.", UNPROCESSABLE)

    row = await repo.entry_of(s, user_id, level, start)
    if row is None:
        row = await repo.create_entry(
            s,
            user_id,
            level=level,
            period_start=start,
            period_end=end,
            editable_until=periods.editable_until(level, start, timezone, cutoff),
        )
    elif dt.datetime.now(dt.UTC) > row.editable_until:
        raise AppError(
            "not_editable",
            "Запись старше 48 часов. Добавь комментарий.",
            409,
            {"comments_url": f"/api/v1/entries/{row.id}/comments"},
        )

    row.score = score
    row.status = _clean_text(status, presets.MAX_STATUS_LENGTH, "Статус")
    row.body = _clean_text(body, presets.MAX_BODY_LENGTH, "Текст записи")
    row.updated_at = dt.datetime.now(dt.UTC)
    await s.flush()
    await repo.replace_tags(s, row.id, _clean_tags(tags))
    return row


async def comment_entry(
    s: AsyncSession, user_id: uuid.UUID, entry_id: uuid.UUID, body: str
):
    """Дописать комментарий. Времени не ограничен и не редактируется (ТЗ 9.2)."""
    row = await repo.entry_by_id(s, user_id, entry_id)
    if row is None:
        raise AppError("not_found", "Запись не найдена.", 404)
    text = _clean_text(body, presets.MAX_COMMENT_LENGTH, "Комментарий")
    if text is None:
        raise AppError("validation_failed", "Комментарий пустой.", 400)
    return await repo.add_comment(s, row.id, text)


def entry_out(row, tags: list[str], comments: list) -> dict:
    now = dt.datetime.now(dt.UTC)
    return {
        "id": str(row.id),
        "level": row.level,
        "period_start": row.period_start.isoformat(),
        "period_end": row.period_end.isoformat(),
        "score": row.score,
        "status": row.status,
        "tags": tags,
        "body": row.body,
        "editable_until": row.editable_until.isoformat(),
        "editable": now <= row.editable_until,
        "comments": [
            {
                "id": str(c.id),
                "body": c.body,
                "created_at": c.created_at.isoformat(),
            }
            for c in comments
        ],
    }


# --- пост-сессионный разбор ---

PLAN_ANSWERS = ("yes", "partial", "no")


async def pending_review(
    s: AsyncSession, user_id: uuid.UUID, today: dt.date
) -> dt.date | None:
    """День, за который разбор не закрыт. Он блокирует новый чек (ТЗ 5.4)."""
    row = await repo.oldest_pending_review(s, user_id, before=today)
    return row.day if row else None


async def submit_review(
    s: AsyncSession,
    user_id: uuid.UUID,
    day: dt.date,
    *,
    plan_followed: str,
    pull_text: str | None,
    execution_score: int | None,
    takeaway: str | None,
):
    """Заполнить разбор за день.

    Разбор возможен только по закрытому дню: пока сессия идёт, разбирать нечего,
    а «разбор в середине дня» стал бы способом снять блокировку разбором заранее.
    """
    if plan_followed not in PLAN_ANSWERS:
        raise AppError(
            "validation_failed", "План: да, частично или нет.", UNPROCESSABLE
        )
    if execution_score is not None and not 1 <= execution_score <= 5:
        raise AppError(
            "validation_failed", "Оценка исполнения — от 1 до 5.", UNPROCESSABLE
        )

    row = await repo.day_of(s, user_id, day)
    if row is None or row.session_opened_at is None:
        raise AppError(
            "no_session",
            "В этот день сессия не открывалась — разбирать нечего.",
            UNPROCESSABLE,
        )
    if row.session_closed_at is None:
        raise AppError(
            "day_not_closed",
            "День ещё идёт. Разбор заполняется после закрытия сессии.",
            UNPROCESSABLE,
        )
    if await repo.review_of(s, user_id, day) is not None:
        raise AppError("already_done", "Разбор за этот день уже заполнен.", 409)

    review = await repo.save_review(
        s,
        user_id,
        day,
        plan_followed=plan_followed,
        pull_text=_clean_text(pull_text, presets.MAX_COMMENT_LENGTH, "Что дёрнуло"),
        execution_score=execution_score,
        takeaway=_clean_text(takeaway, presets.MAX_COMMENT_LENGTH, "Вывод"),
    )
    row.review_state = "done"
    await s.flush()

    await bus.publish(
        s,
        ev.DAYBOOK_REVIEW_COMPLETED,
        {
            "user_id": str(user_id),
            "day": day.isoformat(),
            "plan_followed": plan_followed,
            "execution_score": execution_score,
        },
        dedup_key=f"review:{user_id}:{day.isoformat()}",
    )
    return review


def review_out(row) -> dict | None:
    if row is None:
        return None
    return {
        "day": row.day.isoformat(),
        "plan_followed": row.plan_followed,
        "pull_text": row.pull_text,
        "execution_score": row.execution_score,
        "takeaway": row.takeaway,
        "created_at": row.created_at.isoformat(),
    }
