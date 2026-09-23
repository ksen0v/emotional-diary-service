"""Движок правил без базы: счётчики дня и вычисление условий.

Четыре решения из Архитектуры ч.1 §7 стоят отдельными тестами с говорящими
именами — так, как требует ч.2 §5.5. Каждое из них однажды придётся объяснять
спустя месяцы, и имя теста должно объяснять его за нас.
"""

import datetime as dt
import uuid
from decimal import Decimal

from eds.contracts.rules import TradeFact
from eds.modules.rules.engine import Counters, apply, evaluate, progress, recompute

SIGNIFICANCE = Decimal("0.50")
T0 = dt.datetime(2026, 9, 23, 10, 0, tzinfo=dt.UTC)


def trade(
    minutes: int, pct: str, profit: str | None = None, significant: bool | None = None
) -> TradeFact:
    percent = Decimal(pct)
    return TradeFact(
        trade_id=uuid.uuid4(),
        open_time=T0 + dt.timedelta(minutes=minutes - 5),
        close_time=T0 + dt.timedelta(minutes=minutes),
        account_return_pct=percent,
        profit_usd=Decimal(profit) if profit is not None else percent * 100,
        is_significant=(
            abs(percent) >= SIGNIFICANCE if significant is None else significant
        ),
    )


def fold(*trades: TradeFact) -> Counters:
    counter = Counters()
    for item in trades:
        counter = apply(counter, item, SIGNIFICANCE)
    return counter


def cond(metric: str, cmp: str, value, conn: str | None = None) -> dict:
    out = {"metric": metric, "cmp": cmp, "value": value}
    if conn:
        out["conn"] = conn
    return out


def rule(*items: dict) -> dict:
    return {"items": list(items)}


# --- счётчики дня ---


def test_dust_does_not_break_streak() -> None:
    """Прибыльная сделка мельче порога значимости не обрывает серию убытков.

    Это лазейка, ради которой порог и придуман: три цента прибыли между двумя
    стопами иначе спасали бы от блокировки.
    """
    counters = fold(trade(1, "-0.60"), trade(2, "0.03"), trade(3, "-0.70"))
    assert counters.loss_streak == 2
    assert counters.significant_trades == 2
    assert counters.all_trades == 3


def test_dust_loss_does_not_extend_streak() -> None:
    """Мелкий убыток серию тоже не удлиняет: пыль прозрачна в обе стороны."""
    counters = fold(trade(1, "-0.60"), trade(2, "-0.04"))
    assert counters.loss_streak == 1


def test_significant_profit_breaks_streak() -> None:
    """Значимая прибыльная обрывает серию, и «убыток суммарно» пересчитывается."""
    counters = fold(trade(1, "-0.60"), trade(2, "-0.70"), trade(3, "1.80"))
    assert counters.loss_streak == 0
    assert counters.equity_pct == Decimal("0.50")
    assert counters.loss_sum_pct == Decimal("0")


def test_peak_starts_at_zero() -> None:
    """День, сразу пошедший в минус, даёт просадку от нуля, а не от первой сделки.

    Иначе первый же убыток сам становился бы пиком и обнулял просадку.
    """
    counters = fold(trade(1, "-1.00"), trade(2, "-0.50"))
    assert counters.peak_pct == Decimal("0")
    assert counters.drawdown_pct == Decimal("1.50")


def test_drawdown_counts_from_intraday_peak() -> None:
    counters = fold(trade(1, "2.10"), trade(2, "-0.80"), trade(3, "-2.10"))
    assert counters.peak_pct == Decimal("2.10")
    assert counters.equity_pct == Decimal("-0.80")
    assert counters.drawdown_pct == Decimal("2.90")


def test_loss_sum_is_the_day_not_the_streak() -> None:
    """«Убыток суммарно» — сумма результата за день, если она отрицательная.

    Решение Влада от 23.09 по расхождению ТЗ 6.3 и Архитектуры ч.1 §7:
    считаем по ТЗ. Прибыльный день даёт ноль, а не отрицательный убыток.
    """
    counters = fold(trade(1, "-0.60"), trade(2, "0.90"), trade(3, "-0.70"))
    assert counters.equity_pct == Decimal("-0.40")
    assert counters.loss_sum_pct == Decimal("0.40")

    profitable = fold(trade(1, "-0.60"), trade(2, "1.40"))
    assert profitable.loss_sum_pct == Decimal("0")


def test_batch_sorted_by_close_time() -> None:
    """Неупорядоченная порция из сверки даёт тот же результат, что упорядоченная."""
    ordered = [trade(1, "-0.60"), trade(2, "0.90"), trade(3, "-0.70")]
    shuffled = [ordered[2], ordered[0], ordered[1]]
    assert recompute(shuffled, SIGNIFICANCE) == recompute(ordered, SIGNIFICANCE)


def test_significance_is_recomputed_not_taken_from_the_trade() -> None:
    """Порог меняется в настройках, и пересбор дня обязан считать по новому.

    В сделке значимость зафиксирована на момент приёма; если верить ей, то
    после смены порога день остался бы посчитанным по старому.
    """
    dusty = trade(1, "-0.20", significant=True)
    assert recompute([dusty], Decimal("0.50")).loss_streak == 0
    assert recompute([dusty], Decimal("0.10")).loss_streak == 1


def test_recompute_is_the_same_as_walking() -> None:
    trades = [trade(1, "-0.60"), trade(2, "-0.70"), trade(3, "0.10")]
    assert recompute(trades, SIGNIFICANCE) == fold(*trades)


# --- вычисление условий ---


def test_single_condition() -> None:
    counters = fold(trade(1, "-0.60"), trade(2, "-0.70"))
    assert evaluate(rule(cond("loss_streak", "ge", 2)), counters) is True
    assert evaluate(rule(cond("loss_streak", "ge", 3)), counters) is False


def test_and_needs_both() -> None:
    counters = fold(trade(1, "-0.60"), trade(2, "-0.70"))
    both = rule(
        cond("loss_streak", "ge", 2), cond("drawdown_pct", "ge", "5", "and")
    )
    assert evaluate(both, counters) is False


def test_or_needs_one() -> None:
    counters = fold(trade(1, "-0.60"), trade(2, "-0.70"))
    either = rule(cond("loss_streak", "ge", 2), cond("drawdown_pct", "ge", "5", "or"))
    assert evaluate(either, counters) is True


def test_and_binds_tighter_than_or() -> None:
    """«A и B или C» читается как «(A и B) или C» — как в речи и в любом языке."""
    counters = fold(trade(1, "-0.60"), trade(2, "-0.70"))
    # A: серия ≥ 2 — да. B: просадка ≥ 5 — нет. C: просадка ≥ 1 — да (1.30).
    items = rule(
        cond("loss_streak", "ge", 2),
        cond("drawdown_pct", "ge", "5", "and"),
        cond("drawdown_pct", "ge", "1", "or"),
    )
    assert evaluate(items, counters) is True

    # Та же формула, но C недостижимо: (да и нет) или нет → нет.
    items = rule(
        cond("loss_streak", "ge", 2),
        cond("drawdown_pct", "ge", "5", "and"),
        cond("drawdown_pct", "ge", "9", "or"),
    )
    assert evaluate(items, counters) is False


def test_empty_conditions_never_fire() -> None:
    """Правило без условий не срабатывает всегда — оно не срабатывает вовсе."""
    assert evaluate({"items": []}, Counters()) is False
    assert evaluate(None, Counters()) is False


def test_unavailable_metric_does_not_fire() -> None:
    """Показателя при этом источнике нет — правило молчит, а не срабатывает."""
    counters = fold(trade(1, "-3.00"))
    assert evaluate(rule(cond("unrealized_pct", "ge", "1")), counters) is False


# --- близость к срабатыванию ---


def test_progress_shows_the_condition_that_holds_the_rule() -> None:
    """Внутри «и» показывается самое отстающее условие: оно и держит правило."""
    counters = fold(trade(1, "-0.60"), trade(2, "-0.70"))  # серия 2, просадка 1.30
    point = progress(
        rule(cond("loss_streak", "ge", 2), cond("drawdown_pct", "ge", "5", "and")),
        counters,
    )
    assert point is not None
    assert point.metric == "drawdown_pct"
    assert point.met is False
    assert point.ratio == Decimal("1.30") / Decimal("5")


def test_progress_between_or_groups_takes_the_closest() -> None:
    counters = fold(trade(1, "-0.60"), trade(2, "-0.70"))
    point = progress(
        rule(cond("drawdown_pct", "ge", "10"), cond("loss_streak", "ge", 4, "or")),
        counters,
    )
    assert point is not None
    # Серия 2 из 4 — это 0.5; просадка 1.30 из 10 — 0.13. Ближе серия.
    assert point.metric == "loss_streak"
    assert point.ratio == Decimal("0.5")


def test_progress_is_full_when_rule_is_met() -> None:
    counters = fold(trade(1, "-0.60"), trade(2, "-0.70"))
    point = progress(rule(cond("loss_streak", "ge", 2)), counters)
    assert point is not None
    assert point.met is True
    assert point.ratio == Decimal("1")


def test_progress_is_none_without_available_metrics() -> None:
    point = progress(rule(cond("unrealized_pct", "ge", "1")), fold(trade(1, "-1.00")))
    assert point is None
