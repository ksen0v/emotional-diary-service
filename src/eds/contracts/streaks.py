"""Факты дня на границе модуля стриков.

Зачёт дня зависит от сделок, допуска, разбора и записи дневника — то есть
от трёх чужих модулей. Модуль streaks не имеет права заглядывать в их схемы,
поэтому факты собирает оркестрация и передаёт сюда. Так правило зачёта
остаётся чистой функцией, которую можно разложить по таблице в тестах.
"""

import datetime as dt
from dataclasses import dataclass

# Причины из схемы Архитектуры ч.1 §6 плюс no_entry: «запись дня не заполнена»
# в исходном перечислении не было, а условие зачёта в ТЗ 7.1 есть.
REASON_OK = "ok"
REASON_VIOLATION = "violation"
REASON_BREACH = "breach"
REASON_NO_REVIEW = "no_review"
REASON_NO_CHECK = "no_check"
REASON_NO_ENTRY = "no_entry"
REASON_FROZEN = "frozen"


@dataclass(frozen=True)
class DayFacts:
    """Всё, что нужно знать о дне, чтобы решить, зачтён ли он."""

    day: dt.date
    trades: int
    violations: int
    lock_breaches: int
    admission: str | None  # green | red | denied | None (чека не было)
    session_opened: bool
    review_done: bool
    entry_filled: bool
    frozen: bool = False


@dataclass(frozen=True)
class DayMark:
    day: dt.date
    counted: bool
    reason: str

    @property
    def neutral(self) -> bool:
        """День, который не удлиняет серию и не рвёт её.

        Заморозка и день вне рынка без записи. Без этого понятия выходные
        обрывали бы стрик, а это прямо запрещено (ТЗ 7.2).
        """
        return self.reason == REASON_FROZEN
