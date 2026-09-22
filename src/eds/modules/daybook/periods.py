"""Периоды дневника и срок правки записи. Чистые функции.

Границы периода считает сервер, а не фронт: неделя начинается с понедельника,
месяц — с первого числа, и если это посчитать в двух местах, записи за одну
и ту же неделю окажутся в двух разных периодах.
"""

import datetime as dt

from eds.contracts.trading_time import day_ends_at
from eds.platform.errors import UNPROCESSABLE, AppError

DAY = "day"
WEEK = "week"
MONTH = "month"
LEVELS = (DAY, WEEK, MONTH)

# Запись правится 48 часов, дальше — только комментарий (ТЗ 9.2).
EDITABLE_HOURS = 48


def period_end(level: str, period_start: dt.date) -> dt.date:
    if level == DAY:
        return period_start
    if level == WEEK:
        return period_start + dt.timedelta(days=6)
    if level == MONTH:
        return last_day_of_month(period_start)
    raise AppError("validation_failed", f"Неизвестный уровень «{level}».", 400)


def last_day_of_month(day: dt.date) -> dt.date:
    if day.month == 12:
        return dt.date(day.year, 12, 31)
    return dt.date(day.year, day.month + 1, 1) - dt.timedelta(days=1)


def normalize_start(level: str, period_start: dt.date) -> dt.date:
    """Привести начало периода к настоящему началу.

    Не ошибка, а приведение: фронт может прислать любой день недели, и
    отказывать здесь значило бы заставлять его считать календарь самостоятельно —
    то есть второй раз, по своим правилам.
    """
    if level == DAY:
        return period_start
    if level == WEEK:
        return period_start - dt.timedelta(days=period_start.weekday())
    if level == MONTH:
        return period_start.replace(day=1)
    raise AppError("validation_failed", f"Неизвестный уровень «{level}».", 400)


def bounds(level: str, period_start: dt.date) -> tuple[dt.date, dt.date]:
    start = normalize_start(level, period_start)
    return start, period_end(level, start)


def editable_until(
    level: str, period_start: dt.date, timezone: str, cutoff: dt.time
) -> dt.datetime:
    """Момент, после которого запись только комментируется.

    48 часов считаются от конца периода, а не от момента создания: иначе
    запись, сделанную через два дня, можно было бы править ещё двое суток,
    и «неизменяемость истории» зависела бы от того, когда трейдер сел писать.
    """
    _, end = bounds(level, period_start)
    return day_ends_at(end, timezone, cutoff) + dt.timedelta(hours=EDITABLE_HOURS)


def is_future(level: str, period_start: dt.date, today: dt.date) -> bool:
    start, _ = bounds(level, period_start)
    return start > today


def check_level(level: str) -> str:
    if level not in LEVELS:
        raise AppError(
            "validation_failed",
            "Уровень записи: день, неделя или месяц.",
            UNPROCESSABLE,
        )
    return level
