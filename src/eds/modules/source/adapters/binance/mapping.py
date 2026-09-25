"""Перевод ответов Binance в наши формы. Чистые функции, ни сети, ни базы.

Отдельный файл по той же причине, что у TMM: когда провайдер поменяет имя поля,
чинить придётся здесь, а не в половине адаптера. Плюс на чистых функциях
проверяются записанные ответы биржи из `tests/fixtures/` — без сети и без ключа.

Здесь же живёт разбор прав ключа. Он не про данные, а про безопасность,
но по сути это то же самое: ответ провайдера превращается в наше решение.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from eds.contracts.source import SourceBalance, SourcePosition
from eds.modules.source.adapters.binance.aggregate import Fill, IncomeRow
from eds.modules.source.adapters.binance.rest import at


class MappingError(Exception):
    """Ответ провайдера не разобрался. Одна запись не должна валить проход."""


def _decimal(row: dict, *names: str, default: str | None = None) -> Decimal:
    for name in names:
        if name in row and row[name] is not None:
            try:
                return Decimal(str(row[name]))
            except (InvalidOperation, ValueError) as exc:
                raise MappingError(f"{name}={row[name]!r} — не число") from exc
    if default is not None:
        return Decimal(default)
    raise MappingError(f"нет ни одного из полей: {', '.join(names)}")


def _text(row: dict, *names: str, default: str | None = None) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value) != "":
            return str(value)
    if default is not None:
        return default
    raise MappingError(f"нет ни одного из полей: {', '.join(names)}")


# --- исполнения ---


def fill_from_row(row: dict) -> Fill:
    """Исполнение из `GET /fapi/v1/userTrades`."""
    try:
        return Fill(
            external_id=int(row["id"]),
            order_id=int(row.get("orderId") or 0),
            symbol=_text(row, "symbol"),
            position_side=_text(row, "positionSide", default="BOTH"),
            side=_text(row, "side"),
            price=_decimal(row, "price"),
            qty=_decimal(row, "qty"),
            realized_pnl=_decimal(row, "realizedPnl", default="0"),
            commission=_decimal(row, "commission", default="0"),
            commission_asset=_text(row, "commissionAsset", default="USDT"),
            trade_time=at(row["time"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MappingError(f"исполнение не разобрано: {exc}") from exc


# Тип события потока и тип исполнения внутри него.
ORDER_UPDATE = "ORDER_TRADE_UPDATE"
ACCOUNT_UPDATE = "ACCOUNT_UPDATE"
EXECUTION_TRADE = "TRADE"


def fill_from_stream(event: dict) -> Fill | None:
    """Исполнение из события `ORDER_TRADE_UPDATE`.

    События приходят на каждое изменение ордера — на постановку, на отмену,
    на исполнение. Нас интересует только `x == "TRADE"`: остальное сделок
    не создаёт, и принимать его значило бы собирать позицию из ордеров,
    которых не было.
    """
    order = event.get("o") or {}
    if order.get("x") != EXECUTION_TRADE:
        return None
    try:
        return Fill(
            external_id=int(order["t"]),
            order_id=int(order.get("i") or 0),
            symbol=_text(order, "s"),
            position_side=_text(order, "ps", default="BOTH"),
            side=_text(order, "S"),
            price=_decimal(order, "L", "ap", "p"),
            qty=_decimal(order, "l"),
            realized_pnl=_decimal(order, "rp", default="0"),
            commission=_decimal(order, "n", default="0"),
            commission_asset=_text(order, "N", default="USDT"),
            trade_time=at(order.get("T") or event.get("E")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MappingError(f"событие исполнения не разобрано: {exc}") from exc


# --- начисления ---


def income_from_row(row: dict) -> IncomeRow:
    """Начисление из `GET /fapi/v1/income`: фандинг, комиссии, переводы."""
    try:
        return IncomeRow(
            symbol=row.get("symbol") or None,
            income_type=_text(row, "incomeType"),
            income=_decimal(row, "income", default="0"),
            asset=_text(row, "asset", default="USDT"),
            happened_at=at(row["time"]),
            external_id=int(row["tranId"]) if row.get("tranId") else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MappingError(f"начисление не разобрано: {exc}") from exc


def symbols_of(income: list[IncomeRow]) -> list[str]:
    """Символы, по которым за окно была хоть какая-то активность.

    Это и есть причина двухходового импорта: `userTrades` требует символ,
    а `income` — нет. Без этого шага пришлось бы перебирать все четыреста
    с лишним торгуемых символов (Архитектура ч.1 §5.4).
    """
    seen: list[str] = []
    for row in income:
        if row.symbol and row.symbol not in seen:
            seen.append(row.symbol)
    return sorted(seen)


# --- позиции и баланс ---


def position_from_row(row: dict) -> SourcePosition:
    """Открытая позиция из `GET /fapi/v3/positionRisk`."""
    try:
        return SourcePosition(
            symbol=_text(row, "symbol"),
            position_side=_text(row, "positionSide", default="BOTH"),
            qty=_decimal(row, "positionAmt", default="0"),
            entry_price=_decimal(row, "entryPrice", default="0"),
            mark_price=_decimal(row, "markPrice", default="0"),
            unrealized_usd=_decimal(row, "unRealizedProfit", "unrealizedProfit", default="0"),
            liquidation=_decimal(row, "liquidationPrice", default="0"),
        )
    except MappingError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise MappingError(f"позиция не разобрана: {exc}") from exc


USDT = "USDT"


def balance_from_rows(rows: list[dict], *, taken_at: dt.datetime) -> SourceBalance:
    """Баланс из `GET /fapi/v2/balance`.

    Берём строку USDT: рынок у нас только USDⓈ-M, и складывать монеты
    по курсу значило бы завести ещё одну интеграцию с ценами.
    """
    for row in rows:
        if row.get("asset") != USDT:
            continue
        wallet = _decimal(row, "balance", "walletBalance", default="0")
        margin = _decimal(row, "crossUnPnl", default="0")
        return SourceBalance(
            wallet_usdt=wallet, equity_usdt=wallet + margin, taken_at=taken_at
        )
    raise MappingError("в балансе нет строки USDT")


def balance_from_account(row: dict, *, taken_at: dt.datetime) -> SourceBalance:
    """Баланс из `GET /fapi/v2/account` — запасной путь, если balance недоступен."""
    wallet = _decimal(row, "totalWalletBalance", default="0")
    unrealized = _decimal(row, "totalUnrealizedProfit", default="0")
    return SourceBalance(
        wallet_usdt=wallet, equity_usdt=wallet + unrealized, taken_at=taken_at
    )


# --- права ключа ---


@dataclass(frozen=True)
class KeyVerdict:
    """Решение по правам ключа: подключаем, отказываем, предупреждаем.

    Отказ несёт готовый код и текст — те же, что в контракте (Архитектура
    ч.2 §3.3). Фронт по коду ветвится, текст показывает как есть.
    """

    permissions: dict[str, Any]
    refusal: tuple[str, str] | None = None
    warnings: tuple[tuple[str, str], ...] = ()

    @property
    def allowed(self) -> bool:
        return self.refusal is None


def check_key(raw: dict) -> KeyVerdict:
    """Разобрать `GET /sapi/v1/account/apiRestrictions`.

    Главный выигрыш Binance по сравнению с TMM: здесь read-only не инструкция
    трейдеру, а проверяемое свойство ключа (Архитектура ч.1 §4.8).

    Право вывода — жёсткий отказ, а не предупреждение. Это цена компромисса
    с IP whitelist: ключ без привязки к адресу при утечке работает откуда
    угодно, и единственное, что делает утечку терпимой, — невозможность
    увести по такому ключу деньги.
    """
    permissions = {
        "enableReading": bool(raw.get("enableReading")),
        "enableWithdrawals": bool(raw.get("enableWithdrawals")),
        "enableFutures": bool(raw.get("enableFutures")),
        "enableSpotAndMarginTrading": bool(raw.get("enableSpotAndMarginTrading")),
        "enableMargin": bool(raw.get("enableMargin")),
        "ipRestrict": bool(raw.get("ipRestrict")),
        "createTime": raw.get("createTime"),
    }

    if permissions["enableWithdrawals"]:
        return KeyVerdict(
            permissions,
            refusal=(
                "key_has_withdrawal",
                "У ключа есть право вывода средств. Такой ключ сервис не "
                "подключает: создай ключ только с правом чтения.",
            ),
        )
    if permissions["ipRestrict"]:
        return KeyVerdict(
            permissions,
            refusal=(
                "key_ip_restricted",
                "Ключ привязан к IP. Создай ключ без привязки — наш адрес меняется.",
            ),
        )
    if not permissions["enableReading"]:
        return KeyVerdict(
            permissions,
            refusal=(
                "key_no_reading",
                "У ключа нет права чтения — сделки читать нечем.",
            ),
        )

    warnings: list[tuple[str, str]] = []
    if permissions["enableFutures"]:
        warnings.append(
            (
                "key_can_trade",
                "У ключа есть право торговли фьючерсами. Сервису оно не нужно — "
                "надёжнее пересоздать ключ только с правом чтения.",
            )
        )
    else:
        # Обратная сторона того же права: по документации Binance доступ
        # к fapi открывает именно оно, и ключ без него может не отдать
        # даже историю сделок. Проверить это можно только на живом ключе,
        # поэтому здесь предупреждение, а не отказ.
        warnings.append(
            (
                "key_futures_off",
                "У ключа выключен доступ к фьючерсам. Если Binance откажется "
                "отдавать сделки, включи в правах ключа Futures — на чтение "
                "этого достаточно.",
            )
        )
    if permissions["enableSpotAndMarginTrading"]:
        warnings.append(
            (
                "key_can_trade_spot",
                "У ключа есть право торговли на споте. Сервису оно не нужно — "
                "надёжнее пересоздать ключ только с правом чтения.",
            )
        )
    return KeyVerdict(permissions, warnings=tuple(warnings))
