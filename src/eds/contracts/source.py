"""Порт источника сделок и формы данных на его границе.

Это шов, по которому TMM, Binance и фейк подключаются одинаково. Модуль trades
знает только этот файл и не знает, откуда пришла сделка, — иначе при добавлении
второго источника пришлось бы править приём.
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class SourceCapabilities:
    """Что источник умеет. Интерфейс смотрит на возможности, а не на имя провайдера.

    Проверка `if provider == 'binance'` в коде — та самая ошибка, из-за которой
    третий источник потребует правок в десяти местах.
    """

    provides_trades: bool = True
    provides_tags: bool = False
    provides_positions: bool = False
    provides_balance: bool = False
    needs_aggregation: bool = False
    history_depth: str = "full"  # full | days:90

    def as_dict(self) -> dict[str, Any]:
        return {
            "provides_trades": self.provides_trades,
            "provides_tags": self.provides_tags,
            "provides_positions": self.provides_positions,
            "provides_balance": self.provides_balance,
            "needs_aggregation": self.needs_aggregation,
            "history_depth": self.history_depth,
        }


@dataclass(frozen=True)
class IncomingTag:
    """Тег разметки со стороны источника."""

    external_id: str
    name: str
    column_key: str = "entry_reason"


@dataclass(frozen=True)
class IncomingAccount:
    external_id: str
    name: str
    exchange: str | None = None
    market: str | None = None


@dataclass(frozen=True)
class IncomingTrade:
    """Сделка в том виде, в котором её принимает модуль trades.

    Все проценты — уже приведённые: `account_return_pct` это PnL в процентах
    от депозита (у TMM это `profit_deposit`). Приводит адаптер, а не приёмник:
    так арифметика провайдера остаётся в адаптере провайдера.
    """

    external_id: str
    account_external_id: str
    symbol: str
    side: str  # long | short
    profit_usd: Decimal
    account_return_pct: Decimal
    open_time: dt.datetime
    close_time: dt.datetime | None = None
    percent: Decimal | None = None
    size_usd: Decimal | None = None
    leverage: Decimal | None = None
    duration_sec: int | None = None
    is_open: bool = False
    tags: tuple[IncomingTag, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourcePosition:
    """Открытая позиция. Есть только у источника с `provides_positions`."""

    symbol: str
    position_side: str
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal | None
    unrealized_usd: Decimal
    liquidation: Decimal | None

    @property
    def is_open(self) -> bool:
        return self.qty != 0


@dataclass(frozen=True)
class SourceBalance:
    """Снимок баланса счёта. Есть только у источника с `provides_balance`."""

    wallet_usdt: Decimal
    equity_usdt: Decimal
    taken_at: dt.datetime


class TradeSource(Protocol):
    """Порт. Реализации: fake, tmm, binance."""

    provider: str

    def capabilities(self) -> SourceCapabilities: ...

    async def fetch_accounts(self) -> list[IncomingAccount]: ...

    async def fetch_tags(self) -> list[IncomingTag]:
        """Словарь тегов разметки целиком, не только те, что встретились в сделках.

        Нужен, потому что историю мы не импортируем: сразу после подключения
        сделок ещё нет, а отметить «этот тег — нарушение» трейдеру нужно уже
        тогда. Источник без тегов возвращает пустой список.
        """
        ...

    async def fetch_trades(
        self, since: dt.datetime, until: dt.datetime | None = None
    ) -> list[IncomingTrade]:
        """Сделки, закрытые в окне. Окно никогда не уходит раньше ingest_from."""
        ...

    async def fetch_positions(self) -> list[SourcePosition]:
        """Открытые позиции. Источник без них возвращает пустой список.

        Пустой список, а не исключение: спрашивать источник о том, что он
        объявил в возможностях, — обычный путь, и ветвление по имени провайдера
        на стороне вызывающего это ровно то, чего возможности и избегают.
        """
        ...

    async def fetch_balance(self) -> SourceBalance | None:
        """Баланс счёта. None — источник его не отдаёт.

        None, а не ноль: ноль означал бы пустой счёт, и проценты от депозита
        посчитались бы делением на него.
        """
        ...
