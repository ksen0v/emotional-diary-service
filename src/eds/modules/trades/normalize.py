"""Чистые функции приёма. Ни базы, ни сети — только данные на входе и выходе.

Здесь живут четыре решения, от которых зависит весь сервис, поэтому они вынесены
в функции и покрыты тестами: торговый день сделки, значимость, разметка и хеш тегов.
"""

import datetime as dt
import hashlib
import zoneinfo
from dataclasses import dataclass
from decimal import Decimal

from eds.contracts.source import IncomingTag

MARK_CLEAN = "clean"
MARK_VIOLATION = "violation"
MARK_UNREVIEWED = "unreviewed"


def trading_day(open_time: dt.datetime, timezone: str, cutoff: dt.time) -> dt.date:
    """Торговый день сделки: по времени ОТКРЫТИЯ, в таймзоне трейдера.

    Граница дня сдвигает сутки: при границе 03:00 сделка, открытая в 01:30,
    относится к предыдущему торговому дню. Так ночная торговля не разрывается
    на два дня посередине сессии.

    Значение фиксируется при приёме и больше не меняется: при смене таймзоны
    история не пересчитывается (решение Архитектуры ч.1).
    """
    local = open_time.astimezone(zoneinfo.ZoneInfo(timezone))
    day = local.date()
    if local.time() < cutoff:
        day = day - dt.timedelta(days=1)
    return day


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
