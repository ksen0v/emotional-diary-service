"""Торговый день и его границы — общее правило для приёма сделок и сессии.

Проверяется потому, что от этой функции зависят одновременно торговый день
сделки, момент закрытия сессии и таймер «день кончается в». Ошибка здесь
сдвигает всё сразу и выглядит как загадка в трёх разных местах.
"""

import datetime as dt

from eds.contracts.trading_time import day_bounds, day_ends_at, trading_day

MSK = "Europe/Moscow"
CUTOFF = dt.time(3, 0)


def utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


def test_night_trade_belongs_to_previous_day() -> None:
    # 01:30 по Москве 21-го — это ещё торговый день 20-го при границе 03:00.
    assert trading_day(utc(2026, 9, 20, 22, 30), MSK, CUTOFF) == dt.date(2026, 9, 20)


def test_after_cutoff_new_day_starts() -> None:
    assert trading_day(utc(2026, 9, 21, 0, 30), MSK, CUTOFF) == dt.date(2026, 9, 21)


def test_bounds_match_trading_day() -> None:
    """Границы и определение дня должны согласоваться, иначе день «висит»."""
    day = dt.date(2026, 9, 21)
    start, end = day_bounds(day, MSK, CUTOFF)
    assert trading_day(start, MSK, CUTOFF) == day
    assert trading_day(end - dt.timedelta(seconds=1), MSK, CUTOFF) == day
    # Конец исключающий: сам момент границы — уже следующий день.
    assert trading_day(end, MSK, CUTOFF) == day + dt.timedelta(days=1)


def test_midnight_cutoff_is_plain_local_day() -> None:
    day = dt.date(2026, 9, 21)
    start, end = day_bounds(day, MSK, dt.time(0, 0))
    assert start == utc(2026, 9, 20, 21, 0)
    assert end == utc(2026, 9, 21, 21, 0)
    assert day_ends_at(day, MSK, dt.time(0, 0)) == utc(2026, 9, 21, 20, 59, 59)


def test_dst_change_makes_day_shorter_or_longer() -> None:
    """Граница задана местным временем, поэтому сутки перехода не 24 часа."""
    start, end = day_bounds(dt.date(2026, 3, 29), "Europe/Berlin", dt.time(0, 0))
    assert (end - start) == dt.timedelta(hours=23)
