"""Периоды дневника и правило 48 часов — чистые функции.

Смысл теста в сроке правки. «Запись правится 48 часов» можно понять двумя
способами — от момента создания или от конца периода, — и разница видна только
на записи, сделанной с опозданием: в первом случае её можно править ещё двое
суток, и неизменяемость истории зависит от того, когда трейдер сел писать.
"""

import datetime as dt

import pytest

from eds.modules.daybook import periods
from eds.platform.errors import AppError

MSK = "Europe/Moscow"
MIDNIGHT = dt.time(0, 0)


def test_week_starts_on_monday_whatever_day_came() -> None:
    wednesday = dt.date(2026, 9, 23)
    assert periods.bounds("week", wednesday) == (
        dt.date(2026, 9, 21),
        dt.date(2026, 9, 27),
    )
    # Понедельник остаётся собой.
    assert periods.bounds("week", dt.date(2026, 9, 21))[0] == dt.date(2026, 9, 21)


def test_month_covers_the_whole_month() -> None:
    assert periods.bounds("month", dt.date(2026, 9, 23)) == (
        dt.date(2026, 9, 1),
        dt.date(2026, 9, 30),
    )
    assert periods.bounds("month", dt.date(2026, 2, 10))[1] == dt.date(2026, 2, 28)
    assert periods.bounds("month", dt.date(2028, 2, 10))[1] == dt.date(2028, 2, 29)
    assert periods.bounds("month", dt.date(2026, 12, 31))[1] == dt.date(2026, 12, 31)


def test_day_is_itself() -> None:
    day = dt.date(2026, 9, 19)
    assert periods.bounds("day", day) == (day, day)


def test_editable_until_counts_from_the_end_of_the_period() -> None:
    """48 часов от конца дня, а не от момента создания записи."""
    assert periods.editable_until("day", dt.date(2026, 9, 19), MSK, MIDNIGHT) == (
        dt.datetime(2026, 9, 21, 20, 59, 59, tzinfo=dt.UTC)
    )
    # У недели отсчёт идёт от воскресенья, а не от понедельника.
    assert periods.editable_until("week", dt.date(2026, 9, 21), MSK, MIDNIGHT) == (
        dt.datetime(2026, 9, 29, 20, 59, 59, tzinfo=dt.UTC)
    )


def test_editable_until_respects_day_cutoff() -> None:
    """Граница дня сдвигает и срок правки: день кончается позже."""
    with_cutoff = periods.editable_until("day", dt.date(2026, 9, 19), MSK, dt.time(3, 0))
    without = periods.editable_until("day", dt.date(2026, 9, 19), MSK, MIDNIGHT)
    assert with_cutoff - without == dt.timedelta(hours=3)


def test_unknown_level_is_refused() -> None:
    with pytest.raises(AppError):
        periods.check_level("quarter")
    with pytest.raises(AppError):
        periods.bounds("quarter", dt.date(2026, 9, 1))


def test_future_period_detected() -> None:
    today = dt.date(2026, 9, 22)
    assert periods.is_future("day", dt.date(2026, 9, 23), today) is True
    assert periods.is_future("day", today, today) is False
    # Текущая неделя началась в прошлом, значит будущей не считается.
    assert periods.is_future("week", today, today) is False
