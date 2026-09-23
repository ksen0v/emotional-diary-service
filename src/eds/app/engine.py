"""Движок в работе: сделки → счётчики → правила → инциденты и блокировки.

Живёт в оркестрации, потому что соединяет четыре модуля: сделки из `trades`,
правила и счётчики из `rules`, блокировки из `incidents`, настройки трейдера
через платформу. Ни один модуль не может сделать это сам, не узнав о чужих
схемах — и в этом смысл шва.

Вызывается синхронно после приёма порции сделок (ТЗ 9.7: «движок правил —
модуль внутри воркера, синхронный вызов после батча сделок») и лениво на
чтении главного экрана, чтобы блокировка снималась и без фонового процесса.
"""

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import system_rules
from eds.contracts.rules import OpenTrade, TradeFact
from eds.contracts.trading_time import day_ends_at, trading_day
from eds.modules.incidents import repo as incidents_repo
from eds.modules.incidents import service as incidents
from eds.modules.rules import engine as rules_engine
from eds.modules.rules import service as rules_service
from eds.modules.trades import repo as trades_repo
from eds.platform import auth

log = logging.getLogger("eds.engine")


@dataclass
class EngineReport:
    """Что движок сделал за проход. Числа уходят в ответ сверки и в лог."""

    days: int = 0
    fired: int = 0
    locks_started: int = 0
    breaches: int = 0
    shadow: bool = False
    # Системные триггеры считаются отдельно от пользовательских правил:
    # «сработало 3» без разбивки не отвечает на вопрос, что именно сработало,
    # а на приёмке спрашивают именно это.
    system_fired: int = 0

    def as_dict(self) -> dict:
        return {
            "days": self.days,
            "fired": self.fired,
            "locks_started": self.locks_started,
            "breaches": self.breaches,
            "shadow": self.shadow,
            "system_fired": self.system_fired,
        }


async def facts_of_day(
    s: AsyncSession, user_id: uuid.UUID, day: dt.date
) -> list[TradeFact]:
    rows = await trades_repo.day_facts(s, user_id, day)
    return [
        TradeFact(
            trade_id=row[0],
            open_time=row[1],
            close_time=row[2],
            account_return_pct=row[3],
            profit_usd=row[4],
            is_significant=row[5],
        )
        for row in rows
    ]


async def run_day(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    day: dt.date,
    *,
    now: dt.datetime | None = None,
    report: EngineReport | None = None,
) -> rules_engine.Counters:
    """Пересчитать день и применить то, что из него следует."""
    moment = now or dt.datetime.now(dt.UTC)
    out = report or EngineReport()
    out.days += 1
    out.shadow = prefs.shadow_mode

    facts = await facts_of_day(s, user_id, day)
    counters, firings = await rules_service.run_day(
        s, user_id, day, facts, prefs.significance_pct
    )

    window_until = day_ends_at(day, prefs.timezone, prefs.day_cutoff)
    for firing in firings:
        out.fired += 1
        _incident, lock = await incidents.open_from_firing(
            s,
            user_id,
            firing,
            window_until=window_until,
            shadow=prefs.shadow_mode,
            now=moment,
        )
        if lock is not None:
            out.locks_started += 1
            log.info(
                "блокировка %s: правило «%s», до %s",
                lock.id,
                lock.rule_name,
                lock.timer_until or lock.window_until,
            )

    # Системные триггеры — после пользовательских правил и до compliance.
    # Порядок не косметический: SR-1 сам включает блокировку, и compliance
    # обязан считать её уже идущей, иначе сделка, открытая после тега,
    # проскочила бы мимо SR-2 до следующего прохода.
    locks = await system_rules.sr1_violations(s, user_id, prefs, day, now=moment)
    out.system_fired += len(locks)
    out.locks_started += len(locks)
    if await system_rules.sr3_no_admission(s, user_id, prefs, day, now=moment):
        out.system_fired += 1

    # Compliance идёт после срабатываний: сделка, из-за которой блокировка
    # включилась, открыта до неё и нарушением быть не может.
    if await refresh_lock(s, user_id, prefs, moment):
        out.breaches += 1
    return counters


async def refresh_lock(
    s: AsyncSession, user_id: uuid.UUID, prefs: auth.UserPrefs, now: dt.datetime
) -> bool:
    """Проверить активную блокировку: нарушена ли и не пора ли её снять.

    Окно соблюдения — от начала блокировки до текущего момента, а не до
    таймера: пока условия снятия не выполнены, блокировка идёт, и сделка
    внутри неё остаётся нарушением. Формула Архитектуры ч.1 §7 обрывала окно
    на таймере, и сделка, открытая между истёкшим таймером и незаполненным
    разбором, ускользала бы.
    """
    lock = await incidents_repo.active_lock(s, user_id)
    if lock is None:
        return False

    until = min(now, lock.window_until)
    rows = await trades_repo.opened_between(s, user_id, lock.started_at, until)
    trades = [OpenTrade(trade_id=r[0], symbol=r[1], open_time=r[2]) for r in rows]

    # Две записи об одном событии, и обе нужны. Первая — исход нарушенной
    # блокировки: «чем кончился тот инцидент». Вторая — SR-2: «что трейдер
    # сделал». В прототипе `Incidents.dc.html` это две строки, и вторая
    # тяжелее первой.
    breached = await incidents.record_breach(s, user_id, lock, trades, now)
    await system_rules.sr2_breaches(s, user_id, prefs, lock, trades, now=now)

    await incidents.settle(s, user_id, lock, now)
    return breached or bool(trades)


async def after_ingest(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    days: list[dt.date],
    *,
    now: dt.datetime | None = None,
) -> EngineReport:
    """Проход по дням, которых коснулась порция сделок.

    Дни, а не «сегодня»: сверка после переподключения может принести вчерашние
    сделки, и счётчики того дня обязаны сойтись. Блокировку прошлый день уже
    не включит — его окно закрыто границей дня, — но инцидент запишет.
    """
    report = EngineReport()
    moment = now or dt.datetime.now(dt.UTC)
    for day in sorted(set(days)):
        await run_day(s, user_id, prefs, day, now=moment, report=report)
    return report


async def today_block(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    day: dt.date,
    *,
    now: dt.datetime | None = None,
) -> dict:
    """Блокировка и «ближе всего к срабатыванию» для главного экрана.

    Здесь же ленивое снятие: отдельного процесса границы дня пока нет, а
    блокировка, которая висит после выполненных условий, — худшее, что может
    сделать такой сервис.
    """
    moment = now or dt.datetime.now(dt.UTC)
    await refresh_lock(s, user_id, prefs, moment)

    # SR-4 живёт здесь же: отдельного процесса, который будил бы напоминание
    # раз в минуту, пока нет (он появится вместе с уведомлениями), а «Сегодня»
    # читается постоянно. Повторный алерт по той же сделке отсекает журнал
    # проверок, поэтому частое чтение экрана ничего не рассылает.
    unmarked_overdue = await system_rules.sr4_unmarked(s, user_id, day, now=moment)

    counters = await rules_service.counters_out(s, user_id, day)
    near = await rules_service.near(s, user_id, counters)
    today_incidents = await incidents.day_feed(s, user_id, day, prefs.timezone)

    block = {
        "lock": None,
        "near_rules": near,
        "counters": counters,
        "incidents": today_incidents,
        "unmarked_overdue": unmarked_overdue,
    }

    lock = await incidents_repo.active_lock(s, user_id)
    if lock is None or lock.state != "active":
        return block

    review = await incidents_repo.review_of(s, lock.id)
    incident = await incidents_repo.by_id(s, user_id, lock.incident_id)
    block["lock"] = incidents.lock_out(
        lock, review is not None, moment, incidents.breach_of(incident)
    )
    return block


def day_of(prefs: auth.UserPrefs, moment: dt.datetime) -> dt.date:
    return trading_day(moment, prefs.timezone, prefs.day_cutoff)
