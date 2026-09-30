"""Оркестрация уведомлений: доменное событие → текст → очередь.

Разделение из Архитектуры ч.1 §2 держится здесь буквально. Модуль incidents
решает, что случилось, модуль notifications знает, как сообщить, а этот файл —
единственное место, где эти два знания встречаются. Поэтому новое уведомление
добавляется правкой одной таблицы здесь, а не правкой движка.

**Значения подстановок собираются на сервере и здесь.** Время — в таймзоне
трейдера, деньги — со знаком перед долларом и двумя знаками после точки, как
на экране (Архитектура ч.2 §1.4). Если бы их собирал модуль уведомлений,
он знал бы про таймзоны и про формат денег, то есть про предметную область,
которой у него нет.

**Каждый ключ каталога обязан иметь продюсера.** Шаблон, который трейдер видит
в настройках, но который никогда не отправляется, — это обещание, которого
сервис не выполняет. Проверяется тестом: у каждого ключа есть отправитель,
и отправитель даёт значения всем подстановкам своего шаблона.
"""

import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts import events as ev
from eds.modules.incidents import repo as incidents_repo
from eds.modules.incidents import service as incidents_service
from eds.modules.incidents.models import CODE_RULE_FIRED, CODE_VIOLATION
from eds.modules.notifications import service as notify_service
from eds.modules.notifications.telegram.client import confirm_keyboard
from eds.modules.trades import repo as trades_repo
from eds.platform import auth, bus
from eds.platform.db import session_factory

log = logging.getLogger("eds.notify")

# Причины снятия словами. Собирает сервер: эта же строка уходит в Telegram
# и стоит в ленте инцидентов, и собранная в двух местах она разъедётся.
LIFT_REASON = {
    "conditions_met": "условия выполнены",
    "day_boundary": "граница дня",
    "breached": "блокировка нарушена",
}

# Почему день не зачтён — те же коды, что в `streaks.day_marks`.
STREAK_REASON = {
    "violation": "нарушение",
    "breach": "сделка во время блокировки",
    "no_review": "разбор не заполнен",
    "no_check": "чек не пройден",
    "no_entry": "запись дня не заполнена",
    "frozen": "заморозка",
}


def money(value: Any) -> str:
    """Знак перед долларом: «$-84.20» читается как сломанная вёрстка."""
    number = Decimal(str(value or 0))
    sign = "−" if number < 0 else ("+" if number > 0 else "")
    return f"{sign}${abs(number):.2f}"


def hhmm(moment: dt.datetime | str | None, tz: str) -> str:
    when = _moment(moment)
    if when is None:
        return "—"
    return f"{when.astimezone(ZoneInfo(tz)):%H:%M}"


def _moment(raw: dt.datetime | str | None) -> dt.datetime | None:
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw
    try:
        return dt.datetime.fromisoformat(str(raw))
    except ValueError:  # pragma: no cover — в событиях всегда ISO
        return None


def day_text(raw: dt.date | str | None) -> str:
    if raw is None:
        return "—"
    day = raw if isinstance(raw, dt.date) else dt.date.fromisoformat(str(raw))
    return incidents_service.date_text(day)


async def send(
    s: AsyncSession,
    user_id: uuid.UUID,
    key: str,
    values: dict[str, Any],
    *,
    dedup_key: str | None = None,
    keyboard: dict[str, Any] | None = None,
    prefs: auth.UserPrefs | None = None,
) -> None:
    """Поставить уведомление в очередь и сказать об этом в журнал.

    Журнал остаётся, хотя Telegram уже есть: пока бот не привязан, он и есть
    единственное место, где видно текст, и ровно по нему приёмка шага
    проверяется в первый час.
    """
    settings = prefs or await auth.prefs_of(s, user_id)
    row = await notify_service.notify(
        s,
        user_id=user_id,
        key=key,
        values=values,
        prefs=settings,
        dedup_key=dedup_key,
        keyboard=keyboard,
    )
    if row is None:
        # Повтор по dedup_key. Не ошибка: то же событие приходит и из потока,
        # и из сверки, а сообщение должно уйти один раз.
        return
    if row.state == notify_service.SKIPPED:
        log.info("уведомление %s не отправлено — %s: %s", key, row.error, row.body)
        return
    log.info("уведомление %s в очереди: %s", key, row.body)


# --- значения подстановок ---


async def lock_values(
    s: AsyncSession, user_id: uuid.UUID, payload: dict[str, Any], tz: str
) -> dict[str, Any]:
    started = _moment(payload.get("started_at"))
    timer_until = _moment(payload.get("timer_until"))
    window_until = _moment(payload.get("window_until"))
    until = timer_until or window_until
    minutes = 0
    if started is not None and until is not None:
        minutes = max(0, round((until - started).total_seconds() / 60))
    return {
        "rule_name": payload.get("rule_name") or "правило",
        "minutes": minutes,
        "until": hhmm(until, tz),
        "day": day_text(payload.get("day")),
    }


# --- консьюмер шины ---


async def on_event(event: bus.Event) -> None:
    """Доменное событие → уведомление. Свой курсор, как у любого потребителя."""
    raw_user = event.payload.get("user_id")
    if not raw_user:
        return
    user_id = uuid.UUID(str(raw_user))
    async with session_factory()() as s:
        prefs = await auth.prefs_of(s, user_id)
        await _handle(s, user_id, prefs, event)
        await s.commit()


async def _handle(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    event: bus.Event,
) -> None:
    payload = event.payload
    tz = prefs.timezone

    if event.type == ev.INCIDENTS_LOCK_STARTED:
        await send(
            s,
            user_id,
            "lock_started",
            await lock_values(s, user_id, payload, tz),
            dedup_key=f"lock_started:{payload.get('lock_id')}",
            prefs=prefs,
        )
        return

    if event.type == ev.INCIDENTS_LOCK_LIFTED:
        await send(
            s,
            user_id,
            "lock_lifted",
            {
                "rule_name": await _rule_name_of_lock(s, user_id, payload),
                "reason": LIFT_REASON.get(
                    str(payload.get("reason")), str(payload.get("reason") or "")
                ),
                "day": day_text(payload.get("day")),
            },
            dedup_key=f"lock_lifted:{payload.get('lock_id')}",
            prefs=prefs,
        )
        return

    if event.type == ev.INCIDENTS_LOCK_BREACHED:
        await send(
            s,
            user_id,
            "lock_breached",
            {
                "symbol": payload.get("symbol") or "—",
                "open_time": hhmm(payload.get("open_time"), tz),
                "rule_name": await _rule_name_of_lock(s, user_id, payload),
            },
            dedup_key=f"lock_breached:{payload.get('lock_id')}",
            prefs=prefs,
        )
        return

    if event.type == ev.DAYBOOK_ADMISSION_DECIDED:
        if payload.get("verdict") != "denied":
            return
        await send(
            s,
            user_id,
            "no_admission",
            {
                "score": payload.get("score"),
                "min_score": payload.get("min_score"),
                "day": day_text(payload.get("day")),
            },
            dedup_key=f"no_admission:{user_id}:{payload.get('day')}",
            prefs=prefs,
        )
        return

    if event.type == ev.STREAKS_CHANGED:
        await _on_streak(s, user_id, prefs, payload)
        return

    if event.type == ev.INCIDENTS_OPENED:
        await _on_incident(s, user_id, prefs, payload)


async def _on_streak(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    payload: dict[str, Any],
) -> None:
    """Стрик оборвался — а не просто изменился.

    Рост серии уведомления не требует: сервис говорит неприятные вещи,
    и поздравлять за каждый прожитый день — ровно тот шум, из-за которого
    уведомления выключают.
    """
    before = int(payload.get("previous") or 0)
    current = int(payload.get("current") or 0)
    if before <= 0 or current >= before:
        return
    reason = str(payload.get("reason") or "")
    await send(
        s,
        user_id,
        "streak_broken",
        {
            "streak_before": before,
            "reason": STREAK_REASON.get(reason, reason or "день не зачтён"),
            "day": day_text(payload.get("day")),
        },
        dedup_key=f"streak_broken:{user_id}:{payload.get('day')}:{before}",
        prefs=prefs,
    )


async def _on_incident(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    payload: dict[str, Any],
) -> None:
    """Алерт трейдеру и сигнал доверенному лицу по записанному инциденту."""
    incident_id = payload.get("incident_id")
    if not incident_id:
        return
    incident = await incidents_repo.by_id(s, user_id, uuid.UUID(str(incident_id)))
    if incident is None or incident.shadow:
        return
    details = incident.details or {}

    if details.get("alert"):
        await _alert_of_incident(s, user_id, prefs, incident, details)

    if not details.get("buddy"):
        return
    await send(
        s,
        user_id,
        "buddy_signal",
        {
            "trader_name": prefs.trader_name,
            "rule_name": details.get("rule_name")
            or payload.get("rule_name")
            or "правило",
            "day": day_text(incident.day),
        },
        dedup_key=f"buddy_signal:{incident.id}",
        prefs=prefs,
    )


async def _alert_of_incident(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    incident,
    details: dict[str, Any],
) -> None:
    """Какой текст соответствует этому инциденту.

    Развилка одна и стоит здесь, а не в каждом обработчике: у каждого кода
    инцидента свой текст, и сводить их в одно сообщение значило бы врать
    про то, что случилось.
    """
    day = day_text(incident.day)
    rule_name = details.get("rule_name") or "правило"

    if incident.code == CODE_VIOLATION:
        trade = await _trade_of(s, user_id, details)
        await send(
            s,
            user_id,
            "violation_detected",
            {
                "symbol": details.get("symbol")
                or (trade.symbol if trade is not None else "—"),
                "open_time": hhmm(
                    details.get("trade_open_time")
                    or (trade.open_time if trade is not None else None),
                    prefs.timezone,
                ),
                "profit_usd": money(trade.profit_usd if trade is not None else 0),
                "day": day,
            },
            dedup_key=f"violation:{incident.id}",
            prefs=prefs,
        )
        return

    if incident.code == CODE_RULE_FIRED and "not_locked" in details:
        # Блокировка не начиналась: либо у правила её нет, либо она уже шла.
        # Когда блокировка начинается, о ней говорит своё событие, и второе
        # сообщение об одном и том же было бы шумом.
        await send(
            s,
            user_id,
            "rule_fired",
            {"rule_name": rule_name, "day": day},
            dedup_key=f"rule_fired:{incident.id}",
            prefs=prefs,
        )


async def _trade_of(s: AsyncSession, user_id: uuid.UUID, details: dict[str, Any]):
    raw = details.get("trade_id")
    if not raw:
        return None
    return await trades_repo.by_id(s, user_id, uuid.UUID(str(raw)))


async def _rule_name_of_lock(
    s: AsyncSession, user_id: uuid.UUID, payload: dict[str, Any]
) -> str:
    raw = payload.get("rule_name")
    if raw:
        return str(raw)
    lock_id = payload.get("lock_id")
    if not lock_id:
        return "правило"
    lock = await incidents_repo.lock_by_id(s, user_id, uuid.UUID(str(lock_id)))
    return lock.rule_name if lock is not None else "правило"


NOTIFY_TYPES = (
    ev.INCIDENTS_OPENED,
    ev.INCIDENTS_LOCK_STARTED,
    ev.INCIDENTS_LOCK_LIFTED,
    ev.INCIDENTS_LOCK_BREACHED,
    ev.DAYBOOK_ADMISSION_DECIDED,
    ev.STREAKS_CHANGED,
)


def consumer() -> bus.Consumer:
    return bus.Consumer("notify", on_event, types=NOTIFY_TYPES)


# --- просьба подтвердить снятие ---


async def request_buddy_confirm(
    s: AsyncSession,
    user_id: uuid.UUID,
    lock,
    prefs: auth.UserPrefs,
) -> None:
    """Отправить другу просьбу с кнопкой под ней.

    Кнопка, а не команда: друг ничего не набирает руками, а нажатие приходит
    обратно готовым `callback_data` с идентификатором этой блокировки.
    """
    await send(
        s,
        user_id,
        "buddy_confirm_request",
        {"trader_name": prefs.trader_name, "rule_name": lock.rule_name},
        # В ключ входит время просьбы: повторная просьба после cooldown —
        # это новое сообщение, а не дубль первого.
        dedup_key=f"buddy_confirm:{lock.id}:{int(dt.datetime.now(dt.UTC).timestamp())}",
        keyboard=confirm_keyboard(str(lock.id)),
        prefs=prefs,
    )


async def buddy_signal_name(
    s: AsyncSession, user_id: uuid.UUID, incident_id: uuid.UUID
) -> str | None:
    """Кому ушёл сигнал по этому инциденту — или None, если не ушёл.

    Нужно красной полосе на экране блокировки: в прототипе у неё три фразы,
    и третья — «Максиму отправлен сигнал». Сказать это можно, только если
    сигнал правда поставлен в очередь: контакт мог не подтвердить согласие,
    сигнал мог быть выключен у правила, мог быть включён режим наблюдения.
    Обещанная отправка, которой не было, — та же ложь, что заглушка.
    """
    from eds.modules.notifications import repo as notify_repo
    from eds.modules.notifications.models import SELF, SKIPPED

    row = await notify_repo.by_dedup(s, f"buddy_signal:{incident_id}")
    if row is None or row.channel == SELF or row.state == SKIPPED:
        return None
    contact = await notify_repo.contact_of(s, user_id)
    if contact is None:
        return None
    return contact.display_name or contact.handle


# --- надзор по расписанию ---


async def watch(
    s: AsyncSession, user_id: uuid.UUID, prefs: auth.UserPrefs, now: dt.datetime
) -> None:
    """То, о чём сервис обязан сказать сам, без события в шине.

    Два повода, и оба — из ТЗ: неразмеченные сделки дольше положенного
    (SR-4, 6.5) и молчащий синк при открытой сессии (9.6). Раньше они
    появлялись только на экране «Сегодня», то есть когда трейдер уже смотрел
    в сервис. Смысл у них ровно обратный: сказать тому, кто не смотрит.
    """
    from eds.app import system_rules
    from eds.app import today as app_today
    from eds.contracts.trading_time import trading_day
    from eds.modules.daybook import repo as daybook_repo
    from eds.modules.source import repo as source_repo

    day = trading_day(now, prefs.timezone, prefs.day_cutoff)
    day_row = await daybook_repo.day_of(s, user_id, day)
    session_open = bool(
        day_row is not None
        and day_row.session_opened_at is not None
        and day_row.session_closed_at is None
    )

    overdue = await system_rules.sr4_unmarked(s, user_id, day, now=now)
    fresh = [item for item in overdue if item.get("fresh")]
    if fresh:
        await send(
            s,
            user_id,
            "unmarked_reminder",
            {"count": len(fresh), "minutes": fresh[0].get("minutes")},
            dedup_key=f"unmarked:{fresh[0]['trade_id']}",
            prefs=prefs,
        )

    if not session_open:
        # Вне сессии тишина источника ничего не значит: сделок нет, потому что
        # трейдер не торгует. Алерт здесь был бы шумом, который выключают.
        return
    connection = await source_repo.active_connection(s, user_id)
    source = await app_today._source_block(s, connection)
    idle = app_today._idle_alert(source, session_open)
    if idle is None:
        return
    minutes = int(idle.get("count") or 0)
    last = source.get("last_event_at")
    await send(
        s,
        user_id,
        "sync_lost",
        {"minutes": minutes},
        # Одно сообщение на один провал связи, а не каждую минуту молчания:
        # ключ собран из момента последнего контакта, и пока он не изменился,
        # это тот же самый провал.
        dedup_key=f"sync_lost:{user_id}:{last}",
        prefs=prefs,
    )
