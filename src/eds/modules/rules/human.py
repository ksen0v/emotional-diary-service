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
        # Длительность не задана — блокировка держится до границы торгового
        # дня (ТЗ 4.4, 6.6). Хвост про снятие говорит отдельно, можно ли
        # снять её раньше: без этого «до конца дня» и «снять можно» в одной
        # фразе противоречат друг другу.
        return "торговля заблокируется до конца торгового дня"
    mins = int(minutes)
    return (
        f"торговля заблокируется на {mins} "
        f"{plural(mins, 'минуту', 'минуты', 'минут')}"
    )


def actions_phrase(actions: dict[str, Any], consequence: str = "") -> list[str]:
    lock = actions.get("lock") or {}
    parts: list[str] = []
    if actions.get("alert"):
        parts.append("придёт алерт в Telegram")
    if lock.get("enabled"):
        parts.append(lock_phrase(lock))
    if actions.get("buddy"):
        parts.append(f"{BUDDY} уйдёт сигнал")
    if consequence:
        # Последствие системного триггера, которого нет в словаре действий:
        # сгоревший стрик. Оно всегда истинно и не настраивается, поэтому
        # стоит в конце перечисления, а не тумблером выше.
        parts.append(consequence)
    return parts


def unlock_phrase(actions: dict[str, Any], unlock: dict[str, Any]) -> str:
    """Хвост фразы про снятие. Пустой, если блокировки нет: снимать нечего.

    Главная тонкость — блокировка без заданной длительности. Она идёт до
    границы торгового дня, и слова «истечёт таймер» для неё означают
    «кончится день», а не «пройдут N минут». Написать их как условие снятия
    значит пообещать досрочный выход, которого не будет: `timer_until`
    у такой блокировки не выставляется вовсе, и условие «таймер» не
    выполнится никогда. Поэтому здесь три разных хвоста, а не один.
    """
    lock = actions.get("lock") or {}
    if not lock.get("enabled"):
        return ""

    till_day_end = lock.get("minutes") is None
    early: list[str] = []
    nouns: list[str] = []
    if unlock.get("review"):
        early.append("будет заполнен разбор")
        nouns.append("разбор")
    if unlock.get("buddy"):
        early.append(f"{BUDDY_NOM} подтвердит снятие")
        nouns.append(f"подтверждение от {BUDDY}")

    if till_day_end and unlock.get("timer"):
        # Таймер у блокировки до конца дня — это и есть граница дня.
        tail = " Снять раньше нельзя: таймер у такой блокировки идёт до границы дня."
        if nouns:
            # Остальные условия от этого не исчезают: их всё равно требуют,
            # просто они не ускоряют выход.
            need = "нужен" if len(nouns) == 1 else "нужны"
            tail += f" {capitalize(join_ru(nouns))} всё равно {need}."
        return tail

    if till_day_end:
        if not early:
            # ТЗ 6.6: это осмысленный вариант «сегодня я больше не торгую»,
            # поэтому предупреждаем, но не запрещаем.
            return " Снять её вручную будет нельзя — она сама закончится на границе дня."
        return " Снять раньше можно, когда " + join_ru(early) + "."

    conditions: list[str] = []
    if unlock.get("timer"):
        conditions.append("истечёт таймер")
    conditions += early
    if not conditions:
        return " Снять её вручную будет нельзя — она сама закончится на границе дня."
    return " Снять можно, когда " + join_ru(conditions) + "."


def capitalize(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def sentence(
    if_text: str,
    actions: dict[str, Any],
    unlock: dict[str, Any],
    consequence: str = "",
) -> str:
    """Правило целиком одной фразой.

    `if_text` — то, что стоит после «Если»: у пользовательского правила это
    условия со связками, у системного — фиксированное описание из system.py,
    потому что его условие тремя показателями не выражается (Архитектура ч.1 §7).
    """
    head = f"Если {if_text} — "
    parts = actions_phrase(actions, consequence)
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
