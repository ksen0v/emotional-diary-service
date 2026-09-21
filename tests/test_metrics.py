"""Метрики разметки как чистые функции: без базы и без HTTP."""

from decimal import Decimal

from eds.modules.trades import metrics


def compute(**kwargs):
    base = {
        "trades_all": 0,
        "trades_significant": 0,
        "clean": 0,
        "violations": 0,
        "unmarked": 0,
        "violations_profitable": 0,
        "violations_profit_sum": Decimal("0"),
    }
    return metrics.compute(**{**base, **kwargs})


def test_coverage_is_share_of_marked() -> None:
    m = compute(trades_all=10, clean=6, violations=1, unmarked=3)
    assert m.coverage_pct == Decimal("70.00")


def test_discipline_ignores_unmarked() -> None:
    """Неразмеченные не улучшают коэффициент — иначе не размечать было бы выгодно."""
    m = compute(trades_all=10, clean=6, violations=2, unmarked=2)
    assert m.discipline_pct == Decimal("75.00")


def test_discipline_is_none_without_marked() -> None:
    m = compute(trades_all=5, unmarked=5)
    assert m.discipline_pct is None
    assert m.coverage_pct == Decimal("0.00")


def test_perfect_discipline_with_partial_coverage() -> None:
    """Случай, ради которого покрытие существует: 100% дисциплины при 20% покрытия."""
    m = compute(trades_all=10, clean=2, unmarked=8)
    assert m.discipline_pct == Decimal("100.00")
    assert m.coverage_pct == Decimal("20.00")


def test_emotion_cost_sums_violations_only() -> None:
    m = compute(
        trades_all=3,
        clean=1,
        violations=2,
        violations_profit_sum=Decimal("-213.60"),
        violations_profitable=1,
    )
    assert m.emotion_cost_usd == Decimal("-213.60")
    assert m.violations_profitable == 1


def test_empty_period_is_zeroes_not_errors() -> None:
    m = compute()
    assert m.coverage_pct == Decimal("0.00")
    assert m.emotion_cost_usd == Decimal("0.00")
    assert m.as_dict()["discipline_pct"] is None


def test_confidence_threshold_is_thirty_days() -> None:
    assert metrics.enough_data(29)["enough_data"] is False
    assert metrics.enough_data(30)["enough_data"] is True
    assert metrics.enough_data(12)["days_required"] == 30
