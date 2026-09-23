"""Вычисление условий правила и то, насколько правило близко к срабатыванию.

Условия — плоский список со связками «и» / «или», вложенных групп в MVP нет
(ТЗ 6.3). «И» связывает сильнее «или», как в обычной речи и в любом языке:
«A и B или C» читается как «(A и B) или C». Поэтому список режется на группы
по «или», внутри группы нужны все условия, а между группами хватает одной.

`progress` считается теми же группами: внутри «и» берётся минимум (правило
не ближе своего самого отстающего условия), между «или» — максимум (достаточно
ближайшей группы). Из этой функции живёт блок «Ближе всего к срабатыванию»
на экране «Сегодня»: он превращает правила из невидимой сетки в приборную
панель — видно, что подходишь к границе, до того как её пересечёшь.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from eds.modules.rules.engine.counters import Counters

ZERO = Decimal("0")
ONE = Decimal("1")


def compare(value: Decimal, cmp_key: str, threshold: Decimal) -> bool:
    if cmp_key == "ge":
        return value >= threshold
    if cmp_key == "gt":
        return value > threshold
    if cmp_key == "eq":
        return value == threshold
    if cmp_key == "le":
        return value <= threshold
    if cmp_key == "lt":
        return value < threshold
    return False


def or_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Разрезать плоский список по связкам «или».

    Связка принадлежит условию, которое к чему-то присоединяется, то есть
    второму и дальше. У первого её нет — это проверяет валидация.
    """
    groups: list[list[dict[str, Any]]] = []
    for i, item in enumerate(items):
        if i == 0 or str(item.get("conn", "and")) == "or":
            groups.append([])
        groups[-1].append(item)
    return groups


def _threshold(item: dict[str, Any]) -> Decimal:
    try:
        return Decimal(str(item.get("value")))
    except (ArithmeticError, ValueError, TypeError):
        return ZERO


def condition_met(item: dict[str, Any], counters: Counters) -> bool:
    value = counters.value_of(str(item.get("metric")))
    if value is None:
        # Показателя при этом источнике нет. Правило, собранное на нём, не
        # срабатывает — молча, но честно: в списке правил фронт помечает
        # такое правило недоступной метрикой (Архитектура ч.2 §3.6).
        return False
    return compare(value, str(item.get("cmp")), _threshold(item))


def evaluate(conditions: dict[str, Any] | None, counters: Counters) -> bool:
    """Выполнились ли условия правила.

    Пустой список условий — False, а не True. Правило без условий не должно
    срабатывать всегда; валидация такое правило и не пропустит, но движок
    обязан быть устойчив к строке, попавшей в базу другим путём.
    """
    items = list((conditions or {}).get("items", []))
    if not items:
        return False
    return any(
        all(condition_met(item, counters) for item in group)
        for group in or_groups(items)
    )


@dataclass(frozen=True)
class Progress:
    """Насколько правило близко к срабатыванию и по какому условию.

    `metric`, `value` и `threshold` — то, из чего собирается строка
    «2.9% из 5%» в прототипе: показывается именно то условие, которое держит
    правило, а не первое из списка.
    """

    ratio: Decimal  # 0…1
    metric: str
    cmp: str
    value: Decimal
    threshold: Decimal
    met: bool


def _condition_progress(item: dict[str, Any], counters: Counters) -> Progress | None:
    metric = str(item.get("metric"))
    value = counters.value_of(metric)
    if value is None:
        return None

    cmp_key = str(item.get("cmp"))
    threshold = _threshold(item)
    met = compare(value, cmp_key, threshold)

    if cmp_key in ("ge", "gt", "eq") and threshold > ZERO:
        ratio = min(ONE, max(ZERO, value / threshold))
    else:
        # «Не больше» и «меньше» выполняются, пока показатель мал, и перестают
        # выполняться, когда он растёт. «Приближение» для них не определено:
        # честнее показать два состояния, чем нарисовать полосу, которая едет
        # не в ту сторону.
        ratio = ONE if met else ZERO

    return Progress(
        ratio=ONE if met else ratio,
        metric=metric,
        cmp=cmp_key,
        value=value,
        threshold=threshold,
        met=met,
    )


def progress(conditions: dict[str, Any] | None, counters: Counters) -> Progress | None:
    """Прогресс правила целиком. None — если ни одно условие сейчас не считается.

    Внутри «и» — минимум: правило не ближе своего самого отстающего условия.
    Между «или» — максимум: достаточно ближайшей группы.
    """
    items = list((conditions or {}).get("items", []))
    if not items:
        return None

    best: Progress | None = None
    for group in or_groups(items):
        parts = [_condition_progress(item, counters) for item in group]
        if not parts or any(part is None for part in parts):
            # В группе есть условие на недоступном показателе — группа
            # выполниться не может, и её прогресс не считается.
            continue
        slowest = min(parts, key=lambda p: p.ratio)  # type: ignore[union-attr]
        if best is None or slowest.ratio > best.ratio:  # type: ignore[union-attr]
            best = slowest  # type: ignore[assignment]
    return best
