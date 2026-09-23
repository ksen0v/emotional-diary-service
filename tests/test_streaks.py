"""Зачёт дня и серия дисциплины — чистые правила.

Пять условий ТЗ 7.1 плюс дни вне рынка плюс заморозки дают таблицу случаев,
которую в голове не удержать. Поэтому она разложена здесь: у каждого сочетания
ровно один ответ, и ни одно из условий нельзя тихо потерять.
"""

import datetime as dt

from eds.contracts.streaks import DayFacts, DayMark
from eds.modules.streaks import rules

DAY = dt.date(2026, 9, 20)


def facts(**over) -> DayFacts:
    base = {
        "day": DAY,
        "trades": 0,
        "violations": 0,
        "lock_breaches": 0,
        "admission": None,
        "session_opened": False,
        "review_done": False,
        "entry_filled": False,
        "frozen": False,
    }
    base.update(over)
    return DayFacts(**base)


def good_trading_day(**over) -> DayFacts:
    base = {
        "trades": 14,
        "admission": "green",
        "session_opened": True,
        "review_done": True,
        "entry_filled": True,
    }
    base.update(over)
    return facts(**base)


def test_full_day_counts() -> None:
    mark = rules.mark_of(good_trading_day())
    assert mark == DayMark(DAY, counted=True, reason="ok")


def test_every_condition_can_break_the_day() -> None:
    """Каждое из условий ТЗ 7.1 — достаточная причина не зачесть день."""
    assert rules.mark_of(good_trading_day(violations=1)).reason == "violation"
    assert rules.mark_of(good_trading_day(lock_breaches=1)).reason == "breach"
    assert rules.mark_of(good_trading_day(review_done=False)).reason == "no_review"
    assert rules.mark_of(good_trading_day(entry_filled=False)).reason == "no_entry"
    assert rules.mark_of(good_trading_day(admission=None)).reason == "no_check"
    assert rules.mark_of(good_trading_day(admission="denied")).reason == "no_check"
    for over in (
        {"violations": 1},
        {"lock_breaches": 1},
        {"review_done": False},
        {"entry_filled": False},
        {"admission": None},
    ):
        assert rules.mark_of(good_trading_day(**over)).counted is False


def test_red_admission_still_counts() -> None:
    """Красный допуск — не нарушение: сессия открыта, правила те же."""
    assert rules.mark_of(good_trading_day(admission="red")).counted is True


def test_trading_without_admission_outweighs_a_violation() -> None:
    """Торговля без допуска стоит выше: она случилась вместо сессии, а не в ней."""
    mark = rules.mark_of(good_trading_day(admission="denied", violations=2))
    assert mark.reason == "no_check"


def test_day_out_of_market_needs_only_the_short_form() -> None:
    assert rules.mark_of(facts(entry_filled=True)) == DayMark(DAY, True, "ok")


def test_weekend_without_a_record_is_neutral() -> None:
    """Выходные и отпуск стрик не рвут (ТЗ 7.2): дня просто нет в расчёте."""
    assert rules.mark_of(facts()) is None


def test_no_trades_but_session_opened_needs_the_record() -> None:
    """Сессия была, сделок нет — разбирать нечего, а записать день надо."""
    sat_out = facts(admission="green", session_opened=True)
    assert rules.mark_of(sat_out).reason == "no_entry"
    assert rules.mark_of(facts(admission="green", session_opened=True, entry_filled=True)).counted


def test_denied_day_without_trades_counts_with_a_record() -> None:
    """Допуска не дали, и трейдер не торговал — это соблюдение, а не срыв."""
    assert rules.mark_of(facts(admission="denied", entry_filled=True)).counted is True


def test_frozen_day_beats_everything() -> None:
    assert rules.mark_of(good_trading_day(violations=3, frozen=True)).reason == "frozen"


def marks(*pairs: tuple[int, bool | str]) -> list[DayMark]:
    out = []
    for offset, value in pairs:
        day = DAY + dt.timedelta(days=offset)
        if value == "frozen":
            out.append(DayMark(day, counted=False, reason="frozen"))
        else:
            out.append(
                DayMark(day, counted=bool(value), reason="ok" if value else "violation")
            )
    return out


def test_current_streak_counts_back_to_the_first_miss() -> None:
    assert rules.current_streak(marks((0, True), (1, True), (2, True))) == 3
    assert rules.current_streak(marks((0, True), (1, False), (2, True))) == 1
    assert rules.current_streak(marks((0, True), (1, True), (2, False))) == 0
    assert rules.current_streak([]) == 0


def test_freeze_neither_extends_nor_breaks() -> None:
    assert rules.current_streak(marks((0, True), (1, "frozen"), (2, True))) == 2
    assert rules.current_streak(marks((0, False), (1, "frozen"), (2, True))) == 1


def test_best_streak_remembers_the_longest_run() -> None:
    history = marks(
        (0, True), (1, True), (2, True), (3, True),  # четыре подряд
        (4, False),
        (5, True), (6, True),  # восстановление
    )
    assert rules.best_streak(history) == 4
    assert rules.current_streak(history) == 2


def test_every_reason_has_a_human_text() -> None:
    """Причина показывается трейдеру, и «no_review» на экране — это брак."""
    for reason in ("ok", "violation", "breach", "no_review", "no_check", "no_entry", "frozen"):
        assert rules.REASON_TEXT[reason]
    assert len(rules.CONDITIONS) == 5
