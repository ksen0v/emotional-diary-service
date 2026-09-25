"""Словарь, из которого собирается правило. Чистые данные и чистые функции.

Три показателя — это сознательное сокращение ТЗ 6.3, а не недоделка: большой
словарь предикатов делает конструктор непонятным на старте, а эти три покрывают
сценарии из видения. Остальное лежит в бэклоге словаря и добавляется по одному.

Словарь зависит от активного источника. TMM не отдаёт открытые позиции, поэтому
честная просадка и нереализованный убыток у него недоступны — и показываются
затенёнными с объяснением, а не вырезаются молча (Архитектура ч.2 §3.6):
трейдер, читавший про честную просадку, иначе будет искать её и не найдёт.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# --- показатели ---

INT = "int"
DECIMAL = "decimal"


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    unit: str  # короткая подпись рядом с полем ввода
    unit_long: str  # то же во фразе: «не меньше 2 шт» / «не меньше 5 % депозита»
    phrase: str  # как показатель называется во фразе
    type: str
    min: Decimal
    max: Decimal
    hint: str | None = None
    requires: str | None = None  # возможность источника, без которой недоступен

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "unit": self.unit,
            "type": self.type,
            "min": self.min,
            "max": self.max,
        }
        if self.hint:
            out["hint"] = self.hint
        return out


METRICS: tuple[Metric, ...] = (
    Metric(
        key="loss_streak",
        name="Убыточных сделок подряд",
        unit="шт",
        unit_long="шт",
        phrase="убыточных сделок подряд",
        type=INT,
        min=Decimal("1"),
        max=Decimal("20"),
        hint=(
            "Считаются только значимые сделки — мельче порога значимости "
            "не прерывают и не удлиняют серию."
        ),
    ),
    Metric(
        key="drawdown_pct",
        name="Просадка от пика дня",
        unit="% деп.",
        unit_long="% депозита",
        phrase="просадка от пика дня",
        type=DECIMAL,
        min=Decimal("0.1"),
        max=Decimal("50"),
    ),
    Metric(
        key="loss_sum_pct",
        name="Убыток суммарно",
        unit="% деп.",
        unit_long="% депозита",
        phrase="суммарный убыток",
        type=DECIMAL,
        min=Decimal("0.1"),
        max=Decimal("50"),
        hint="Сумма результата за день, если она отрицательная.",
    ),
    # Две метрики источника с открытыми позициями (ТЗ 4.2, Архитектура ч.1 §5.7):
    # просадка считается честно, с учётом незакрытого минуса — того самого
    # момента тильта, который при источнике без позиций слепая зона.
    Metric(
        key="drawdown_full_pct",
        name="Просадка с учётом открытых позиций",
        unit="% деп.",
        unit_long="% депозита",
        phrase="просадка с учётом открытых позиций",
        type=DECIMAL,
        min=Decimal("0.1"),
        max=Decimal("50"),
        requires="provides_positions",
        hint=(
            "Проверяется на обновлении открытой позиции, а не на закрытой "
            "сделке: иначе правило срабатывало бы уже после того, как из "
            "минуса вышли."
        ),
    ),
    Metric(
        key="unrealized_pct",
        name="Нереализованный убыток",
        unit="% деп.",
        unit_long="% депозита",
        phrase="нереализованный убыток",
        type=DECIMAL,
        min=Decimal("0.1"),
        max=Decimal("50"),
        requires="provides_positions",
        hint=(
            "Проверяется на обновлении открытой позиции, а не на закрытой "
            "сделке. Значение отрицательное, пока позиция в минусе."
        ),
    ),
)

BY_KEY: dict[str, Metric] = {m.key: m for m in METRICS}

# Почему источник не даёт возможность — текст для затенённой метрики.
REQUIREMENT_REASON: dict[str, str] = {
    "provides_positions": "Активный источник не отдаёт открытые позиции.",
    "provides_balance": "Активный источник не отдаёт баланс счёта.",
    "provides_tags": "Активный источник не отдаёт теги разметки.",
}

NO_SOURCE_REASON = "Источник не подключён, поэтому его возможности неизвестны."

# --- сравнения и связки: ровно те, что в прототипе ---

COMPARATORS: tuple[tuple[str, str], ...] = (
    ("ge", "не меньше"),
    ("gt", "больше"),
    ("eq", "ровно"),
    ("le", "не больше"),
    ("lt", "меньше"),
)
CMP_WORD: dict[str, str] = dict(COMPARATORS)

CONNECTORS: tuple[tuple[str, str], ...] = (("and", "и"), ("or", "или"))
CONN_WORD: dict[str, str] = dict(CONNECTORS)

# --- условия снятия блокировки (ТЗ 6.6) ---

TIMER_MIN = 5
TIMER_MAX = 240
TIMER_DEFAULT = 30

UNLOCK_KEYS: tuple[str, ...] = ("timer", "review", "buddy")

UNLOCK_CONDITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "timer",
        "name": "Таймер",
        "params": {
            "minutes": {"min": TIMER_MIN, "max": TIMER_MAX, "default": TIMER_DEFAULT}
        },
    },
    {"key": "review", "name": "Разбор из трёх вопросов", "params": {}},
    {
        "key": "buddy",
        "name": "Подтверждение доверенного лица",
        "params": {},
        "requires_contact": True,
    },
)

# Максимум условий в правиле. ТЗ 6.3 говорил «не ограничено», Архитектура ч.2 §3.6
# поставила предел в пять — решение Влада от 23.09: считаем, что ч.2 уточнила ТЗ.
# Пять условий на плоском списке из трёх показателей — это уже больше, чем можно
# собрать осмысленно.
MAX_CONDITIONS = 5


def available(capabilities: dict[str, Any] | None) -> tuple[list[Metric], list[Metric]]:
    """Разделить словарь на доступное и недоступное при данном источнике.

    Пустые возможности (источник не подключён) — это не «всё доступно»: правило,
    собранное на метрике, которой источник не даёт, никогда не сработает, и
    обнаружится это молчанием вместо блокировки.
    """
    caps = capabilities or {}
    ready: list[Metric] = []
    blocked: list[Metric] = []
    for metric in METRICS:
        if metric.requires is None or bool(caps.get(metric.requires)):
            ready.append(metric)
        else:
            blocked.append(metric)
    return ready, blocked


def reason_for(metric: Metric, capabilities: dict[str, Any] | None) -> str:
    if not capabilities:
        return NO_SOURCE_REASON
    assert metric.requires is not None
    return REQUIREMENT_REASON.get(
        metric.requires, "Активный источник не поддерживает этот показатель."
    )


def catalog(
    capabilities: dict[str, Any] | None, significance_pct: Decimal
) -> dict[str, Any]:
    """Ответ GET /rules/metrics (Архитектура ч.2 §3.6)."""
    ready, blocked = available(capabilities)
    return {
        "metrics": [m.as_dict() for m in ready],
        "unavailable_metrics": [
            {
                "key": m.key,
                "name": m.name,
                "unit": m.unit,
                "requires": m.requires,
                "reason": reason_for(m, capabilities),
            }
            for m in blocked
        ],
        "comparators": [{"key": k, "name": n} for k, n in COMPARATORS],
        "connectors": [{"key": k, "name": n} for k, n in CONNECTORS],
        "significance_pct": significance_pct,
        "max_conditions": MAX_CONDITIONS,
        "unlock_conditions": [dict(c) for c in UNLOCK_CONDITIONS],
    }
