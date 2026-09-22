"""Метрики разметки. Чистые функции над уже собранными числами.

Три метрики из ТЗ, и у каждой есть причина считаться именно так:

**Покрытие разметкой** — доля сделок, у которых разметка есть вообще.
Нужна как честность коэффициента дисциплины: 95% дисциплины при покрытии 30%
означает не дисциплину, а то, что трейдер перестал размечать.

**Коэффициент дисциплины** — доля чистых среди РАЗМЕЧЕННЫХ, а не среди всех.
Иначе неразмеченные сделки автоматически улучшали бы показатель, и выгоднее
всего было бы не размечать вовсе.

**Цена эмоций** — сумма результата по сделкам-нарушениям. Отдельно считаются
нарушения в плюс: нарушение остаётся нарушением, даже если принесло деньги,
и именно эта цифра ломает логику «получилось же — значит можно».

**Слито на эмоциях** — только убыточные нарушения. Отдельно от цены эмоций,
потому что сальдо прячет и то и другое: −340 слитых и +28 заработанных
нарушением дают −312, и по этому числу не видно ни одного из двух фактов.
"""

from dataclasses import dataclass
from decimal import Decimal

CONFIDENCE_DAYS = 30


@dataclass(frozen=True)
class MarkingMetrics:
    trades_all: int
    trades_significant: int
    marked: int
    clean: int
    violations: int
    violations_profitable: int
    unmarked: int
    coverage_pct: Decimal
    discipline_pct: Decimal | None
    emotion_cost_usd: Decimal
    lost_on_emotions_usd: Decimal
    violations_gain_usd: Decimal

    def as_dict(self) -> dict:
        discipline = None if self.discipline_pct is None else str(self.discipline_pct)
        return {
            "trades": {"all": self.trades_all, "significant": self.trades_significant},
            "marked": self.marked,
            "unmarked": self.unmarked,
            "coverage_pct": str(self.coverage_pct),
            "discipline_pct": discipline,
            "violations": {
                "count": self.violations,
                "profitable": self.violations_profitable,
            },
            "emotion_cost_usd": str(self.emotion_cost_usd),
            "lost_on_emotions_usd": str(self.lost_on_emotions_usd),
            "violations_gain_usd": str(self.violations_gain_usd),
        }


def _pct(part: int, whole: int) -> Decimal:
    if whole == 0:
        return Decimal("0.00")
    return (Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.01"))


def compute(
    *,
    trades_all: int,
    trades_significant: int,
    clean: int,
    violations: int,
    unmarked: int,
    violations_profitable: int,
    violations_profit_sum: Decimal,
    violations_loss_sum: Decimal = Decimal("0"),
    violations_gain_sum: Decimal = Decimal("0"),
) -> MarkingMetrics:
    marked = clean + violations
    return MarkingMetrics(
        trades_all=trades_all,
        trades_significant=trades_significant,
        marked=marked,
        clean=clean,
        violations=violations,
        violations_profitable=violations_profitable,
        unmarked=unmarked,
        coverage_pct=_pct(marked, trades_all),
        # Без размеченных сделок коэффициент не существует: ноль означал бы
        # «полная недисциплинированность», а это не то же самое, что «не считали».
        discipline_pct=None if marked == 0 else _pct(clean, marked),
        emotion_cost_usd=violations_profit_sum.quantize(Decimal("0.01")),
        lost_on_emotions_usd=violations_loss_sum.quantize(Decimal("0.01")),
        violations_gain_usd=violations_gain_sum.quantize(Decimal("0.01")),
    )


def enough_data(days_with_trades: int) -> dict:
    """Порог достоверности из ТЗ 8: до 30 торговых дней выводы помечаем."""
    return {
        "enough_data": days_with_trades >= CONFIDENCE_DAYS,
        "days_available": days_with_trades,
        "days_required": CONFIDENCE_DAYS,
    }
