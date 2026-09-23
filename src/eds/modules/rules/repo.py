"""Доступ к таблицам rules."""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.rules.models import RuleRow
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
