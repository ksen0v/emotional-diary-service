"""Чистые функции приёма. Ни базы, ни сети — только данные на входе и выходе.

Здесь живут четыре решения, от которых зависит весь сервис, поэтому они вынесены
в функции и покрыты тестами: торговый день сделки, значимость, разметка и хеш тегов.
"""

import datetime as dt
import hashlib
from dataclasses import dataclass
from decimal import Decimal

from eds.contracts.source import IncomingTag

# Правило торгового дня общее для приёма сделок и для сессии, поэтому живёт
# в контрактах. Здесь оставлено под прежним именем: приём считает день
# по времени ОТКРЫТИЯ сделки и фиксирует его навсегда.
from eds.contracts.trading_time import trading_day

MARK_CLEAN = "clean"
MARK_VIOLATION = "violation"
MARK_UNREVIEWED = "unreviewed"

__all__ = [
    "MARK_CLEAN",
    "MARK_UNREVIEWED",
    "MARK_VIOLATION",
    "CurvePoint",
    "day_curve",
    "is_significant",
    "loss_streak",
    "marking_of",
    "tags_hash",
    "trading_day",
]


def is_significant(account_return_pct: Decimal, significance_pct: Decimal) -> bool:
    """Значима ли сделка. Пыль прозрачна для серий убытков.

    Без этого порога скальпинг ломает правила: мелкая прибыльная сделка между
    двумя стопами обрывала бы серию и спасала от блокировки.
    """
    return abs(account_return_pct) >= abs(significance_pct)


def marking_of(
    tags: tuple[IncomingTag, ...] | list[IncomingTag], violation_ids: set[str]
) -> str:
    """Разметка сделки по тегам входа.

    Нет тегов — `unreviewed`: трейдер ещё не сказал, по системе была сделка или нет,
    и считать её чистой нельзя, иначе коэффициент дисциплины будет завышен.
    """
    if not tags:
        return MARK_UNREVIEWED
    if any(tag.external_id in violation_ids for tag in tags):
        return MARK_VIOLATION
    return MARK_CLEAN


def tags_hash(tags: tuple[IncomingTag, ...] | list[IncomingTag]) -> str:
    """Отпечаток набора тегов.

    Нужен из-за потока: обновление сделки приходит на любое изменение, а нас
    интересует только изменение разметки. Сравнение хеша отсекает лишние события
    до того, как они дойдут до движка правил.
    """
    parts = sorted(f"{tag.column_key}:{tag.external_id}:{tag.name}" for tag in tags)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class CurvePoint:
    at: dt.datetime
    trade_id: str
    equity_pct: Decimal
    peak_pct: Decimal
    drawdown_pct: Decimal


def day_curve(points: list[tuple[dt.datetime, str, Decimal]]) -> list[CurvePoint]:
    """Кривая дня: накопленный процент от депозита и просадка от пика.

    `peak` стартует с нуля, а не с первой сделки: просадка считается от лучшей
    точки дня, а если день сразу пошёл в минус — от нуля. Иначе первый убыток
    сам становился бы пиком и обнулял просадку.

    Порядок — по времени закрытия. Вход обязан быть отсортирован: при сверке
    порция приходит неупорядоченной, и неотсортированный вход даёт неверную серию.
    """
    equity = Decimal("0")
    peak = Decimal("0")
    out: list[CurvePoint] = []
    for at, trade_id, account_return_pct in points:
        equity += account_return_pct
        peak = max(peak, equity)
        out.append(
            CurvePoint(
                at=at,
                trade_id=trade_id,
                equity_pct=equity,
                peak_pct=peak,
                drawdown_pct=peak - equity,
            )
        )
    return out


def loss_streak(returns: list[tuple[Decimal, bool]]) -> int:
    """Текущая серия убыточных сделок подряд (ТЗ 6.3).

    Пыль прозрачна: сделка ниже порога значимости серию не продолжает и не
    обнуляет. Без этого мелкая прибыль в три цента между двумя стопами спасала
    бы от блокировки, а мелкий убыток изображал бы серию, которой не было.

    Вход — в порядке закрытия сделок: пары (процент от депозита, значимость).
    """
    streak = 0
    for account_return_pct, significant in returns:
        if not significant:
            continue
        if account_return_pct < 0:
            streak += 1
        else:
            streak = 0
    return streak
