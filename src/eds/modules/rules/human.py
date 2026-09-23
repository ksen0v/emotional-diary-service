"""Правило словами. Чистые функции: на вход JSON правила, на выход русская фраза.

Фраза считается здесь, а не на фронте, по одной причине: та же строка уйдёт
в Telegram и ляжет в запись инцидента как текст правила на момент срабатывания.
Собранная в трёх местах, она однажды разойдётся, и «почему меня заблокировало»
перестанет объясняться само.

Формулировки взяты из прототипа RuleBuilder.dc.html дословно. Одно отличие:
в прототипе доверенное лицо названо по имени («Максиму уйдёт сигнал»), у нас
пока «доверенному лицу» — контактов ещё нет (шаг 13), а склонять произвольное
имя в дательный падеж мы не будем.
"""

from decimal import Decimal
from typing import Any

from eds.modules.rules.dictionary import BY_KEY, CMP_WORD, CONN_WORD

BUDDY = "доверенному лицу"
BUDDY_NOM = "доверенное лицо"


def plural(n: int, one: str, few: str, many: str) -> str:
    rest = abs(n) % 100
    if 10 < rest < 20:
        return many
    last = rest % 10
    if last == 1:
        return one
    if 1 < last < 5:
        return few
    return many


def number(value: Any) -> str:
    """Число так, как его ввёл трейдер: 2, 0.5, 5 — без хвостовых нулей."""
    try:
        dec = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return str(value)
    if dec == dec.to_integral_value():
        return str(dec.quantize(Decimal("1")))
    return str(dec.normalize())


def join_ru(parts: list[str]) -> str:
    """«а», «а и б», «а, б и в» — как в прототипе."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " и " + parts[-1]


def conditions_phrase(items: list[dict[str, Any]]) -> str:
    """«убыточных сделок подряд не меньше 2 шт и просадка от пика дня больше 5 % депозита».

    Связка берётся у условия, которому она принадлежит, то есть у второго и
    дальше. Первое условие связки не имеет — это проверяется валидацией.
    """
    out = ""
    for i, item in enumerate(items):
        metric = BY_KEY.get(str(item.get("metric")))
        if metric is None:
            continue
        piece = (
            f"{metric.phrase} {CMP_WORD.get(str(item.get('cmp')), '')} "
            f"{number(item.get('value'))} {metric.unit_long}"
        )
        if i == 0:
            out = piece
        else:
            out += f" {CONN_WORD.get(str(item.get('conn')), 'и')} " + piece
    return out


def lock_phrase(lock: dict[str, Any]) -> str:
    minutes = lock.get("minutes")
    if minutes is None:
        # Блокировка до конца дня — единственная форма для SR-1: окно по тегу
        # принадлежит торговому дню сделки и держится до его границы (ТЗ 4.4).
        return "торговля заблокируется до конца торгового дня"
    mins = int(minutes)
    return (
        f"торговля заблокируется на {mins} "
        f"{plural(mins, 'минуту', 'минуты', 'минут')}"
    )


def actions_phrase(actions: dict[str, Any]) -> list[str]:
    lock = actions.get("lock") or {}
    parts: list[str] = []
    if actions.get("alert"):
        parts.append("придёт алерт в Telegram")
    if lock.get("enabled"):
        parts.append(lock_phrase(lock))
    if actions.get("buddy"):
        parts.append(f"{BUDDY} уйдёт сигнал")
    return parts


def unlock_phrase(actions: dict[str, Any], unlock: dict[str, Any]) -> str:
    """Хвост фразы про снятие. Пустой, если блокировки нет: снимать нечего."""
    lock = actions.get("lock") or {}
    if not lock.get("enabled"):
        return ""

    conditions: list[str] = []
    if unlock.get("timer"):
        conditions.append("истечёт таймер")
    if unlock.get("review"):
        conditions.append("будет заполнен разбор")
    if unlock.get("buddy"):
        conditions.append(f"{BUDDY_NOM} подтвердит снятие")

    if not conditions:
        # ТЗ 6.6: это осмысленный вариант «сегодня я больше не торгую»,
        # поэтому предупреждаем, но не запрещаем.
        return " Снять её вручную будет нельзя — она сама закончится на границе дня."
    return " Снять можно, когда " + join_ru(conditions) + "."


def sentence(if_text: str, actions: dict[str, Any], unlock: dict[str, Any]) -> str:
    """Правило целиком одной фразой.

    `if_text` — то, что стоит после «Если»: у пользовательского правила это
    условия со связками, у системного — фиксированное описание из code.py,
    потому что его условие тремя показателями не выражается (Архитектура ч.1 §7).
    """
    head = f"Если {if_text} — "
    parts = actions_phrase(actions)
    if not parts:
        return head + "ничего не произойдёт: у правила не выбрано ни одного действия."
    return head + join_ru(parts) + "." + unlock_phrase(actions, unlock)


def summary(actions: dict[str, Any]) -> str:
    """Короткая строка для карточки в списке правил, как в прототипе."""
    lock = actions.get("lock") or {}
    if lock.get("enabled"):
        minutes = lock.get("minutes")
        if minutes is None:
            return "Блокировка до конца дня"
        return f"Блокировка {int(minutes)} мин"
    if actions.get("buddy"):
        return "Алерт и сигнал другу"
    return "Только алерт"
