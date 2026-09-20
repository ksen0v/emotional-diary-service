"""Чистые функции приёма. Каждое решение из архитектуры — отдельным тестом."""

import datetime as dt
from decimal import Decimal

from eds.contracts.source import IncomingTag
from eds.modules.trades import normalize

MSK = "Europe/Moscow"
MIDNIGHT = dt.time(0, 0)


def utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


# --- торговый день ---


def test_trading_day_uses_trader_timezone() -> None:
    # 21:30 UTC = 00:30 МСК следующего дня
    assert normalize.trading_day(utc(2026, 9, 19, 21, 30), MSK, MIDNIGHT) == dt.date(
        2026, 9, 20
    )


def test_trading_day_uses_open_time_not_close() -> None:
    """Сделка относится к дню по времени ОТКРЫТИЯ (ТЗ 9.4)."""
    opened = utc(2026, 9, 19, 20, 50)  # 23:50 МСК
    assert normalize.trading_day(opened, MSK, MIDNIGHT) == dt.date(2026, 9, 19)


def test_cutoff_shifts_night_trades_to_previous_day() -> None:
    """При границе 03:00 сделка в 01:30 по местному — это ещё прошлый день.

    Иначе ночная сессия разрывалась бы на два торговых дня посередине.
    """
    cutoff = dt.time(3, 0)
    night = utc(2026, 9, 19, 22, 30)  # 01:30 МСК 20-го
    assert normalize.trading_day(night, MSK, cutoff) == dt.date(2026, 9, 19)


def test_cutoff_boundary_is_inclusive_for_new_day() -> None:
    cutoff = dt.time(3, 0)
    exactly = utc(2026, 9, 20, 0, 0)  # 03:00 МСК
    assert normalize.trading_day(exactly, MSK, cutoff) == dt.date(2026, 9, 20)


# --- значимость ---


def test_significance_threshold_inclusive() -> None:
    threshold = Decimal("0.50")
    assert normalize.is_significant(Decimal("0.50"), threshold) is True
    assert normalize.is_significant(Decimal("0.49"), threshold) is False


def test_significance_ignores_sign() -> None:
    threshold = Decimal("0.50")
    assert normalize.is_significant(Decimal("-0.68"), threshold) is True


# --- разметка ---


def tag(external_id: str, name: str = "тег") -> IncomingTag:
    return IncomingTag(external_id=external_id, name=name)


def test_no_tags_means_unreviewed() -> None:
    """Без тега сделка не «чистая», а неразмеченная.

    Иначе коэффициент дисциплины считался бы по сделкам, которые трейдер
    вообще не смотрел, и был бы завышен.
    """
    assert normalize.marking_of([], set()) == normalize.MARK_UNREVIEWED


def test_violation_tag_wins() -> None:
    tags = [tag("1", "ПО СИСТЕМЕ"), tag("2", "ЛУДКА")]
    assert normalize.marking_of(tags, {"2"}) == normalize.MARK_VIOLATION


def test_tags_without_violation_are_clean() -> None:
    tags = [tag("1", "ПО СИСТЕМЕ")]
    assert normalize.marking_of(tags, {"2"}) == normalize.MARK_CLEAN


# --- хеш тегов ---


def test_tags_hash_ignores_order() -> None:
    a = [tag("1", "первый"), tag("2", "второй")]
    b = [tag("2", "второй"), tag("1", "первый")]
    assert normalize.tags_hash(a) == normalize.tags_hash(b)


def test_tags_hash_changes_with_content() -> None:
    assert normalize.tags_hash([tag("1", "а")]) != normalize.tags_hash([tag("1", "б")])


def test_empty_tags_hash_is_stable() -> None:
    assert normalize.tags_hash([]) == normalize.tags_hash([])


# --- кривая дня ---


def point(minute: int, pct: str) -> tuple[dt.datetime, str, Decimal]:
    return utc(2026, 9, 20, 10, minute), f"t{minute}", Decimal(pct)


def test_peak_starts_at_zero() -> None:
    """День, сразу пошедший в минус, даёт просадку от нуля, а не от первой сделки."""
    curve = normalize.day_curve([point(0, "-1.0"), point(5, "-0.5")])
    assert curve[0].peak_pct == Decimal("0")
    assert curve[0].drawdown_pct == Decimal("1.0")
    assert curve[1].drawdown_pct == Decimal("1.5")


def test_drawdown_measured_from_best_point() -> None:
    curve = normalize.day_curve([point(0, "2.0"), point(5, "-0.5"), point(10, "-1.0")])
    assert curve[0].peak_pct == Decimal("2.0")
    assert curve[2].equity_pct == Decimal("0.5")
    assert curve[2].drawdown_pct == Decimal("1.5")


def test_empty_day_gives_empty_curve() -> None:
    assert normalize.day_curve([]) == []


# --- правила приёма, которые не видны в чистых функциях ---


async def test_ingest_skips_trades_before_ingest_from() -> None:
    """Сделка, закрытая раньше подключения, не принимается.

    Это то самое «историю не импортируем»: без проверки прошлое просочилось бы
    при первом же полном перечитывании источника.
    """
    import uuid

    from eds.contracts.ingest import IngestContext
    from eds.contracts.source import IncomingTrade
    from eds.modules.trades.service import ingest_batch

    connected_at = utc(2026, 9, 20, 9, 0)
    ctx = IngestContext(
        user_id=uuid.uuid4(),
        source="fake",
        connection_id=uuid.uuid4(),
        account_ids={"acc": uuid.uuid4()},
        violation_tag_ids=frozenset(),
        timezone=MSK,
        day_cutoff=MIDNIGHT,
        significance_pct=Decimal("0.50"),
        ingest_from=connected_at,
    )

    def trade(external_id: str, closed: dt.datetime) -> IncomingTrade:
        return IncomingTrade(
            external_id=external_id,
            account_external_id="acc",
            symbol="BTCUSDT",
            side="long",
            profit_usd=Decimal("-10"),
            account_return_pct=Decimal("-0.6"),
            open_time=closed - dt.timedelta(minutes=5),
            close_time=closed,
        )

    report = await ingest_batch(
        None,  # база не понадобится: обе сделки отбрасываются до обращения к ней
        ctx,
        [
            trade("old", connected_at - dt.timedelta(minutes=1)),
            trade("older", connected_at - dt.timedelta(days=3)),
        ],
    )

    assert report.received == 2
    assert report.inserted == 0
    assert report.skipped_before_ingest_from == 2


async def test_ingest_skips_open_positions() -> None:
    """Открытые позиции в MVP не принимаем (ТЗ 4.5, решение ОВ-3)."""
    import uuid

    from eds.contracts.ingest import IngestContext
    from eds.contracts.source import IncomingTrade
    from eds.modules.trades.service import ingest_batch

    ctx = IngestContext(
        user_id=uuid.uuid4(),
        source="fake",
        connection_id=uuid.uuid4(),
        account_ids={"acc": uuid.uuid4()},
        violation_tag_ids=frozenset(),
        timezone=MSK,
        day_cutoff=MIDNIGHT,
        significance_pct=Decimal("0.50"),
        ingest_from=utc(2026, 9, 1),
    )
    open_trade = IncomingTrade(
        external_id="open-1",
        account_external_id="acc",
        symbol="BTCUSDT",
        side="long",
        profit_usd=Decimal("0"),
        account_return_pct=Decimal("0"),
        open_time=utc(2026, 9, 20, 10, 0),
        close_time=None,
        is_open=True,
    )

    report = await ingest_batch(None, ctx, [open_trade])
    assert report.skipped_open == 1
    assert report.inserted == 0
