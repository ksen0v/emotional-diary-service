"""Движок правил: счётчики дня и вычисление условий.

Разделение на два файла не декоративное: `counters` отвечает на вопрос
«что сейчас происходит в дне», `evaluate` — «выполнилось ли правило».
Первое зависит от сделок, второе только от чисел, и второе можно проверять
таблицей, не заводя ни одной сделки.
"""

from eds.modules.rules.engine.counters import Counters, apply, recompute
from eds.modules.rules.engine.evaluate import Progress, evaluate, progress

__all__ = ["Counters", "Progress", "apply", "evaluate", "progress", "recompute"]
