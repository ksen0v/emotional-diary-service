"""Логика модуля rules: создание, правка, описание правил и движок.

Движок не смотрит на сделки напрямую: факты о них приходят контрактом
`TradeFact`, а собирает их оркестрация. Поэтому модуль по-прежнему не знает
ни о схеме trades, ни о настройках трейдера.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.rules import Firing, TradeFact
from eds.modules.rules import dictionary as dic
from eds.modules.rules import engine, human, repo, validate
from eds.modules.rules import system as sysrules
from eds.modules.rules.models import RuleRow
from eds.platform.errors import AppError, not_found

# Доверенное лицо появится вместе с модулем notifications и двойным
# согласием. До тех пор подтверждённого контакта не существует ни у кого, и
# правила с условием buddy создать нельзя — так требует ТЗ 6.8, и это лучше
# заглушки, которая примет настройку и никому ничего не отправит.
HAS_CONFIRMED_CONTACT = False

# Окно счётчика срабатываний в карточке правила — как в прототипе.
FIRED_WINDOW_DAYS = 30


async def ensure_system_rules(s: AsyncSession, user_id: uuid.UUID) -> None:
    """Досоздать SR-1…SR-4, если их ещё нет, и починить те, что уже есть.

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
    await _repair_system_rules(s, user_id)


async def _repair_system_rules(s: AsyncSession, user_id: uuid.UUID) -> None:
    """Привести уже созданные системные правила к тому, что сервис исполняет.

    Нужно потому, что определение триггера живёт в коде и меняется вместе
    с механикой, а запись в базе создаётся один раз. Разойдясь, они дают
    худший вид поломки: экран обещает то, чего обработчик не делает.

    Трогается только то, что трейдер изменить не мог:

    - **нередактируемые поля** — их список объявлен в `editable`, и всё
      остальное принадлежит коду;
    - **сигнал доверенному лицу**, пока подтверждённого контакта нет. Это не
      «тихая отмена настройки»: включить его было нельзя, валидация не
      пропускала, — зато правило с ним лежало в базе в виде, который эта же
      валидация считает недопустимым, и «Сохранить» не загоралось никогда.

    Настроенное трейдером в разрешённых полях остаётся как есть.
    """
    rows = {row.system_code: row for row in await repo.live(s, user_id)}
    for rule in sysrules.SYSTEM_RULES:
        row = rows.get(rule.code)
        if row is None:
            continue

        editable = set(rule.editable)
        actions = _merge(dict(rule.actions), dict(row.actions or {}), editable, "actions")
        unlock = (
            {key: bool((row.unlock or {}).get(key)) for key in dic.UNLOCK_KEYS}
            if "unlock" in editable
            else dict(rule.unlock)
        )
        if not sysrules.BUDDY_ACTIVE:
            actions["buddy"] = False
            unlock["buddy"] = False
        # Условия снятия без блокировки ничего не значат: снимать нечего.
        if not (actions.get("lock") or {}).get("enabled"):
            unlock = dict.fromkeys(dic.UNLOCK_KEYS, False)

        if actions == row.actions and unlock == row.unlock and row.name == rule.name:
            continue
        row.actions = actions
        row.unlock = unlock
        row.name = rule.name
        # Версию не поднимаем: это не правка правила трейдером, а приведение
        # записи к коду. Инциденты хранят текст на момент срабатывания и
        # от этого не меняются.
        await repo.save(s, row, bump_version=False)


def _merge(
    default: dict[str, Any], current: dict[str, Any], editable: set[str], prefix: str
) -> dict[str, Any]:
    """Значения из кода, поверх них — то, что трейдеру разрешено менять."""
    out = dict(default)
    for key, value in default.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            out[key] = _merge(value, current.get(key) or {}, editable, path)
        elif path in editable and key in current:
            out[key] = current[key]
    return out


def rule_out(row: RuleRow, fired_last_30d: int = 0) -> dict[str, Any]:
    """Правило для API (Архитектура ч.2 §3.6)."""
    is_system = row.kind == "system"
    definition = sysrules.BY_CODE.get(row.system_code or "")

    consequence = definition.consequence if definition is not None else ""
    tail = definition.tail if definition is not None else ""
    if is_system and definition is not None:
        if_text = definition.if_text(row.actions)
        summary = definition.summary(row.actions)
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
        "human_text": human.sentence(
            if_text, row.actions, row.unlock, consequence, tail
        ),
        "summary": summary,
        "fired_last_30d": fired_last_30d,
        "version": row.version,
        "updated_at": row.updated_at,
    }
    if is_system and definition is not None:
        out["editable_fields"] = list(definition.editable)
        # Что у этого триггера ещё не работает — полем, а не умолчанием.
        # Ноль в счётчике сам по себе означает и «не было», и «не считаем»,
        # и экран обязан сказать, какое из двух (урок шага 9).
        out["pending"] = sysrules.pending_of(row.system_code or "")
    return out


async def listing(
    s: AsyncSession,
    user_id: uuid.UUID,
    capabilities: dict[str, Any] | None,
    *,
    today: dt.date | None = None,
    shadow_mode: bool = False,
) -> dict[str, Any]:
    await ensure_system_rules(s, user_id)
    rows = await repo.live(s, user_id)
    _ready, blocked = dic.available(capabilities)
    day = today or dt.datetime.now(dt.UTC).date()
    fired = await repo.fired_counts(
        s, user_id, day - dt.timedelta(days=FIRED_WINDOW_DAYS)
    )
    return {
        "rules": [rule_out(row, fired.get(row.id, 0)) for row in rows],
        # Правило могло быть собрано при другом источнике. Молча его прятать
        # нельзя, поэтому фронт получает список метрик, которых сейчас нет,
        # и может пометить такое правило.
        "unavailable_metrics": [m.key for m in blocked],
        "engine": {
            # Три независимых флага, а не один общий. Каждый отвечает за своё
            # «ещё не готово» и гаснет своим шагом: на шаге 9 предупреждение
            # про системные триггеры висело на `active`, движок включился — и
            # унесло предупреждение с собой, хотя триггеры не срабатывали.
            "active": True,
            "system_active": sysrules.SYSTEM_ACTIVE,
            "retro_active": sysrules.RETRO_ACTIVE,
            "shadow_mode": shadow_mode,
            "note": (
                "Движок правил ещё не запущен: правила сохраняются, но не "
                "считаются."
            ),
            # Подпись под заголовком «Системные · отключить нельзя»: короткая,
            # потому что подробности стоят на самих карточках.
            "system_note": (
                "Пока не срабатывают: их условия ещё не подключены."
            ),
            "shadow_note": (
                "Режим наблюдения включён: правило посчитается и инцидент "
                "запишется, но блокировка не применится."
            ),
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

    consequence = definition.consequence if definition is not None else ""
    tail = definition.tail if definition is not None else ""
    if definition is not None:
        if_text = definition.if_text(checked_actions)
        summary = definition.summary(checked_actions)
    else:
        if_text = "за торговый день " + human.conditions_phrase(
            list(checked_conditions.get("items", []))
        )
        summary = human.summary(checked_actions)

    return {
        "if_text": if_text,
        "human_text": human.sentence(
            if_text, checked_actions, checked_unlock, consequence, tail
        ),
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
        "Доверенное лицо появится вместе с Telegram: нужен бот и двойное согласие."
    )
    # Канал алерта. Фраза правила по-прежнему говорит «придёт алерт в Telegram»
    # — она описывает само правило, а не готовность канала, и уйдёт в инцидент
    # как текст на момент срабатывания. А вот экран обязан сказать, куда алерт
    # уходит сегодня: иначе трейдер будет ждать сообщение в Telegram, которого
    # пока нет.
    catalog["alert_note"] = (
        "Telegram ещё не подключён. Пока алерт пишется в журнал сервиса, "
        "а блокировку видно на экране."
    )
    return catalog


# Отличает «не передано» от «передано пусто»: пусто означает «открытых позиций
# нет», не передано — «эта ветка про позиции ничего не знает».
KEEP = object()


# --- движок (шаг 9) ---

# Сколько правил показывать в блоке «Ближе всего к срабатыванию».
# Три, потому что блок в прототипе не прокручивается, а смысл его в том,
# чтобы одним взглядом увидеть ближайшую границу, а не весь список правил.
NEAR_LIMIT = 3

# С какого прогресса строка подсвечивается янтарным. В прототипе янтарная
# верхняя строка на 58% и серая вторая на 50%, поэтому подсвечивается ровно
# одна — ближайшая, и только когда она прошла половину пути.
HOT_RATIO = Decimal("0.5")


def _jsonable(value: Any) -> Any:
    """Снимок счётчиков в JSONB. Decimal и UUID json.dumps не умеет."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def run_day(
    s: AsyncSession,
    user_id: uuid.UUID,
    day: dt.date,
    facts: list[TradeFact],
    significance_pct: Decimal,
    *,
    unrealized_pct: Decimal | None | object = KEEP,
    position_trigger: str | None = None,
) -> tuple[engine.Counters, list[Firing]]:
    """Пройти день сделка за сделкой и собрать срабатывания.

    Проход именно по сделкам, а не по итогу дня: порция из сверки может
    принести три сделки разом, и если правило выполнилось на второй, а третья
    его отменила, блокировка всё равно должна была включиться. Итог дня такой
    случай потерял бы молча.

    **Правило срабатывает на переходе, а не на каждой сделке, пока условие
    выполняется.** Иначе «2 убыточных подряд» дало бы новую блокировку на
    третьей, четвёртой и пятой убыточной сделке — по инциденту на каждую.
    Срабатывание — событие, а не состояние; предыдущее состояние условия
    берётся из журнала проверок, поэтому повторный проход ничего не добавляет.

    **Открытая позиция проверяется отдельным поводом.** Закрытая сделка
    проверяет правила по закрытым числам, обновление позиции — по числам
    с учётом нереализованного. Разделение не формальное: иначе правило про
    просадку с открытой позицией срабатывало бы только в момент, когда что-то
    закрылось, то есть уже после тильта, ради которого оно написано. На этом
    поводе проверяются только правила, которые от позиций и зависят: остальным
    нереализованное ничего не меняет, а журнал проверок рос бы каждую минуту.
    """
    rules = await repo.active_user_rules(s, user_id)
    done = await repo.evaluations_of_day(s, user_id, day)

    counters = engine.Counters()
    was_met: dict[uuid.UUID, bool] = {}
    firings: list[Firing] = []

    for fact in sorted(facts, key=lambda f: (f.close_time, str(f.trade_id))):
        counters = engine.apply(counters, fact, significance_pct)
        trigger_ref = f"trade:{fact.trade_id}"

        for rule in rules:
            # Сделка старше самого правила его не касается: иначе только что
            # собранное правило сработало бы задним числом на утренних стопах.
            if fact.close_time < rule.created_at:
                continue

            met = engine.evaluate(rule.conditions, counters)
            already = done.get((rule.id, trigger_ref))
            if already is not None:
                was_met[rule.id] = bool(already.snapshot.get("met", already.fired))
                continue

            fired = met and not was_met.get(rule.id, False)
            snapshot = _jsonable({**counters.as_dict(), "met": met})
            written = await repo.record_evaluation(
                s,
                user_id,
                rule.id,
                day,
                trigger_ref=trigger_ref,
                fired=fired,
                snapshot=snapshot,
            )
            was_met[rule.id] = met
            if fired and written:
                firings.append(
                    Firing(
                        rule_id=rule.id,
                        rule_name=rule.name,
                        rule_text=rule_out(rule)["human_text"],
                        rule_version=rule.version,
                        day=day,
                        trigger_ref=trigger_ref,
                        trade_id=fact.trade_id,
                        actions=dict(rule.actions),
                        unlock=dict(rule.unlock),
                        snapshot=snapshot,
                    )
                )

    # Нереализованное приходит только от обновления позиций. Обычный пересчёт
    # дня про него ничего не знает и не имеет права затирать: иначе открытие
    # экрана «Сегодня» стирало бы просадку с открытой позицией, посчитанную
    # минуту назад. Поэтому «не передано» и «передано пусто» — разные вещи.
    if unrealized_pct is KEEP:
        stored = await repo.counters_of(s, user_id, day)
        unrealized_pct = stored.unrealized_pct if stored is not None else None
    counters = engine.with_unrealized(counters, unrealized_pct)

    if position_trigger is not None and unrealized_pct is not None:
        for rule in rules:
            if not engine.uses_position_metric(rule.conditions):
                continue
            met = engine.evaluate(rule.conditions, counters)
            already = done.get((rule.id, position_trigger))
            if already is not None:
                continue
            fired = met and not was_met.get(rule.id, False)
            snapshot = _jsonable({**counters.as_dict(), "met": met})
            written = await repo.record_evaluation(
                s,
                user_id,
                rule.id,
                day,
                trigger_ref=position_trigger,
                fired=fired,
                snapshot=snapshot,
            )
            was_met[rule.id] = met
            if fired and written:
                firings.append(
                    Firing(
                        rule_id=rule.id,
                        rule_name=rule.name,
                        rule_text=rule_out(rule)["human_text"],
                        rule_version=rule.version,
                        day=day,
                        trigger_ref=position_trigger,
                        trade_id=None,
                        actions=dict(rule.actions),
                        unlock=dict(rule.unlock),
                        snapshot=snapshot,
                    )
                )

    await repo.save_counters(
        s,
        user_id,
        day,
        {**counters.as_dict(), "last_trade_id": counters.last_trade_id},
    )
    return counters, firings


async def near(
    s: AsyncSession, user_id: uuid.UUID, counters: engine.Counters
) -> list[dict[str, Any]]:
    """Блок «Ближе всего к срабатыванию» (Дизайн Э-04, блок 4).

    Считается на сервере целиком, включая строку «2.9% из 5%»: фронт не знает
    ни приоритета связок, ни того, какое из условий держит правило.
    """
    rules = await repo.active_user_rules(s, user_id)
    rows: list[dict[str, Any]] = []
    for rule in rules:
        point = engine.progress(rule.conditions, counters)
        if point is None:
            continue
        metric = dic.BY_KEY.get(point.metric)
        suffix = "%" if metric and metric.type == dic.DECIMAL else ""
        rows.append(
            {
                "rule_id": str(rule.id),
                "name": rule.name,
                "metric": point.metric,
                "metric_name": metric.name if metric else point.metric,
                "value_text": (
                    f"{human.number(point.value)}{suffix} из "
                    f"{human.number(point.threshold)}{suffix}"
                ),
                "ratio": point.ratio,
                "met": point.met,
            }
        )

    rows.sort(key=lambda r: (r["ratio"], r["name"]), reverse=True)
    rows = rows[:NEAR_LIMIT]
    for i, row in enumerate(rows):
        # Янтарным — только ближайшее правило и только со второй половины пути.
        row["hot"] = bool(i == 0 and row["ratio"] >= HOT_RATIO)
    return rows


async def counters_out(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> engine.Counters:
    """Счётчики дня из кеша. Пустые, если движок по этому дню ещё не ходил."""
    row = await repo.counters_of(s, user_id, day)
    if row is None:
        return engine.Counters()
    return engine.Counters(
        loss_streak=row.loss_streak,
        equity_pct=row.equity_pct,
        peak_pct=row.peak_pct,
        drawdown_pct=row.drawdown_pct,
        loss_sum_pct=row.loss_sum_pct,
        significant_trades=row.significant_trades,
        all_trades=row.all_trades,
        last_trade_id=row.last_trade_id,
        unrealized_pct=row.unrealized_pct,
        drawdown_full_pct=row.drawdown_full_pct,
    )
