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
from eds.modules.daybook import questions, repo
from eds.modules.daybook.models import TradingDay
from eds.platform import bus
from eds.platform.errors import UNPROCESSABLE, AppError

GREEN = "green"
RED = "red"
DENIED = "denied"

# Состояния дня из дизайна (машина состояний, §2). locked и review_pending
# появятся вместе с блокировками (шаг 9) и разбором (шаг 6): пока их некому
# выставить, и возвращать их было бы обещанием, которого сервис не выполняет.
STATE_NO_SOURCE = "no_source"
STATE_NO_CHECK = "no_check"
STATE_CHECK_FAILED = "check_failed"
STATE_TRADING = "trading"
STATE_SESSION_CLOSED = "session_closed"


def verdict_of(score: int, pass_score: int, min_score: int) -> str:
    """Три исхода по порогам из настроек трейдера (ТЗ 5.2)."""
    if score >= pass_score:
        return GREEN
    if score >= min_score:
        return RED
    return DENIED


def state_of(day: TradingDay | None, *, has_source: bool) -> str:
    """Состояние дня — одно слово, по которому фронт выбирает экран.

    Считает сервер: правила переходов — часть предметной логики, и
    продублированные во фронте они разойдутся при первой правке.
    """
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
    row = await repo.ensure_day(s, user_id, day)

    if row.review_state == "pending":
        # Не разобрал вчера — нет допуска сегодня (ТЗ 5.4).
        raise AppError(
            "review_pending",
            "Разбор за прошлую сессию не закрыт. Допуск не выдаётся, пока он не закрыт.",
            409,
        )
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
    # Разбор ставится в pending на шаге 6 — вместе с формой, которой его можно
    # закрыть. Ставить его сейчас значило бы запереть трейдера: незакрытый
    # разбор не даёт пройти чек, а закрыть его пока нечем.
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


def admission_out(row: TradingDay | None, check_at: dt.datetime | None) -> dict | None:
    if row is None or row.admission is None:
        return None
    return {
        "verdict": row.admission,
        "score": row.check_score,
        "checked_at": check_at,
    }
