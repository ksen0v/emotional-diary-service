"""Главный экран одним запросом: состояние дня, допуск, счётчики, источник.

Живёт в оркестрации, потому что собирает четыре модуля: настройки трейдера,
торговый день, сделки и источник. Ни один модуль не может собрать это сам,
не узнав о чужих схемах.
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.trading_time import day_ends_at, trading_day
from eds.modules.daybook import periods
from eds.modules.daybook import repo as daybook_repo
from eds.modules.daybook import service as daybook
from eds.modules.source import repo as source_repo
from eds.modules.trades import repo as trades_repo
from eds.modules.trades import service as trades_service
from eds.platform import auth


def today_of(prefs: auth.UserPrefs, now: dt.datetime | None = None) -> dt.date:
    return trading_day(
        now or dt.datetime.now(dt.UTC), prefs.timezone, prefs.day_cutoff
    )


async def build(
    s: AsyncSession, user_id: uuid.UUID, prefs: auth.UserPrefs
) -> dict:
    now = dt.datetime.now(dt.UTC)
    day = today_of(prefs, now)

    # Сначала закрываем то, что кончилось: иначе вчерашняя сессия осталась бы
    # открытой, и трейдер увидел бы «сессия идёт» на дне, которого уже нет.
    await daybook.close_finished_days(
        s, user_id, day, timezone=prefs.timezone, cutoff=prefs.day_cutoff
    )

    connection = await source_repo.active_connection(s, user_id)
    day_row = await daybook_repo.day_of(s, user_id, day)
    check_row = await daybook_repo.check_of(s, user_id, day)
    counters = await trades_service.day_counters(s, user_id, day)
    pending_day = await daybook.pending_review(s, user_id, day)
    entry_row = await daybook_repo.entry_of(s, user_id, periods.DAY, day)

    state = daybook.state_of(
        day_row, has_source=connection is not None, pending_review_day=pending_day
    )

    return {
        "day": day,
        "server_time": now,
        "day_ends_at": day_ends_at(day, prefs.timezone, prefs.day_cutoff),
        "state": state,
        "shadow_mode": prefs.shadow_mode,
        "admission": daybook.admission_out(day_row, check_row),
        "session": {
            "opened_at": day_row.session_opened_at if day_row else None,
            "closed_at": day_row.session_closed_at if day_row else None,
        },
        # Блокировки — шаг 9, стрик — шаг 7. null здесь означает «этого ещё
        # нет в сервисе», и фронт по нему показывает заглушку вместо пустого
        # блока с нулями.
        "lock": None,
        "streak": None,
        "entry": (
            None
            if entry_row is None
            else daybook.entry_out(
                entry_row,
                (await daybook_repo.tags_of(s, [entry_row.id])).get(entry_row.id, []),
                (await daybook_repo.comments_of(s, [entry_row.id])).get(entry_row.id, []),
            )
        ),
        "review": {
            "state": day_row.review_state if day_row else "none",
            # Разбор за прошедший день не даёт пройти новый чек (ТЗ 5.4).
            # Показываем именно этот день: «заполни разбор» без даты — задача
            # без адреса, особенно если пропущено больше одного дня.
            "pending_day": pending_day.isoformat() if pending_day else None,
            "required_for_next_session": pending_day is not None,
        },
        "counters": counters,
        # Итог вчерашнего дня. Нужен экрану до чека: перед тем как открывать
        # сессию, полезно увидеть, чем кончился прошлый день, — особенно
        # заполнен ли разбор.
        "yesterday": await _yesterday(s, user_id, day),
        "source": await _source_block(s, connection),
        "attention": await _attention(counters, connection),
        "thresholds": {
            "pass_score": prefs.pass_score,
            "min_score": prefs.min_score,
        },
    }


async def _yesterday(s: AsyncSession, user_id: uuid.UUID, today_day: dt.date) -> dict:
    day = today_day - dt.timedelta(days=1)
    summary = (await trades_repo.day_summaries(s, user_id, day, day)).get(day)
    row = await daybook_repo.day_of(s, user_id, day)
    return {
        "day": day.isoformat(),
        "trades": summary["trades"] if summary else 0,
        "violations": summary["violations"] if summary else 0,
        "profit_usd": str(
            trades_service.quantize_money(
                summary["profit_usd"] if summary else Decimal("0")
            )
        ),
        "admission": row.admission if row else None,
        "review_state": row.review_state if row else "none",
    }


async def _source_block(s: AsyncSession, connection) -> dict | None:
    if connection is None:
        return None
    run = await source_repo.last_reconcile_run(s, connection.id)
    return {
        "provider": connection.provider,
        "sync_state": connection.state,
        "account": next(
            (a.name for a in await source_repo.accounts_of(s, connection.id)), None
        ),
        "last_event_at": run.started_at if run else None,
        # «Устарел» пока означает только ошибку подключения. Считать устаревшим
        # молчание дольше N минут можно будет, когда появится автоматическая
        # сверка по расписанию: сейчас сверка идёт по кнопке, и любое молчание
        # было бы ложной тревогой.
        "stale": connection.state == "error",
        "capabilities": dict(connection.capabilities),
    }


async def _attention(counters: dict, connection) -> list[dict]:
    """Что требует действия. Один список вместо набора булевых полей."""
    out: list[dict] = []
    unmarked = counters.get("unmarked", 0)
    if unmarked:
        out.append(
            {
                "code": "unmarked_trades",
                "count": unmarked,
                "message": f"{unmarked} сделок без разметки за сегодня.",
            }
        )
    if connection is not None and connection.state == "error":
        out.append(
            {
                "code": "source_error",
                "count": 1,
                "message": connection.last_error
                or "Источник сделок не отвечает. Пока он молчит, защиты нет.",
            }
        )
    return out
