"""Правило зачёта дня. Чистые функции — ни базы, ни сети.

Здесь живёт вся логика стрика, и она вынесена отдельно ровно потому, что
её приходится проверять таблицей случаев: пять условий ТЗ 7.1, плюс дни
вне рынка, плюс заморозки, и каждое сочетание должно давать ровно один ответ.
"""

from eds.contracts.streaks import (
    REASON_BREACH,
    REASON_FROZEN,
    REASON_NO_CHECK,
    REASON_NO_ENTRY,
    REASON_NO_REVIEW,
    REASON_OK,
    REASON_VIOLATION,
    DayFacts,
    DayMark,
)

# Человеческие объяснения. Живут рядом с правилом: текст «почему не зачтён»
# и сам расчёт должны меняться вместе, иначе они разойдутся.
REASON_TEXT = {
    REASON_OK: "День зачтён",
    REASON_VIOLATION: "Есть нарушения",
    REASON_BREACH: "Нарушена блокировка",
    REASON_NO_REVIEW: "Разбор не заполнен",
    REASON_NO_CHECK: "Были сделки без допуска",
    REASON_NO_ENTRY: "Запись дня не заполнена",
    REASON_FROZEN: "Заморожен",
}

CONDITIONS = (
    "Пройден пре-маркет чек",
    "Заполнены запись дня и разбор",
    "Ноль нарушений",
    "Ноль нарушенных блокировок",
    "Ноль сделок без допуска",
)


def mark_of(facts: DayFacts) -> DayMark | None:
    """Зачтён ли день и почему. None — день нейтральный, его нет в расчёте.

    Порядок проверок — это приоритет причин: сначала то, что тяжелее.
    Торговля без допуска стоит выше нарушения, потому что нарушение случается
    внутри разрешённой сессии, а торговля без допуска — вместо неё.
    """
    if facts.frozen:
        return DayMark(facts.day, counted=False, reason=REASON_FROZEN)

    active = facts.trades > 0 or facts.session_opened or facts.admission is not None
    if not active:
        # Вне рынка. Засчитывается короткой записью и не рвёт серию без неё:
        # выходные и отпуск стрик не рвут (ТЗ 7.2).
        if facts.entry_filled:
            return DayMark(facts.day, counted=True, reason=REASON_OK)
        return None

    if facts.trades > 0 and facts.admission in (None, "denied"):
        return DayMark(facts.day, counted=False, reason=REASON_NO_CHECK)
    if facts.lock_breaches > 0:
        return DayMark(facts.day, counted=False, reason=REASON_BREACH)
    if facts.violations > 0:
        return DayMark(facts.day, counted=False, reason=REASON_VIOLATION)
    # Разбор требуется там, где была торговля: в неторговый день разбирать
    # нечего, и ТЗ 7.1 прямо разрешает засчитать его короткой формой.
    if facts.trades > 0 and not facts.review_done:
        return DayMark(facts.day, counted=False, reason=REASON_NO_REVIEW)
    if not facts.entry_filled:
        return DayMark(facts.day, counted=False, reason=REASON_NO_ENTRY)
    return DayMark(facts.day, counted=True, reason=REASON_OK)


def current_streak(marks: list[DayMark]) -> int:
    """Текущая серия: считаем назад от последнего оценённого дня.

    Вход отсортирован по возрастанию дня. Нейтральные дни пропускаются —
    именно это делает заморозку заморозкой, а выходной выходным.
    """
    streak = 0
    for mark in reversed(marks):
        if mark.neutral:
            continue
        if not mark.counted:
            break
        streak += 1
    return streak


def best_streak(marks: list[DayMark]) -> int:
    """Лучшая серия за всю историю. Показывается рядом с текущей (ТЗ 7.2)."""
    best = 0
    running = 0
    for mark in marks:
        if mark.neutral:
            continue
        if mark.counted:
            running += 1
            best = max(best, running)
        else:
            running = 0
    return best
