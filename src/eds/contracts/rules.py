"""Что движок правил знает о сделке и что он отдаёт наружу.

Движку нужны сделки, а модуль rules не имеет права читать схему trades.
Поэтому сделка приходит сюда фактом из четырёх чисел — ровно тем, от чего
зависят три показателя ТЗ 6.3, — а собирает эти факты оркестрация. Тот же
приём, что у `DayFacts` в контрактах стриков: правило остаётся чистой
функцией, которую можно разложить по таблице в тестах.

Обратно движок отдаёт `Firing` — срабатывание правила. Его читает модуль
incidents, который про правила не знает ничего, кроме этой формы: имя,
текст на момент срабатывания, действия и условия снятия.
"""

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TradeFact:
    """Сделка глазами движка. Всё остальное о ней движку не нужно."""

    trade_id: uuid.UUID
    open_time: dt.datetime
    close_time: dt.datetime
    account_return_pct: Decimal
    profit_usd: Decimal
    is_significant: bool


@dataclass(frozen=True)
class OpenTrade:
    """Сделка, открытая внутри окна блокировки. Доказательство нарушения.

    Сравнивается время ОТКРЫТИЯ, а не время, когда мы о сделке узнали:
    сделка, открытая до блокировки и закрытая внутри неё, нарушением
    не является (Архитектура ч.1 §7).
    """

    trade_id: uuid.UUID
    symbol: str
    open_time: dt.datetime


@dataclass(frozen=True)
class Firing:
    """Правило сработало. То, что движок передаёт в инциденты.

    `rule_text` — фраза правила на момент срабатывания, а не ссылка на правило:
    инцидент обязан объяснять себя и после того, как правило поправили или
    удалили (Архитектура ч.2 §3.6).
    """

    rule_id: uuid.UUID
    rule_name: str
    rule_text: str
    rule_version: int
    day: dt.date
    trigger_ref: str
    trade_id: uuid.UUID | None
    actions: dict[str, Any]
    unlock: dict[str, bool]
    snapshot: dict[str, Any] = field(default_factory=dict)
