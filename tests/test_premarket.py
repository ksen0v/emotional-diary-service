"""Балл чека, пороги допуска и состояния дня — чистые функции.

Главное здесь — обратная шкала. Вопрос «есть ли желание отыграться» устроен
так, что большой ответ означает плохое состояние, и если переворот считать
во фронте, балл будет зависеть от того, откуда пришёл ответ.
"""

import datetime as dt

import pytest

from eds.modules.daybook import questions, service
from eds.modules.daybook.models import TradingDay
from eds.platform.errors import AppError

BEST = {"sleep": 5, "emotion": 5, "yesterday": 5, "revenge": 1, "plan": 5}
WORST = {"sleep": 1, "emotion": 1, "yesterday": 1, "revenge": 5, "plan": 1}
MIDDLE = {"sleep": 3, "emotion": 3, "yesterday": 3, "revenge": 3, "plan": 3}


def test_five_questions_max_25() -> None:
    assert len(questions.QUESTIONS) == 5
    assert questions.MAX_SCORE == 25


def test_inverted_question_is_counted_backwards() -> None:
    assert questions.score_of(BEST) == 25
    assert questions.score_of(WORST) == 5
    assert questions.score_of(MIDDLE) == 15
    # «Нет желания отыграться» даёт максимум, «сильное желание» — минимум.
    assert questions.BY_ID["revenge"].points(1) == 5
    assert questions.BY_ID["revenge"].points(5) == 1


def test_verdicts_follow_settings_not_constants() -> None:
    assert service.verdict_of(25, 19, 13) == service.GREEN
    assert service.verdict_of(19, 19, 13) == service.GREEN
    assert service.verdict_of(18, 19, 13) == service.RED
    assert service.verdict_of(13, 19, 13) == service.RED
    assert service.verdict_of(12, 19, 13) == service.DENIED
    # Пороги — настройка трейдера: с другими порогами тот же балл даёт другое.
    assert service.verdict_of(18, 15, 10) == service.GREEN


def test_weak_answers_name_what_dragged_the_score() -> None:
    weak = questions.weak_answers({**BEST, "sleep": 2, "revenge": 4})
    names = {item["short"]: item["points"] for item in weak}
    assert names == {"Сон": 2, "Желание отыграться": 2}


def test_incomplete_check_is_refused() -> None:
    with pytest.raises(AppError) as exc:
        service.validate_answers({"sleep": 5})
    assert exc.value.code == "validation_failed"
    assert exc.value.details["missing"] == ["emotion", "plan", "revenge", "yesterday"]


def test_answer_outside_scale_is_refused() -> None:
    with pytest.raises(AppError) as exc:
        service.validate_answers({**BEST, "sleep": 7})
    assert exc.value.code == "validation_failed"
    assert exc.value.http_status == 422


def test_unknown_question_is_refused() -> None:
    with pytest.raises(AppError) as exc:
        service.validate_answers({**BEST, "mood": 3})
    assert exc.value.details["unknown"] == ["mood"]


def day(**over) -> TradingDay:
    row = TradingDay(
        user_id=None,
        day=dt.date(2026, 9, 21),
        admission=None,
        check_score=None,
        session_opened_at=None,
        session_closed_at=None,
        review_state="none",
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


def test_state_machine_covers_the_day() -> None:
    assert service.state_of(None, has_source=False) == service.STATE_NO_SOURCE
    assert service.state_of(None, has_source=True) == service.STATE_NO_CHECK
    assert service.state_of(day(), has_source=True) == service.STATE_NO_CHECK
    assert (
        service.state_of(day(admission="denied"), has_source=True)
        == service.STATE_CHECK_FAILED
    )
    opened = day(admission="green", session_opened_at=dt.datetime.now(dt.UTC))
    assert service.state_of(opened, has_source=True) == service.STATE_TRADING
    closed = day(
        admission="red",
        session_opened_at=dt.datetime.now(dt.UTC),
        session_closed_at=dt.datetime.now(dt.UTC),
    )
    assert service.state_of(closed, has_source=True) == service.STATE_SESSION_CLOSED


def test_no_source_wins_over_everything() -> None:
    """Без источника сервису нечего считать, и это главный факт экрана."""
    opened = day(admission="green", session_opened_at=dt.datetime.now(dt.UTC))
    assert service.state_of(opened, has_source=False) == service.STATE_NO_SOURCE


def test_restrictions_only_for_red_and_only_as_text() -> None:
    assert service._restrictions(service.GREEN) is None
    assert service._restrictions(service.DENIED) is None
    red = service._restrictions(service.RED)
    assert red is not None
    # Обещать урезание размера нельзя: сервис ничего не отправляет на биржу.
    assert "не урезает" in red["note"]
