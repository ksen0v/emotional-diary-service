"""Счётчики торгового дня. Чистые функции: данные на входе, данные на выходе.

Здесь живут четыре решения из Архитектуры ч.1 §7, и каждое покрыто отдельным
тестом с говорящим именем — это то место, от которого зависит каждое
срабатывание правила.

Счётчики — кеш, а не истина. Истина это таблица сделок, и `recompute()`
пересобирает состояние дня проходом по ней в любой момент: после сбоя,
после ретропересчёта и просто для проверки, что кеш не разъехался.
"""

import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from eds.contracts.rules import TradeFact

ZERO = Decimal("0")


@dataclass(frozen=True)
class Counters:
    """Состояние дня. Неизменяемое: `apply` возвращает новое, а не правит старое.

    Так проход по дню нельзя испортить случайной правкой на середине, а в тесте
    видно каждое промежуточное состояние.
    """

    loss_streak: int = 0
    equity_pct: Decimal = ZERO
    peak_pct: Decimal = ZERO
    drawdown_pct: Decimal = ZERO
    loss_sum_pct: Decimal = ZERO
    significant_trades: int = 0
    all_trades: int = 0
    last_trade_id: uuid.UUID | None = None
    # Источник без открытых позиций их не даёт. None, а не ноль: ноль значил бы
    # «открытых позиций нет», а это другое (Архитектура ч.2 §3.5). Появятся
    # вместе с Binance на шаге 14.
    unrealized_pct: Decimal | None = None
    drawdown_full_pct: Decimal | None = None

    def value_of(self, metric: str) -> Decimal | None:
        """Значение показателя из словаря правил. None — показатель недоступен."""
        if metric == "loss_streak":
            return Decimal(self.loss_streak)
        if metric == "drawdown_pct":
            return self.drawdown_pct
        if metric == "loss_sum_pct":
            return self.loss_sum_pct
        if metric == "unrealized_pct":
            return self.unrealized_pct
        if metric == "drawdown_full_pct":
            return self.drawdown_full_pct
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "loss_streak": self.loss_streak,
            "equity_pct": self.equity_pct,
            "peak_pct": self.peak_pct,
            "drawdown_pct": self.drawdown_pct,
            "loss_sum_pct": self.loss_sum_pct,
            "significant_trades": self.significant_trades,
            "all_trades": self.all_trades,
            "unrealized_pct": self.unrealized_pct,
            "drawdown_full_pct": self.drawdown_full_pct,
        }


def apply(counter: Counters, trade: TradeFact, significance_pct: Decimal) -> Counters:
    """Принять одну сделку в счётчики дня.

    Четыре решения, зашитые сюда:

    - **`peak_pct` стартует с нуля**, а не с первой сделки. Просадка считается
      от лучшей точки дня, а если день сразу пошёл в минус — от нуля. Иначе
      первый же убыток сам становился бы пиком и обнулял просадку.
    - **Пыль прозрачна для серий.** Сделка ниже порога значимости серию не
      продолжает и не обрывает: мелкая прибыль в три цента между двумя стопами
      иначе спасала бы от блокировки.
    - **Значимая прибыльная обрывает серию.** Серия — это «подряд», и одна
      настоящая прибыль её кончает.
    - **Порядок — по времени закрытия.** Сортирует вызывающий: при сверке
      порция приходит неупорядоченной, и неотсортированный вход даёт неверную
      серию (Архитектура ч.1 §7).

    «Убыток суммарно» — сумма результата за день, если она отрицательная
    (ТЗ 6.3). Архитектура ч.1 §7 описывала его как сумму текущей серии;
    расхождение закрыто 23.09 в пользу ТЗ.
    """
    equity = counter.equity_pct + trade.account_return_pct
    peak = max(counter.peak_pct, equity)
    loss_streak = counter.loss_streak
    significant = counter.significant_trades

    if _is_significant(trade, significance_pct):
        significant += 1
        loss_streak = loss_streak + 1 if trade.profit_usd < 0 else 0

    return replace(
        counter,
        all_trades=counter.all_trades + 1,
        equity_pct=equity,
        peak_pct=peak,
        drawdown_pct=peak - equity,
        loss_sum_pct=-equity if equity < ZERO else ZERO,
        significant_trades=significant,
        loss_streak=loss_streak,
        last_trade_id=trade.trade_id,
    )


def _is_significant(trade: TradeFact, significance_pct: Decimal) -> bool:
    """Значимость пересчитываем, а не берём из сделки.

    В сделке она зафиксирована на момент приёма, а порог трейдер меняет
    в настройках. Пересбор дня после смены порога обязан дать то, что
    означает новый порог, а не то, что означал старый.
    """
    if significance_pct is None:
        return trade.is_significant
    return abs(trade.account_return_pct) >= abs(significance_pct)


def recompute(trades: list[TradeFact], significance_pct: Decimal) -> Counters:
    """Пересобрать счётчики дня из сделок. Вход сортируется здесь.

    Это страховка после сбоя и основа ретропересчёта (шаг 11): состояние дня
    всегда можно получить заново, не доверяя накопленному.
    """
    counter = Counters()
    for trade in sorted(trades, key=lambda t: (t.close_time, str(t.trade_id))):
        counter = apply(counter, trade, significance_pct)
    return counter
