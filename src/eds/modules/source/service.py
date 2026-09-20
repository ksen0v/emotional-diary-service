"""Подключения источников, словарь тегов и наполнение фейковой ленты."""

import datetime as dt
import hashlib
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts import events as ev
from eds.modules.source import repo
from eds.modules.source.adapters.fake.source import ACCOUNT_EXTERNAL_ID, FakeSource
from eds.modules.source.models import Connection
from eds.platform import bus
from eds.platform.errors import UNPROCESSABLE, AppError


async def connect_fake(s: AsyncSession, user_id: uuid.UUID) -> Connection:
    """Подключить фейковый источник.

    Существует только для разработки и тестов: настоящие подключения появятся
    на шаге 4 (TMM) и 14 (Binance). Держать его в общем коде — сознательный выбор:
    на нём стоят все сценарные тесты, и он должен ломаться вместе с остальным.
    """
    existing = [c for c in await repo.connections_of(s, user_id) if c.provider == "fake"]
    if existing:
        connection = existing[0]
    else:
        probe = FakeSource(s, uuid.uuid4())
        # Настоящий источник считает от момента подключения. Фейковому нужна
        # возможность строить сценарии про прошлые дни — поздний тег на сделке
        # позавчера, серия стопов вчера, — поэтому его точка отсчёта сдвинута
        # на месяц назад. Это единственное, чем он отличается по правилам,
        # и различие лежит здесь, а не растворено в приёме сделок.
        connection = await repo.create_connection(
            s,
            user_id=user_id,
            provider="fake",
            capabilities=probe.capabilities().as_dict(),
            market="futures",
            key_masked="fake",
            ingest_from=dt.datetime.now(dt.UTC) - dt.timedelta(days=30),
        )

    source = FakeSource(s, connection.id)
    await repo.upsert_accounts(s, user_id, connection.id, await source.fetch_accounts())
    await repo.deactivate_all(s, user_id)
    await repo.activate(s, connection)
    return connection


async def activate(s: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID) -> Connection:
    connection = await repo.connection_by_id(s, user_id, connection_id)
    if connection is None:
        raise AppError("not_found", "Подключение не найдено.", 404)
    await repo.deactivate_all(s, user_id)
    return await repo.activate(s, connection)


async def set_violation_tags(
    s: AsyncSession, user_id: uuid.UUID, tag_ids: list[str]
) -> int:
    """Отметить, какие теги считаются нарушением. Пересчёт разметки — шаг 3."""
    connection = await repo.active_connection(s, user_id)
    if connection is None:
        raise AppError("no_active_source", "Источник сделок не подключён.", 409)

    wanted = set(tag_ids)
    changed = 0
    for tag in await repo.tags_of(s, connection.id):
        should_be = tag.external_id in wanted
        if tag.is_violation != should_be:
            tag.is_violation = should_be
            changed += 1
    await s.flush()

    if changed:
        await bus.publish(
            s,
            ev.SOURCE_TAG_DICTIONARY_CHANGED,
            {"user_id": str(user_id), "connection_id": str(connection.id)},
        )
    return changed


async def push_fake_trade(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    symbol: str,
    side: str,
    profit_usd: Decimal,
    account_return_pct: Decimal,
    tags: list[str],
    minutes_ago: int,
    duration_sec: int,
) -> dict:
    """Добавить сделку в ленту фейкового источника.

    Это dev-инструмент: он не пишет в trades напрямую, а кладёт сделку в источник,
    откуда её забирает обычная сверка. Иначе он проверял бы не тот путь,
    по которому пойдут настоящие данные.
    """
    connection = await repo.active_connection(s, user_id)
    if connection is None or connection.provider != "fake":
        raise AppError(
            "no_fake_source",
            "Фейковый источник не активен. Подключи его на экране настроек.",
            409,
        )
    if side not in ("long", "short"):
        raise AppError("validation_failed", "Направление: long или short.", 400)
    if duration_sec <= 0 or minutes_ago < 0:
        raise AppError(
            "validation_failed", "Время сделки указано неверно.", UNPROCESSABLE
        )

    close_time = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)
    open_time = close_time - dt.timedelta(seconds=duration_sec)
    if close_time < connection.ingest_from:
        raise AppError(
            "before_ingest_from",
            "Сделка закрыта раньше момента подключения — сервис такие не принимает.",
            UNPROCESSABLE,
        )

    counter = await repo.fake_feed_size(s, connection.id)
    payload = {
        "external_id": f"fake-{connection.id.hex[:6]}-{counter + 1}",
        "account_external_id": ACCOUNT_EXTERNAL_ID,
        "symbol": symbol.upper(),
        "side": side,
        "profit_usd": str(profit_usd),
        "account_return_pct": str(account_return_pct),
        "percent": str(account_return_pct * 10),
        "size_usd": "5000.00",
        "leverage": "10",
        "duration_sec": duration_sec,
        "open_time": open_time.isoformat(),
        "close_time": close_time.isoformat(),
        "is_open": False,
        "tags": [
            {"external_id": _tag_id(name), "name": name, "column_key": "entry_reason"}
            for name in tags
        ],
    }
    await repo.add_fake_trade(s, connection.id, payload, close_time)
    return payload


def _tag_id(name: str) -> str:
    """Устойчивый идентификатор тега по имени: у фейка своих id нет.

    Именно sha256, а не встроенный hash(): тот рандомизирован для строк
    от запуска к запуску, и один и тот же тег получал бы разные id
    после каждого перезапуска процесса.
    """
    digest = hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()
    return f"tag-{digest[:12]}"
