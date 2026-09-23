"""Проверка правила. Целиком на сервере — на фронте её обойдут.

Каждая проверка отсюда есть в таблице Архитектуры ч.2 §3.6. Тексты ошибок
готовые и русские: фронт показывает их как есть и своих формулировок не сочиняет
(Архитектура ч.2 §1.3).
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from eds.modules.rules import dictionary as dic
from eds.modules.rules import system as sysrules
from eds.platform.errors import UNPROCESSABLE, AppError

NAME_MAX = 80


def _bad(code: str, message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(code, message, UNPROCESSABLE, details)


def name(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise _bad("validation_failed", "У правила должно быть название.")
    if len(cleaned) > NAME_MAX:
        raise _bad(
            "validation_failed",
            f"Название правила длиннее {NAME_MAX} символов.",
        )
    return cleaned


def _decimal(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise _bad(
            "validation_failed", "Значение условия должно быть числом."
        ) from exc


def conditions(items: list[dict[str, Any]], capabilities: dict[str, Any] | None) -> dict:
    """Плоский список условий со связками. Вложенных групп в MVP нет (ТЗ 6.3)."""
    if not items:
        raise _bad(
            "conditions_empty",
            "У правила нет условий. Правило без условий не сработает никогда.",
        )
    if len(items) > dic.MAX_CONDITIONS:
        raise _bad(
            "conditions_too_many",
            f"Условий в правиле не больше {dic.MAX_CONDITIONS}.",
            {"max": dic.MAX_CONDITIONS, "given": len(items)},
        )

    ready, _blocked = dic.available(capabilities)
    allowed = {m.key for m in ready}

    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        key = str(item.get("metric", ""))
        metric = dic.BY_KEY.get(key)
        if metric is None:
            raise _bad(
                "validation_failed",
                f"Показатель «{key}» не из словаря правил.",
                {"metric": key},
            )
        if key not in allowed:
            raise _bad(
                "metric_unavailable",
                f"Показатель «{metric.name}» недоступен: "
                + dic.reason_for(metric, capabilities),
                {"metric": key, "requires": metric.requires},
            )

        cmp_key = str(item.get("cmp", ""))
        if cmp_key not in dic.CMP_WORD:
            raise _bad(
                "validation_failed",
                f"Сравнение «{cmp_key}» неизвестно.",
                {"cmp": cmp_key},
            )

        value = _decimal(item.get("value"))
        if metric.type == dic.INT and value != value.to_integral_value():
            raise _bad(
                "value_out_of_range",
                f"«{metric.name}» считается в штуках — нужно целое число.",
                {"metric": key, "value": str(value)},
            )
        if not (metric.min <= value <= metric.max):
            raise _bad(
                "value_out_of_range",
                f"«{metric.name}»: значение от {metric.min} до {metric.max} {metric.unit}.",
                {"metric": key, "min": str(metric.min), "max": str(metric.max)},
            )

        # Связка принадлежит условию, которое к чему-то присоединяется.
        # У первого её быть не должно — иначе непонятно, к чему оно клеится.
        conn = item.get("conn")
        if i == 0:
            if conn is not None:
                raise _bad(
                    "validation_failed",
                    "У первого условия не бывает связки «и» / «или».",
                )
            out.append({"metric": key, "cmp": cmp_key, "value": _plain(value)})
            continue
        if conn is None:
            raise _bad(
                "validation_failed",
                "Со второго условия нужна связка «и» или «или».",
            )
        if str(conn) not in dic.CONN_WORD:
            raise _bad(
                "validation_failed", f"Связка «{conn}» неизвестна.", {"conn": str(conn)}
            )
        out.append(
            {"metric": key, "cmp": cmp_key, "value": _plain(value), "conn": str(conn)}
        )

    return {"items": out}


def _plain(value: Decimal) -> float | int:
    """В JSONB число кладём числом, а не строкой: по нему будет считать движок."""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def actions(
    raw: dict[str, Any], *, has_confirmed_contact: bool, for_system: bool = False
) -> dict[str, Any]:
    alert = bool(raw.get("alert"))
    buddy = bool(raw.get("buddy"))
    lock_raw = raw.get("lock") or {}
    lock_on = bool(lock_raw.get("enabled"))

    if not (alert or buddy or lock_on):
        raise _bad(
            "no_action",
            "У правила не выбрано ни одного действия. Такое правило ничего не делает.",
        )

    minutes: int | None = None
    if lock_on:
        raw_minutes = lock_raw.get("minutes")
        if raw_minutes is None:
            if not for_system:
                # У пользовательского правила длительность обязательна:
                # в конструкторе есть поле минут и нет переключателя
                # «до конца дня» (прототип RuleBuilder).
                raise _bad(
                    "validation_failed",
                    "Укажи длительность блокировки в минутах.",
                )
        else:
            minutes = _int_in_range(
                raw_minutes,
                dic.TIMER_MIN,
                dic.TIMER_MAX,
                f"Длительность блокировки — от {dic.TIMER_MIN} до {dic.TIMER_MAX} минут.",
            )

    if buddy and not has_confirmed_contact:
        raise _bad(
            "no_confirmed_contact",
            "Доверенное лицо не подтвердило согласие, поэтому сигнал ему отправить нельзя.",
        )

    out: dict[str, Any] = {
        "alert": alert,
        "lock": {"enabled": lock_on, "minutes": minutes},
        "buddy": buddy,
    }
    if "remind_after_minutes" in raw:
        out["remind_after_minutes"] = _int_in_range(
            raw["remind_after_minutes"],
            sysrules.REMIND_MIN,
            sysrules.REMIND_MAX,
            f"Напоминание — от {sysrules.REMIND_MIN} до {sysrules.REMIND_MAX} минут.",
        )
    return out


def _int_in_range(raw: Any, low: int, high: int, message: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise _bad("validation_failed", message) from exc
    if not (low <= value <= high):
        raise _bad("value_out_of_range", message, {"min": low, "max": high})
    return value


def unlock(raw: dict[str, Any], *, has_confirmed_contact: bool) -> dict[str, bool]:
    out = {key: bool(raw.get(key)) for key in dic.UNLOCK_KEYS}
    if out["buddy"] and not has_confirmed_contact:
        raise _bad(
            "no_confirmed_contact",
            "Доверенное лицо не подтвердило согласие, "
            "поэтому снятие по его подтверждению настроить нельзя.",
        )
    return out


def system_patch(code: str, patch: dict[str, Any]) -> None:
    """Системное правило правится только в разрешённых полях (ТЗ 6.5)."""
    rule = sysrules.BY_CODE.get(code)
    allowed = set(rule.editable) if rule else set()
    touched = _touched(patch)
    forbidden = sorted(f for f in touched if f not in allowed)
    if forbidden:
        raise AppError(
            "forbidden",
            "У системного правила меняются только длительность блокировки, "
            "условия снятия и сигнал доверенному лицу.",
            403,
            {"forbidden": forbidden, "editable": sorted(allowed)},
        )


def _touched(patch: dict[str, Any]) -> set[str]:
    """Какие поля запрос пытается изменить, в тех же именах, что в editable_fields."""
    found: set[str] = set()
    for key, value in patch.items():
        if value is None:
            continue
        if key == "actions" and isinstance(value, dict):
            for sub, sub_value in value.items():
                if sub_value is None:
                    continue
                if sub == "lock" and isinstance(sub_value, dict):
                    for leaf in sub_value:
                        found.add(f"actions.lock.{leaf}")
                    continue
                found.add(f"actions.{sub}")
            continue
        found.add(key)
    return found
