"""Агрегатор Binance: сборка сделок из исполнений.

Два вида проверок, и оба обязательны.

Табличные ловят случаи, которые я придумал: доливку, частичный выход, переворот
одним филлом, пропущенный филл, hedge. Property-based ловят те, которые я
не придумал, — ради этого они и стоят (Архитектура ч.2 §5.4): `hypothesis`
генерирует последовательности филлов, а проверяются инварианты, которые обязаны
держаться на любой из них.

Пятый инвариант — непересечение сделок по времени — заодно детектор hedge:
если он нарушен на настоящих данных, значит у трейдера включён hedge-режим,
и адаптер обязан предупредить, а не молча посчитать.
"""

import datetime as dt
import random
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from eds.modules.source.adapters.binance.aggregate import (
    Fill,
    IncomeRow,
    aggregate,
    apply_funding,
    return_pct,
)

T0 = dt.datetime(2026, 9, 18, 10, 0, tzinfo=dt.UTC)


def fill(
    n: int,
    side: str,
    qty: str,
    price: str = "100",
    pnl: str = "0",
    commission: str = "0",
    asset: str = "USDT",
    minutes: int | None = None,
    symbol: str = "BTCUSDT",
    position_side: str = "BOTH",
) -> Fill:
    return Fill(
        external_id=n,
        order_id=n * 10,
        symbol=symbol,
        position_side=position_side,
        side=side,
        price=Decimal(price),
        qty=Decimal(qty),
        realized_pnl=Decimal(pnl),
        commission=Decimal(commission),
        commission_asset=asset,
        trade_time=T0 + dt.timedelta(minutes=n if minutes is None else minutes),
    )


# --- табличные: пять краевых случаев из Архитектуры ч.1 §5.5 ---


def test_simple_round_trip() -> None:
    """Вход и выход — одна сделка, результат от биржи, комиссия вычтена."""
    res = aggregate(
        [
            fill(1, "BUY", "1", price="100", commission="0.4"),
            fill(2, "SELL", "1", price="110", pnl="10", commission="0.44"),
        ]
    )
    assert len(res.trades) == 1
    trade = res.trades[0]
    assert trade.side == "long"
    assert trade.entry_price == Decimal("100")
    assert trade.realized_pnl == Decimal("10")
    assert trade.commission_usdt == Decimal("0.84")
    assert trade.profit_usd == Decimal("9.16")
    assert trade.open_time == T0 + dt.timedelta(minutes=1)
    assert trade.close_time == T0 + dt.timedelta(minutes=2)
    assert not res.open_positions


def test_short_round_trip() -> None:
    """Шорт — тот же путь, только знак другой. Отдельной ветки в коде нет."""
    res = aggregate(
        [
            fill(1, "SELL", "2", price="100"),
            fill(2, "BUY", "2", price="95", pnl="10"),
        ]
    )
    assert [t.side for t in res.trades] == ["short"]
    assert res.trades[0].realized_pnl == Decimal("10")


def test_add_on_moves_weighted_entry() -> None:
    """Доливка: сделка остаётся одной, вход становится средневзвешенным."""
    res = aggregate(
        [
            fill(1, "BUY", "1", price="100"),
            fill(2, "BUY", "3", price="120"),
            fill(3, "SELL", "4", price="130", pnl="40"),
        ]
    )
    assert len(res.trades) == 1
    # (100*1 + 120*3) / 4
    assert res.trades[0].entry_price == Decimal("115")
    assert res.trades[0].qty == Decimal("4")


def test_partial_exit_does_not_close_trade() -> None:
    """Частичный выход копит результат; сделка закрывается только на нуле."""
    res = aggregate(
        [
            fill(1, "BUY", "4", price="100"),
            fill(2, "SELL", "1", price="110", pnl="10"),
            fill(3, "SELL", "1", price="105", pnl="5"),
        ]
    )
    assert res.trades == []
    position = res.open_positions[("BTCUSDT", "BOTH")]
    assert position.signed_qty == Decimal("2")
    assert position.realized_pnl == Decimal("15")
    # Частичный выход не двигает цену входа: она про то, по чём вошли.
    assert position.entry_price == Decimal("100")

    closed = aggregate([fill(4, "SELL", "2", price="108", pnl="16")], carried=res.open_positions)
    assert len(closed.trades) == 1
    assert closed.trades[0].realized_pnl == Decimal("31")


def test_reversal_in_one_fill_gives_two_trades() -> None:
    """Переворот: закрываем текущую, открываем следующую на остатке."""
    res = aggregate(
        [
            fill(1, "BUY", "2", price="100"),
            fill(2, "SELL", "5", price="110", pnl="20", commission="1.0"),
            fill(3, "BUY", "3", price="105", pnl="-15", commission="0.6"),
        ]
    )
    assert len(res.trades) == 2
    first, second = res.trades
    assert first.side == "long"
    assert second.side == "short"
    # Комиссия переворотного филла делится по объёму: 2 из 5 закрывают лонг.
    assert first.commission_usdt == Decimal("0.4")
    assert second.commission_usdt == Decimal("1.2")
    # `realizedPnl` биржа считает по закрываемой части — он весь у первой.
    assert first.realized_pnl == Decimal("20")
    assert second.realized_pnl == Decimal("-15")
    assert not res.open_positions


def test_hedge_mode_is_reported_not_swallowed() -> None:
    """Hedge не проверялся, поэтому о нём говорят, а не считают молча."""
    res = aggregate(
        [
            fill(1, "BUY", "1", position_side="LONG"),
            fill(2, "SELL", "1", pnl="5", position_side="LONG"),
        ]
    )
    assert res.hedge_detected is True
    # Считать всё равно считаем — группировка по position_side это умеет.
    assert len(res.trades) == 1


def test_one_way_does_not_look_like_hedge() -> None:
    res = aggregate([fill(1, "BUY", "1"), fill(2, "SELL", "1", pnl="1")])
    assert res.hedge_detected is False


def test_commission_in_bnb_is_not_counted_but_named() -> None:
    """Комиссия в BNB в расчёт не идёт (Архитектура ч.1 §5.5), и об этом говорят."""
    res = aggregate(
        [
            fill(1, "BUY", "1", commission="0.002", asset="BNB"),
            fill(2, "SELL", "1", pnl="10", commission="0.002", asset="BNB"),
        ]
    )
    assert res.trades[0].commission_usdt == Decimal("0")
    assert res.trades[0].profit_usd == Decimal("10")
    assert res.commission_in_other_asset == {"BNB"}


def test_two_symbols_do_not_mix() -> None:
    res = aggregate(
        [
            fill(1, "BUY", "1", symbol="BTCUSDT"),
            fill(2, "BUY", "1", symbol="ETHUSDT"),
            fill(3, "SELL", "1", pnl="5", symbol="BTCUSDT"),
            fill(4, "SELL", "1", pnl="-2", symbol="ETHUSDT"),
        ]
    )
    assert {t.symbol for t in res.trades} == {"BTCUSDT", "ETHUSDT"}
    assert len(res.trades) == 2


def test_carried_position_continues_after_gap() -> None:
    """Пропущенный филл: пересчёт продолжается с последнего закрытого нуля."""
    first = aggregate([fill(1, "BUY", "2", price="100")])
    assert first.trades == []

    second = aggregate(
        [fill(2, "SELL", "2", price="120", pnl="40")], carried=first.open_positions
    )
    assert len(second.trades) == 1
    assert second.trades[0].open_time == T0 + dt.timedelta(minutes=1)
    assert second.trades[0].entry_price == Decimal("100")


def test_external_id_survives_rebuild() -> None:
    """Пересобранная из тех же филлов сделка получает тот же ключ.

    Без этого повторная сборка задваивала бы сделки в ленте, а вся защита
    от дублей стоит на `unique(user_id, source, external_id)`.
    """
    fills = [fill(1, "BUY", "1"), fill(2, "SELL", "1", pnl="3")]
    assert aggregate(fills).trades[0].external_id == aggregate(fills).trades[0].external_id
    assert aggregate(fills).trades[0].external_id == "BTCUSDT:BOTH:1"


# --- фандинг и проценты ---


def test_funding_lands_in_the_trade_that_was_open() -> None:
    res = aggregate(
        [
            fill(1, "BUY", "1", minutes=0),
            fill(2, "SELL", "1", pnl="10", minutes=60),
            fill(3, "BUY", "1", minutes=120),
            fill(4, "SELL", "1", pnl="5", minutes=180),
        ]
    )
    income = [
        IncomeRow(
            symbol="BTCUSDT",
            income_type="FUNDING_FEE",
            income=Decimal("-0.7"),
            asset="USDT",
            happened_at=T0 + dt.timedelta(minutes=30),
        ),
        IncomeRow(
            symbol="BTCUSDT",
            income_type="FUNDING_FEE",
            income=Decimal("-0.3"),
            asset="USDT",
            happened_at=T0 + dt.timedelta(minutes=600),  # вне обеих сделок
        ),
    ]
    first, second = apply_funding(res.trades, income)
    assert first.funding == Decimal("-0.7")
    assert first.profit_usd == Decimal("9.3")
    assert second.funding == Decimal("0")


def test_return_pct_needs_a_balance() -> None:
    assert return_pct(Decimal("10"), Decimal("1000")) == Decimal("1")
    # Ноль значил бы «сделка ничего не изменила», а мы просто не знаем базу.
    assert return_pct(Decimal("10"), None) is None
    assert return_pct(Decimal("10"), Decimal("0")) is None


# --- property-based: инварианты Архитектуры ч.2 §5.4 ---

QTY = st.integers(min_value=1, max_value=5)
PRICE = st.integers(min_value=50, max_value=150)
PNL = st.integers(min_value=-40, max_value=40)


@st.composite
def fill_sequence(draw: st.DrawFn) -> list[Fill]:
    """Случайная последовательность филлов по одному символу.

    Специально без «правильности»: переворот, доливка, частичный выход и
    длинный хвост незакрытой позиции появляются сами. Инварианты обязаны
    держаться на любой такой последовательности, а не только на разумной.
    """
    count = draw(st.integers(min_value=1, max_value=12))
    out: list[Fill] = []
    for n in range(1, count + 1):
        out.append(
            fill(
                n,
                draw(st.sampled_from(["BUY", "SELL"])),
                str(draw(QTY)),
                price=str(draw(PRICE)),
                pnl=str(draw(PNL)),
                commission=str(draw(st.integers(min_value=0, max_value=5))) + ".05",
            )
        )
    return out


@given(fill_sequence())
@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
def test_closed_trade_has_zero_signed_volume(fills: list[Fill]) -> None:
    """Закрытая сделка закрыта полностью: объём входа равен объёму выхода.

    Это и есть «сумма qty со знаком равна нулю» из Архитектуры ч.2 §5.4.
    Считается по долям, а не по филлам целиком: переворотный филл входит
    в две сделки, и его объём делится между ними.
    """
    res = aggregate(fills)
    for trade in res.trades:
        assert trade.qty == trade.exit_qty


@given(fill_sequence())
@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
def test_nothing_is_lost_or_doubled(fills: list[Fill]) -> None:
    """Сумма realizedPnl филлов равна сумме по сделкам и незакрытым позициям."""
    res = aggregate(fills)
    total = sum(f.realized_pnl for f in fills)
    got = sum(t.realized_pnl for t in res.trades) + sum(
        p.realized_pnl for p in res.open_positions.values()
    )
    assert got == total


@given(fill_sequence())
@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
def test_idempotent_on_duplicates(fills: list[Fill]) -> None:
    """Повторная подача тех же филлов ничего не меняет.

    Один филл приходит и потоком, и сверкой, поэтому дубль — норма приёма.
    """
    once = aggregate(fills)
    twice = aggregate(fills + fills)
    assert [t.external_id for t in once.trades] == [t.external_id for t in twice.trades]
    assert [t.profit_usd for t in once.trades] == [t.profit_usd for t in twice.trades]


@given(fill_sequence(), st.integers(min_value=0, max_value=10_000))
@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
def test_order_of_arrival_does_not_matter(fills: list[Fill], seed: int) -> None:
    """Порядок прихода филлов на результат не влияет: внутри они сортируются."""
    shuffled = list(fills)
    random.Random(seed).shuffle(shuffled)
    straight = aggregate(fills)
    mixed = aggregate(shuffled)
    assert [t.external_id for t in straight.trades] == [
        t.external_id for t in mixed.trades
    ]
    assert [t.profit_usd for t in straight.trades] == [
        t.profit_usd for t in mixed.trades
    ]


@given(fill_sequence())
@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
def test_trades_of_one_position_do_not_overlap(fills: list[Fill]) -> None:
    """Сделки по одной позиции идут одна за другой, а не внахлёст.

    В one-way режиме одновременно открытых позиций по символу не бывает.
    Нарушение этого инварианта на настоящих данных означает hedge.
    """
    res = aggregate(fills)
    previous_close = None
    for trade in res.trades:
        if previous_close is not None:
            assert trade.open_time >= previous_close
        previous_close = trade.close_time
