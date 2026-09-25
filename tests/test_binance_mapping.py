"""Перевод ответов Binance в наши формы и разбор прав ключа.

Ответы взяты в той форме, в которой их описывает документация биржи. Живого
ключа у сервиса при прогоне тестов нет и быть не должно, поэтому единственный
способ поймать расхождение в именах полей — зафиксировать их здесь.
"""

import datetime as dt
from decimal import Decimal

import pytest

from eds.modules.source.adapters.binance import mapping

T = dt.datetime(2026, 9, 18, 10, 30, tzinfo=dt.UTC)
MS = int(T.timestamp() * 1000)

USER_TRADE = {
    "symbol": "BTCUSDT",
    "id": 981123,
    "orderId": 55114,
    "side": "SELL",
    "positionSide": "BOTH",
    "price": "64120.50",
    "qty": "0.015",
    "realizedPnl": "-84.20",
    "commission": "0.38472",
    "commissionAsset": "USDT",
    "time": MS,
    "buyer": False,
    "maker": False,
}

ORDER_EVENT = {
    "e": "ORDER_TRADE_UPDATE",
    "E": MS,
    "T": MS,
    "o": {
        "s": "ETHUSDT",
        "i": 91001,
        "t": 770012,
        "S": "BUY",
        "ps": "BOTH",
        "x": "TRADE",
        "X": "FILLED",
        "l": "0.4",
        "L": "2480.10",
        "n": "0.19",
        "N": "USDT",
        "rp": "12.40",
        "T": MS,
    },
}


def test_user_trade_becomes_a_fill() -> None:
    fill = mapping.fill_from_row(USER_TRADE)
    assert fill.external_id == 981123
    assert fill.symbol == "BTCUSDT"
    assert fill.side == "SELL"
    assert fill.qty == Decimal("0.015")
    assert fill.realized_pnl == Decimal("-84.20")
    assert fill.commission_asset == "USDT"
    assert fill.trade_time == T
    # Продажа уменьшает позицию — на этом знаке держится весь агрегатор.
    assert fill.signed_qty == Decimal("-0.015")


def test_stream_event_becomes_the_same_fill() -> None:
    fill = mapping.fill_from_stream(ORDER_EVENT)
    assert fill is not None
    assert fill.external_id == 770012
    assert fill.symbol == "ETHUSDT"
    assert fill.price == Decimal("2480.10")
    assert fill.realized_pnl == Decimal("12.40")


def test_non_trade_events_are_ignored() -> None:
    """Событие приходит на любое изменение ордера, а сделку создаёт только исполнение.

    Принимать остальное значило бы собирать позицию из ордеров, которых не было.
    """
    for execution in ("NEW", "CANCELED", "EXPIRED", "CALCULATED"):
        event = {"e": "ORDER_TRADE_UPDATE", "E": MS, "o": {**ORDER_EVENT["o"], "x": execution}}
        assert mapping.fill_from_stream(event) is None


def test_broken_row_is_named_not_swallowed() -> None:
    with pytest.raises(mapping.MappingError):
        mapping.fill_from_row({**USER_TRADE, "qty": "не число"})


def test_income_and_symbols() -> None:
    rows = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.41",
            "asset": "USDT",
            "time": MS,
            "tranId": 771,
        },
        {
            "symbol": "",
            "incomeType": "TRANSFER",
            "income": "500",
            "asset": "USDT",
            "time": MS,
            "tranId": 772,
        },
        {
            "symbol": "ETHUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "12.4",
            "asset": "USDT",
            "time": MS,
            "tranId": 773,
        },
    ]
    parsed = [mapping.income_from_row(row) for row in rows]
    assert parsed[0].income == Decimal("-0.41")
    assert parsed[1].symbol is None
    # Символы — точка входа двухходового импорта: `userTrades` без символа
    # не спросить, а `income` его не требует.
    assert mapping.symbols_of(parsed) == ["BTCUSDT", "ETHUSDT"]


def test_position_and_balance() -> None:
    position = mapping.position_from_row(
        {
            "symbol": "BTCUSDT",
            "positionSide": "BOTH",
            "positionAmt": "-0.02",
            "entryPrice": "64000",
            "markPrice": "64500",
            "unRealizedProfit": "-10.0",
            "liquidationPrice": "71000",
        }
    )
    assert position.is_open is True
    assert position.unrealized_usd == Decimal("-10.0")

    balance = mapping.balance_from_rows(
        [
            {"asset": "BNB", "balance": "1.2", "crossUnPnl": "0"},
            {"asset": "USDT", "balance": "32800.55", "crossUnPnl": "-10.0"},
        ],
        taken_at=T,
    )
    assert balance.wallet_usdt == Decimal("32800.55")
    assert balance.equity_usdt == Decimal("32790.55")


def test_balance_without_usdt_is_an_error() -> None:
    """Рынок у нас только USDⓈ-M. Складывать монеты по курсу — другая интеграция."""
    with pytest.raises(mapping.MappingError):
        mapping.balance_from_rows([{"asset": "BNB", "balance": "1"}], taken_at=T)


# --- права ключа: главный выигрыш Binance по сравнению с TMM ---

FULL_READ = {
    "enableReading": True,
    "enableWithdrawals": False,
    "enableFutures": True,
    "enableSpotAndMarginTrading": False,
    "ipRestrict": False,
}


def test_withdrawal_right_is_a_hard_refusal() -> None:
    """Право вывода — отказ, а не предупреждение.

    Это цена компромисса с IP whitelist: ключ без привязки к адресу при утечке
    работает откуда угодно, и терпимой утечку делает только невозможность
    увести по такому ключу деньги.
    """
    verdict = mapping.check_key({**FULL_READ, "enableWithdrawals": True})
    assert verdict.allowed is False
    assert verdict.refusal[0] == "key_has_withdrawal"


def test_ip_whitelist_is_a_refusal() -> None:
    verdict = mapping.check_key({**FULL_READ, "ipRestrict": True})
    assert verdict.refusal[0] == "key_ip_restricted"


def test_no_reading_is_a_refusal() -> None:
    verdict = mapping.check_key({**FULL_READ, "enableReading": False})
    assert verdict.refusal[0] == "key_no_reading"


def test_trading_right_is_a_warning_not_a_refusal() -> None:
    verdict = mapping.check_key(FULL_READ)
    assert verdict.allowed is True
    assert "key_can_trade" in {code for code, _ in verdict.warnings}


def test_futures_off_is_named_too() -> None:
    """Обратная сторона того же права: без него биржа может не отдать и историю.

    Предупреждение, а не отказ: проверить это можно только на живом ключе,
    а решение «отказывать» не записано ни в ТЗ, ни в архитектуре.
    """
    verdict = mapping.check_key({**FULL_READ, "enableFutures": False})
    assert verdict.allowed is True
    assert "key_futures_off" in {code for code, _ in verdict.warnings}


def test_permissions_snapshot_is_kept() -> None:
    """Снимок прав сохраняется целиком: потом будет видно, с чем работали."""
    verdict = mapping.check_key({**FULL_READ, "createTime": MS})
    assert verdict.permissions["enableReading"] is True
    assert verdict.permissions["createTime"] == MS
