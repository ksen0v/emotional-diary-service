"""Логика модуля rules: создание, правка и описание правил.

Движка здесь нет — он появится на шаге 9. Поэтому в этом файле нет ни одной
функции, которая смотрит на сделки: правило пока только хранится, проверяется
на осмысленность и описывается словами.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.rules import dictionary as dic
from eds.modules.rules import human, repo, validate
from eds.modules.rules import system as sysrules
from eds.modules.rules.models import RuleRow
from eds.platform.errors import AppError, not_found

# Доверенное лицо появится на шаге 13 вместе с модулем notifications и двойным
# согласием. До тех пор подтверждённого контакта не существует ни у кого, и
# правила с условием buddy создать нельзя — так требует ТЗ 6.8, и это лучше
# заглушки, которая примет настройку и никому ничего не отправит.
HAS_CONFIRMED_CONTACT = False

# Счётчик срабатываний считает движок (шаг 9). До него — честный ноль,
# а не правдоподобное число из прототипа.
FIRED_UNKNOWN = 0


async def ensure_system_rules(s: AsyncSession, user_id: uuid.UUID) -> None:
    """Досоздать SR-1…SR-4, если их ещё нет.

    По требованию, а не при регистрации: так правила появляются и у тех, кто
    зарегистрировался до этого шага, и не нужна миграция данных, которая
    угадывала бы состав пользователей. Вызов идемпотентен ограничением базы.
    """
    for rule in sysrules.SYSTEM_RULES:
        await repo.insert_system_rule(
            s,
            user_id,
            code=rule.code,
            name=rule.name,
            actions=dict(rule.actions),
            unlock=dict(rule.unlock),
        )


def rule_out(row: RuleRow) -> dict[str, Any]:
    """Правило для API (Архитектура ч.2 §3.6)."""
    is_system = row.kind == "system"
    definition = sysrules.BY_CODE.get(row.system_code or "")

    if is_system and definition is not None:
        if_text = definition.if_text(row.actions)
        summary = definition.summary
    else:
        if_text = "за торговый день " + human.conditions_phrase(
            list((row.conditions or {}).get("items", []))
        )
        summary = human.summary(row.actions)

    out: dict[str, Any] = {
        "id": str(row.id),
        "name": row.name,
        "kind": row.kind,
        "system_code": row.system_code,
        "enabled": row.enabled,
        # У системных правил условий в базе нет: они в коде обработчика.
        # null, а не пустой список — пустой список читался бы как «условий нет»,
        # то есть «сработает всегда».
        "conditions": None if is_system else row.conditions,
        "actions": row.actions,
        "unlock": row.unlock,
        "if_text": if_text,
        "human_text": human.sentence(if_text, row.actions, row.unlock),
        "summary": summary,
        "fired_last_30d": FIRED_UNKNOWN,
        "version": row.version,
        "updated_at": row.updated_at,
    }
    if is_system and definition is not None:
        out["editable_fields"] = list(definition.editable)
    return out


async def listing(
    s: AsyncSession, user_id: uuid.UUID, capabilities: dict[str, Any] | None
) -> dict[str, Any]:
    await ensure_system_rules(s, user_id)
    rows = await repo.live(s, user_id)
    _ready, blocked = dic.available(capabilities)
    return {
        "rules": [rule_out(row) for row in rows],
        # Правило могло быть собрано при другом источнике. Молча его прятать
        # нельзя, поэтому фронт получает список метрик, которых сейчас нет,
        # и может пометить такое правило.
        "unavailable_metrics": [m.key for m in blocked],
        "engine": {
            # Прямым полем, а не подразумеваемым нулём: экран обязан сказать,
            # что правила пока не срабатывают, иначе тишина читается как «работает».
            "active": False,
            "note": "Правила сохраняются, но ещё не срабатывают: движок появится на шаге 9.",
        },
    }


async def create(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    name: str,
    conditions: list[dict[str, Any]],
    actions: dict[str, Any],
    unlock: dict[str, Any],
    capabilities: dict[str, Any] | None,
) -> RuleRow:
    checked_name = validate.name(name)
    checked_conditions = validate.conditions(conditions, capabilities)
    checked_actions = validate.actions(
        actions, has_confirmed_contact=HAS_CONFIRMED_CONTACT
    )
    checked_unlock = validate.unlock(
        unlock, has_confirmed_contact=HAS_CONFIRMED_CONTACT
    )
    return await repo.insert_user_rule(
        s,
        user_id,
        name=checked_name,
        conditions=checked_conditions,
        actions=checked_actions,
        unlock=checked_unlock,
    )


async def patch(
    s: AsyncSession,
    user_id: uuid.UUID,
    rule_id: uuid.UUID,
    patch_body: dict[str, Any],
    capabilities: dict[str, Any] | None,
) -> RuleRow:
    row = await repo.by_id(s, user_id, rule_id)
    if row is None:
        raise not_found("Правило не найдено.")

    if row.kind == "system":
        validate.system_patch(row.system_code or "", patch_body)

    if patch_body.get("name") is not None:
        row.name = validate.name(str(patch_body["name"]))

    if patch_body.get("conditions") is not None:
        row.conditions = validate.conditions(
            list(patch_body["conditions"]), capabilities
        )

    if patch_body.get("actions") is not None:
        merged = _merge_actions(row.actions, dict(patch_body["actions"]))
        row.actions = validate.actions(
            merged,
            has_confirmed_contact=HAS_CONFIRMED_CONTACT,
            for_system=row.kind == "system",
        )

    if patch_body.get("unlock") is not None:
        row.unlock = validate.unlock(
            {**row.unlock, **dict(patch_body["unlock"])},
            has_confirmed_contact=HAS_CONFIRMED_CONTACT,
        )

    return await repo.save(s, row, bump_version=True)


def _merge_actions(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Частичное обновление действий, включая вложенный lock.

    Обычный `{**current, **incoming}` затёр бы `lock` целиком, и запрос,
    меняющий только длительность, выключил бы саму блокировку.
    """
    merged = dict(current)
    for key, value in incoming.items():
        if key == "lock" and isinstance(value, dict):
            merged["lock"] = {**(current.get("lock") or {}), **value}
            continue
        merged[key] = value
    return merged


async def toggle(
    s: AsyncSession, user_id: uuid.UUID, rule_id: uuid.UUID, enabled: bool
) -> RuleRow:
    row = await repo.by_id(s, user_id, rule_id)
    if row is None:
        raise not_found("Правило не найдено.")
    if row.kind == "system" and not enabled:
        raise AppError(
            "system_rule_always_on",
            "Системный триггер отключить нельзя.",
            403,
            {"system_code": row.system_code},
        )
    row.enabled = enabled
    # Включение и выключение — не правка условий, поэтому версию не поднимаем:
    # текст правила не изменился, а инциденты ссылаются именно на текст.
    return await repo.save(s, row, bump_version=False)


async def delete(
    s: AsyncSession, user_id: uuid.UUID, rule_id: uuid.UUID
) -> None:
    row = await repo.by_id(s, user_id, rule_id)
    if row is None:
        raise not_found("Правило не найдено.")
    if row.kind == "system":
        raise AppError(
            "system_rule_always_on",
            "Системный триггер удалить нельзя.",
            403,
            {"system_code": row.system_code},
        )
    await repo.mark_deleted(s, row)


def preview(
    *,
    capabilities: dict[str, Any] | None,
    system_code: str | None,
    conditions: list[dict[str, Any]],
    actions: dict[str, Any],
    unlock: dict[str, Any],
) -> dict[str, Any]:
    """Фраза для правила, которое ещё не сохранено.

    Отдельный эндпоинт нужен потому, что фраза обязана считаться на сервере
    (её же отправят в Telegram и запишут в инцидент), а конструктор показывает
    её сразу, ещё до «Сохранить». Тот же приём, что у предпросмотра текстов
    уведомлений в Архитектуре ч.2 §3.10: собирать строку на фронте значило бы
    получить два разных описания одного правила.

    Ответ всегда 200: пока трейдер набирает число, правило невалидно, и отвечать
    на каждый удар по клавише ошибкой — плохой способ помочь. Причина, по которой
    «Сохранить» недоступно, приходит отдельным полем.
    """
    definition = sysrules.BY_CODE.get(system_code or "")
    problem: dict[str, Any] | None = None
    try:
        if definition is None:
            checked_conditions = validate.conditions(conditions, capabilities)
        else:
            checked_conditions = {"items": []}
        checked_actions = validate.actions(
            actions,
            has_confirmed_contact=HAS_CONFIRMED_CONTACT,
            for_system=definition is not None,
        )
        checked_unlock = validate.unlock(
            unlock, has_confirmed_contact=HAS_CONFIRMED_CONTACT
        )
    except AppError as err:
        problem = {"code": err.code, "message": err.message, "details": err.details}
        checked_conditions = {"items": conditions}
        checked_actions = _loose_actions(actions)
        checked_unlock = {key: bool(unlock.get(key)) for key in dic.UNLOCK_KEYS}

    if definition is not None:
        if_text = definition.if_text(checked_actions)
        summary = definition.summary
    else:
        if_text = "за торговый день " + human.conditions_phrase(
            list(checked_conditions.get("items", []))
        )
        summary = human.summary(checked_actions)

    return {
        "if_text": if_text,
        "human_text": human.sentence(if_text, checked_actions, checked_unlock),
        "summary": summary,
        "valid": problem is None,
        "problem": problem,
    }


def _loose_actions(raw: dict[str, Any]) -> dict[str, Any]:
    """Действия как есть, без проверок: только для черновика фразы."""
    lock = raw.get("lock") or {}
    minutes = lock.get("minutes")
    try:
        minutes = None if minutes is None else int(minutes)
    except (TypeError, ValueError):
        minutes = None
    out: dict[str, Any] = {
        "alert": bool(raw.get("alert")),
        "lock": {"enabled": bool(lock.get("enabled")), "minutes": minutes},
        "buddy": bool(raw.get("buddy")),
    }
    if "remind_after_minutes" in raw:
        out["remind_after_minutes"] = raw["remind_after_minutes"]
    return out


def metrics_catalog(
    capabilities: dict[str, Any] | None, significance_pct: Decimal
) -> dict[str, Any]:
    catalog = dic.catalog(capabilities, significance_pct)
    catalog["buddy_available"] = HAS_CONFIRMED_CONTACT
    catalog["buddy_note"] = (
        "Доверенное лицо появится на шаге 13: нужен Telegram-бот и двойное согласие."
    )
    return catalog
