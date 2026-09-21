"""Подключения источников, словарь тегов и наполнение фейковой ленты."""

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts import events as ev
from eds.contracts.source import IncomingAccount, IncomingTag
from eds.modules.source import repo
from eds.modules.source.adapters.fake.source import ACCOUNT_EXTERNAL_ID, FakeSource
from eds.modules.source.adapters.tmm.rest import TmmClient
from eds.modules.source.adapters.tmm.source import PROBE_DAYS, TmmSource
from eds.modules.source.models import Connection
from eds.platform import bus, crypto
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

    # Включаем только если активного источника ещё нет. Раньше фейк включался
    # всегда, но с появлением настоящего источника это значило бы, что кнопка
    # «подключить тестовый» молча отбирает активность у TMM — переключение
    # источников стало операцией с подтверждением, и обходить его нельзя.
    active = await repo.active_connection(s, user_id)
    if active is None:
        await repo.activate(s, connection)
    return connection


async def activate(
    s: AsyncSession,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    confirm: bool = False,
) -> Connection:
    """Сделать источник активным.

    Переключение между двумя источниками требует подтверждения: смешанные
    метрики из двух источников не означают ничего, поэтому после переключения
    расчёты начинаются заново. Первое включение подтверждать нечем — выбора нет.
    """
    connection = await repo.connection_by_id(s, user_id, connection_id)
    if connection is None:
        raise AppError("not_found", "Подключение не найдено.", 404)

    current = await repo.active_connection(s, user_id)
    if current is not None and current.id == connection.id:
        return connection
    if current is not None and not confirm:
        raise AppError(
            "confirmation_required",
            "Переключение источника меняет расчёты.",
            409,
            switch_consequences(current, connection),
        )

    await repo.deactivate_all(s, user_id)
    return await repo.activate(s, connection)


async def set_fake_capabilities(
    s: AsyncSession, user_id: uuid.UUID, *, provides_tags: bool
) -> Connection:
    """Заставить фейковый источник изображать источник без тегов.

    Нужно, чтобы своя разметка проверялась до появления Binance: у него тегов нет,
    и разметка живёт у нас. Работает только для фейка — настоящему источнику
    возможности не переписать, они его свойство, а не настройка.
    """
    connection = await repo.active_connection(s, user_id)
    if connection is None or connection.provider != "fake":
        raise AppError(
            "no_fake_source",
            "Возможности можно менять только у тестового источника.",
            409,
        )
    caps = dict(connection.capabilities)
    caps["provides_tags"] = provides_tags
    connection.capabilities = caps
    await s.flush()
    return connection


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

    # Источник без тегов не может их прислать. Отбрасываем здесь, а не в приёме:
    # приём должен видеть ровно то, что отдал бы настоящий источник.
    if not connection.capabilities.get("provides_tags", True):
        tags = []

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


# --- настоящие источники ---

TMM = "tmm"
# Провайдеры, которых мы умеем подключать ключом. binance добавится на шаге 14.
CONNECTABLE = (TMM,)


@dataclass
class Probe:
    """Что провайдер ответил на пробу подключения.

    Существует по одной причине: истории мы не импортируем, поэтому сразу после
    подключения лента пуста, и проверить «ключ рабочий, поля сошлись» нечем.
    Проба читает последние сделки и показывает их как есть, ничего не сохраняя.
    """

    accounts: list[IncomingAccount]
    entry_tags: list[IncomingTag]
    trades_seen: int
    sample: list[dict]
    tags_available: bool
    tags_problem: str | None
    accounts_from_trades: bool
    window_filter_honored: bool | None
    mapping_errors: list[str]

    def as_dict(self) -> dict:
        return {
            "accounts": [
                {"external_id": a.external_id, "name": a.name, "exchange": a.exchange}
                for a in self.accounts
            ],
            "entry_tags": [
                {"external_id": t.external_id, "name": t.name} for t in self.entry_tags
            ],
            "trades_seen": self.trades_seen,
            "sample": self.sample,
            "tags_available": self.tags_available,
            "tags_problem": self.tags_problem,
            "accounts_from_trades": self.accounts_from_trades,
            "window_filter_honored": self.window_filter_honored,
            "mapping_errors": self.mapping_errors[:5],
        }


def _client(key: str) -> TmmClient:
    return TmmClient(key)


async def probe_tmm(key: str, *, days: int = PROBE_DAYS) -> tuple[Probe, TmmSource, TmmClient]:
    """Проверить ключ и посмотреть, что за ним видно. В базу не пишет ничего."""
    client = _client(key)
    async with client:
        source = TmmSource(client)
        # Порядок важен: сначала теги, потом счета. Счета при отсутствии адреса
        # списка ключей выводятся из сделок, и словарь тегов к тому моменту
        # уже прочитан — иначе теги в образце оказались бы неразобранными.
        entry_tags = await source.fetch_tags()
        accounts = await source.fetch_accounts()
        trades = await source.probe_trades(days=days)

    sample = [_sample_row(t) for t in trades[:5]]
    probe = Probe(
        accounts=accounts,
        entry_tags=entry_tags,
        trades_seen=len(trades),
        sample=sample,
        tags_available=source.capabilities().provides_tags,
        tags_problem=source.tags_problem,
        accounts_from_trades=source.accounts_from_trades,
        window_filter_honored=source.window_filter_honored,
        mapping_errors=source.mapping_errors,
    )
    return probe, source, client


def _sample_row(trade) -> dict:
    return {
        "external_id": trade.external_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "profit_usd": str(trade.profit_usd),
        "account_return_pct": str(trade.account_return_pct),
        "duration_sec": trade.duration_sec,
        "open_time": trade.open_time.isoformat(),
        "close_time": trade.close_time.isoformat() if trade.close_time else None,
        "tags": [tag.name for tag in trade.tags],
        "is_open": trade.is_open,
    }


async def connect(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    provider: str,
    key: str,
    market: str | None = None,
    secret: str | None = None,
) -> dict:
    """Подключить источник по ключу.

    Синхронно: проверка ключа, список счетов, словарь тегов. Если проверка
    не прошла, подключение не создаётся — иначе в настройках остался бы
    источник, который никогда не заработает.
    """
    if provider not in CONNECTABLE:
        raise AppError(
            "provider_not_supported",
            f"Источник «{provider}» пока не подключается. TMM — сейчас, Binance — позже.",
            501,
        )
    if secret:
        raise AppError(
            "validation_failed", "У TMM нет секрета — нужен только ключ.", 400
        )
    key = (key or "").strip()
    if not key:
        raise AppError("validation_failed", "Ключ не введён.", 400)
    if not crypto.available():
        # Без мастер-ключа шифровать нечем, а хранить ключ открытым нельзя.
        raise AppError(
            "secret_key_missing",
            "Сервис не настроен: не задан ключ шифрования EDS_SECRET_KEY. "
            "Подключать источники нельзя.",
            503,
        )

    probe, source, client = await probe_tmm(key)

    existing = await repo.connection_by_provider(s, user_id, TMM, market)
    capabilities = source.capabilities().as_dict()
    if existing is None:
        connection = await repo.create_connection(
            s,
            user_id=user_id,
            provider=TMM,
            capabilities=capabilities,
            market=market,
            key_masked=crypto.mask(key),
            key_encrypted=crypto.encrypt(key),
            key_version=crypto.KEY_VERSION,
            base_url=client.base_in_use,
        )
    else:
        # Повторное подключение — это замена ключа. Точку отсчёта не двигаем:
        # она означает «с какого момента сервис видит сделки», и сдвиг назад
        # втянул бы историю, а вперёд — стёр бы уже принятые дни из расчётов.
        connection = existing
        connection.key_encrypted = crypto.encrypt(key)
        connection.key_masked = crypto.mask(key)
        connection.key_version = crypto.KEY_VERSION
        connection.capabilities = capabilities
        connection.base_url = client.base_in_use
        connection.state = "connected"
        connection.last_error = None
        await s.flush()

    accounts = await repo.upsert_accounts(s, user_id, connection.id, probe.accounts)
    await repo.learn_tags(s, user_id, connection.id, probe.entry_tags)
    await _save_limits(s, connection.id, client)

    # Первый источник включаем сразу: подтверждать нечего, выбора нет.
    # Второй требует подтверждения — это отдельная операция с последствиями.
    if await repo.active_connection(s, user_id) is None:
        await repo.activate(s, connection)

    return {
        "connection": connection,
        "accounts": accounts,
        "probe": probe,
        "warnings": _warnings(probe),
    }


def _warnings(probe: Probe) -> list[dict]:
    out: list[dict] = []
    if not probe.tags_available:
        out.append(
            {
                "code": "tags_unavailable",
                "message": "Словарь тегов прочитать не удалось, поэтому нарушения "
                "придётся отмечать вручную в ленте сделок. Причина: "
                f"{probe.tags_problem}.",
            }
        )
    if probe.accounts_from_trades:
        out.append(
            {
                "code": "accounts_from_trades",
                "message": "Список счётов провайдер не отдал, счета определены "
                "по самим сделкам. Имена будут служебными.",
            }
        )
    if probe.mapping_errors:
        out.append(
            {
                "code": "mapping_errors",
                "message": f"{len(probe.mapping_errors)} сделок не разобрались: "
                + "; ".join(probe.mapping_errors[:3]),
            }
        )
    return out


async def verify(s: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID) -> dict:
    """Проверить подключение заново. Точку отсчёта не меняет (контракт §3.3)."""
    connection = await repo.connection_by_id(s, user_id, connection_id)
    if connection is None:
        raise AppError("not_found", "Подключение не найдено.", 404)
    if connection.provider != TMM:
        raise AppError(
            "provider_not_supported",
            "Проверять нечего: у этого источника нет ключа.",
            409,
        )
    if not connection.key_encrypted:
        raise AppError("key_missing", "У подключения не сохранён ключ.", 409)

    key = crypto.decrypt(connection.key_encrypted)
    try:
        probe, source, client = await probe_tmm(key)
    except AppError as exc:
        await repo.set_state(s, connection, "error", exc.message)
        raise

    await repo.set_state(s, connection, "connected", None)
    await repo.set_capabilities(s, connection, source.capabilities().as_dict())
    accounts = await repo.upsert_accounts(s, user_id, connection.id, probe.accounts)
    await repo.learn_tags(s, user_id, connection.id, probe.entry_tags)
    await _save_limits(s, connection.id, client)
    return {
        "connection": connection,
        "accounts": accounts,
        "probe": probe,
        "warnings": _warnings(probe),
    }


async def _save_limits(s: AsyncSession, connection_id: uuid.UUID, client: TmmClient) -> None:
    await repo.save_rate_limit(
        s,
        connection_id,
        limit_value=client.rate_limit.limit,
        remaining=client.rate_limit.remaining,
        reset_at=client.rate_limit.reset_at,
    )


async def delete(s: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Удалить подключение. Сделки остаются — история неизменяема (ТЗ 9.2)."""
    connection = await repo.connection_by_id(s, user_id, connection_id)
    if connection is None:
        raise AppError("not_found", "Подключение не найдено.", 404)
    await repo.delete_connection(s, connection)


def switch_consequences(current: Connection | None, target: Connection) -> dict:
    """Что изменится при переключении источника.

    Список собирается из возможностей обоих источников, а не пишется во фронте:
    иначе при третьем источнике текст в диалоге останется про два.
    """
    was = dict(current.capabilities) if current else {}
    now = dict(target.capabilities)
    flags = [
        key
        for key in ("provides_tags", "provides_positions", "provides_balance")
    ]
    losing = [key for key in flags if was.get(key) and not now.get(key)]
    gaining = [key for key in flags if now.get(key) and not was.get(key)]

    consequences = [
        "Сделки нового источника начнут приходить с момента переключения — "
        "историю он не подтянет.",
        "Стрик начнёт считаться заново.",
        "Метрики по прежнему источнику останутся в архиве и в текущие не войдут.",
    ]
    if "provides_tags" in losing:
        consequences.append(
            "У нового источника нет тегов разметки — нарушения нужно будет "
            "отмечать вручную."
        )
    if "provides_tags" in gaining:
        consequences.append(
            "Новый источник отдаёт теги разметки — своя разметка станет "
            "недоступна, тег ставится в дневнике источника."
        )
    return {
        "consequences": consequences,
        "losing_capabilities": losing,
        "gaining_capabilities": gaining,
    }
