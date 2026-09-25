"""Движок правил: счётчики дня и вычисление условий.

Разделение на два файла не декоративное: `counters` отвечает на вопрос
«что сейчас происходит в дне», `evaluate` — «выполнилось ли правило».
Первое зависит от сделок, второе только от чисел, и второе можно проверять
таблицей, не заводя ни одной сделки.
"""

from eds.modules.rules.engine.counters import (
    POSITION_METRICS,
    Counters,
    apply,
    recompute,
    uses_position_metric,
    with_unrealized,
)
from eds.modules.rules.engine.evaluate import Progress, evaluate, progress

__all__ = [
    "POSITION_METRICS",
    "Counters",
    "Progress",
    "apply",
    "evaluate",
    "progress",
    "recompute",
    "uses_position_metric",
    "with_unrealized",
]
