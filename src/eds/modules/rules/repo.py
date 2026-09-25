"""Доступ к таблицам rules."""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.rules.models import DayCounterRow, EvaluationRow, RuleRow
from eds.modules.rules.system import ORDER


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def live(s: AsyncSession, user_id: uuid.UUID) -> list[RuleRow]:
    """Все неудалённые правила: сначала системные в своём порядке, потом свои.

    Сортировка здесь, а не в SQL с CASE: порядок системных правил задан
    константой в коде, и держать его в двух местах незачем.
    """
    res = await s.execute(
        select(RuleRow).where(
            RuleRow.user_id == user_id, RuleRow.deleted_at.is_(None)
        )
    )
    rows = list(res.scalars())
    rows.sort(
        key=lambda r: (
            0 if r.kind == "system" else 1,
            ORDER.get(r.system_code or "", 99),
            r.created_at,
        )
    )
    return rows


async def by_id(
    s: AsyncSession, user_id: uuid.UUID, rule_id: uuid.UUID
) -> RuleRow | None:
    res = await s.execute(
        select(RuleRow).where(
            RuleRow.id == rule_id,
            RuleRow.user_id == user_id,
            RuleRow.deleted_at.is_(None),
        )
    )
    return res.scalar_one_or_none()


async def by_code(
    s: AsyncSession, user_id: uuid.UUID, code: str
) -> RuleRow | None:
    res = await s.execute(
        select(RuleRow).where(
            RuleRow.user_id == user_id, RuleRow.system_code == code
        )
    )
    return res.scalar_one_or_none()


async def insert_user_rule(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    name: str,
    conditions: dict[str, Any],
    actions: dict[str, Any],
    unlock: dict[str, Any],
) -> RuleRow:
    now = _now()
    row = RuleRow(
        id=uuid.uuid4(),
        user_id=user_id,
        name=name,
        kind="user",
        system_code=None,
        enabled=True,
        conditions=conditions,
        actions=actions,
        unlock=unlock,
        version=1,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )
    s.add(row)
    await s.flush()
    return row


async def insert_system_rule(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    code: str,
    name: str,
    actions: dict[str, Any],
    unlock: dict[str, Any],
) -> None:
    """Создать системное правило, если его ещё нет.

    `on conflict do nothing` по частичному индексу, а не проверка «есть ли»:
    два одновременных запроса к экрану правил иначе создали бы SR-1 дважды.
    Условия пустые — они в коде обработчика (Архитектура ч.1 §7).
    """
    now = _now()
    stmt = (
        pg_insert(RuleRow)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            name=name,
            kind="system",
            system_code=code,
            enabled=True,
            conditions={"items": []},
            actions=actions,
            unlock=unlock,
            version=1,
            deleted_at=None,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[RuleRow.user_id, RuleRow.system_code],
            index_where=RuleRow.system_code.isnot(None),
        )
    )
    await s.execute(stmt)


async def save(s: AsyncSession, row: RuleRow, *, bump_version: bool) -> RuleRow:
    """Записать изменения правила.

    `version` растёт на каждой правке, потому что инцидент хранит текст правила
    на момент срабатывания: без версии история «почему меня заблокировало»
    станет нечитаемой после первой же правки (Архитектура ч.2 §3.6).
    """
    if bump_version:
        row.version += 1
    row.updated_at = _now()
    await s.flush()
    return row


async def mark_deleted(s: AsyncSession, row: RuleRow) -> None:
    row.deleted_at = _now()
    await s.flush()


# --- движок: состояние дня и журнал проверок (шаг 9) ---


async def active_user_rules(s: AsyncSession, user_id: uuid.UUID) -> list[RuleRow]:
    """Правила, которые движок проверяет по счётчикам дня.

    Только пользовательские: у системных условий в базе нет, они живут в коде
    обработчика (`app/system_rules.py` и `app/retro.py`). Выключенные не
    проверяются, удалённые тоже — но из базы не исчезают, на них ссылаются
    инциденты.
    """
    res = await s.execute(
        select(RuleRow).where(
            RuleRow.user_id == user_id,
            RuleRow.deleted_at.is_(None),
            RuleRow.enabled.is_(True),
            RuleRow.kind == "user",
        )
    )
    return sorted(res.scalars(), key=lambda r: r.created_at)


async def counters_of(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> DayCounterRow | None:
    res = await s.execute(
        select(DayCounterRow).where(
            DayCounterRow.user_id == user_id, DayCounterRow.day == day
        )
    )
    return res.scalar_one_or_none()


async def save_counters(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date, values: dict[str, Any]
) -> None:
    """Записать состояние дня целиком.

    Upsert, а не «прочитать и обновить»: строка — производная от сделок дня,
    и переписывать её целиком дешевле и безопаснее, чем сливать поля.
    """
    stmt = (
        pg_insert(DayCounterRow)
        .values(user_id=user_id, day=day, updated_at=_now(), **values)
        .on_conflict_do_update(
            index_elements=[DayCounterRow.user_id, DayCounterRow.day],
            set_={**values, "updated_at": _now()},
        )
    )
    await s.execute(stmt)


async def evaluations_of_day(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> dict[tuple[uuid.UUID, str], EvaluationRow]:
    """Уже сделанные проверки дня, ключом (правило, повод).

    Движок читает их целиком перед проходом: по ним он и пропускает
    обработанное, и узнаёт, выполнялось ли условие на прошлой сделке.
    """
    res = await s.execute(
        select(EvaluationRow).where(
            EvaluationRow.user_id == user_id, EvaluationRow.day == day
        ).order_by(EvaluationRow.id)
    )
    return {(row.rule_id, row.trigger_ref): row for row in res.scalars()}


async def record_evaluation(
    s: AsyncSession,
    user_id: uuid.UUID,
    rule_id: uuid.UUID,
    day: dt.date,
    *,
    trigger_ref: str,
    fired: bool,
    snapshot: dict[str, Any],
) -> bool:
    """Записать проверку. False — такая уже была, значит обрабатывать не надо.

    Идемпотентность стоит в базе, а не в проверке «а не записывали ли мы уже»:
    одно и то же событие приходит и из потока, и из сверки, и два запроса
    могут прийти одновременно.
    """
    stmt = (
        pg_insert(EvaluationRow)
        .values(
            rule_id=rule_id,
            user_id=user_id,
            day=day,
            trigger_ref=trigger_ref,
            fired=fired,
            snapshot=snapshot,
            created_at=_now(),
        )
        .on_conflict_do_nothing(
            index_elements=[EvaluationRow.rule_id, EvaluationRow.trigger_ref]
        )
        .returning(EvaluationRow.id)
    )
    res = await s.execute(stmt)
    return res.first() is not None


async def fired_counts(
    s: AsyncSession, user_id: uuid.UUID, since: dt.date
) -> dict[uuid.UUID, int]:
    """Сколько раз каждое правило сработало начиная с даты.

    Счётчик в карточке правила решает практическую задачу: правило,
    сработавшее сорок раз за месяц, настроено неверно, и это видно из списка
    без всякой аналитики (Дизайн Э-11).
    """
    res = await s.execute(
        select(EvaluationRow.rule_id, func.count())
        .where(
            EvaluationRow.user_id == user_id,
            EvaluationRow.day >= since,
            EvaluationRow.fired.is_(True),
        )
        .group_by(EvaluationRow.rule_id)
    )
    return {row[0]: row[1] for row in res}


async def fired_by_day(
    s: AsyncSession, user_id: uuid.UUID, since: dt.date, until: dt.date
) -> dict[dt.date, int]:
    """Сколько срабатываний в каждом дне диапазона. Для дневника.

    По дням, а не по правилам: в дневнике рядом с записью стоит вопрос
    «сколько раз за этот день сервис меня остановил», и ответ на него не
    зависит от того, какое именно правило сработало.
    """
    res = await s.execute(
        select(EvaluationRow.day, func.count())
        .where(
            EvaluationRow.user_id == user_id,
            EvaluationRow.day >= since,
            EvaluationRow.day <= until,
            EvaluationRow.fired.is_(True),
        )
        .group_by(EvaluationRow.day)
    )
    return {row[0]: row[1] for row in res}
