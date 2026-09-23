"""Главный экран одним запросом: состояние дня, допуск, счётчики, источник.

Живёт в оркестрации, потому что собирает четыре модуля: настройки трейдера,
торговый день, сделки и источник. Ни один модуль не может собрать это сам,
не узнав о чужих схемах.
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import engine
from eds.app import streaks as app_streaks
from eds.contracts.trading_time import day_ends_at, trading_day
from eds.modules.daybook import periods
from eds.modules.daybook import repo as daybook_repo
from eds.modules.daybook import service as daybook
from eds.modules.incidents import service as incidents
from eds.modules.source import repo as source_repo
from eds.modules.streaks import service as streaks_service
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
    pending_day = await daybook.pending_review(s, user_id, day)
    entry_row = await daybook_repo.entry_of(s, user_id, periods.DAY, day)

    # Блокировка, правила у границы и счётчики движка. Здесь же ленивое
    # снятие: процесса границы дня пока нет, а блокировка, у которой все
    # условия выполнены, обязана сняться без перезагрузки чужими руками.
    live = await engine.today_block(s, user_id, prefs, day, now=now)
    counters = _counters_block(
        await trades_service.day_counters(s, user_id, day), live["counters"]
    )

    # Стрик пересчитываем на чтении главной страницы: отдельного процесса,
    # который делал бы это ночью, пока нет, а показывать вчерашнюю серию
    # сегодня — то же самое, что показывать неверную.
    await app_streaks.refresh(s, user_id, prefs, today=day)

    streak = await streaks_service.state_out(s, user_id, day)

    # Вторая фраза красной полосы на экране блокировки: что стало со стриком.
    # Собирается здесь, а не в модуле incidents: про стрик знает другой модуль,
    # и склеить два факта в одну фразу может только оркестрация.
    lock_block = live["lock"]
    if lock_block is not None and lock_block.get("breach"):
        lock_block["breach"]["streak_text"] = incidents.streak_burned_text(
            streak["current"]
        )

    state = daybook.state_of(
        day_row,
        has_source=connection is not None,
        pending_review_day=pending_day,
        lock_active=live["lock"] is not None,
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
        "lock": live["lock"],
        # Блок «Ближе всего к срабатыванию» (Дизайн Э-04, блок 4). В контракте
        # ч.2 §3.5 его нет — блок появился в прототипе, — но собирается он там же,
        # где весь экран: одним запросом, и считается целиком на сервере.
        "near_rules": live["near_rules"],
        # Блок «Инциденты сегодня» из прототипа Main.dc.html. Строку собирает
        # тот же сборщик, что и ленту раздела «Инциденты»: одно событие
        # должно быть описано одинаково в обоих местах.
        "incidents": live["incidents"],
        "streak": streak,
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
        "attention": await _attention(
            counters, connection, live["unmarked_overdue"]
        ),
        "thresholds": {
            "pass_score": prefs.pass_score,
            "min_score": prefs.min_score,
        },
    }


def _counters_block(marking: dict, counters) -> dict:
    """Счётчики дня из двух половин: разметка из trades, показатели из движка.

    Две половины, потому что считают их разные модули и считают по-разному:
    разметка это запросы с группировкой, показатели правил — проход по сделкам
    в порядке закрытия. Складывает их оркестрация, а не один из модулей.
    """
    return {
        **marking,
        "all_trades": counters.all_trades,
        "significant_trades": counters.significant_trades,
        "loss_streak": counters.loss_streak,
        "equity_pct": trades_service.quantize_pct(counters.equity_pct),
        "peak_pct": trades_service.quantize_pct(counters.peak_pct),
        "drawdown_pct": trades_service.quantize_pct(counters.drawdown_pct),
        "loss_sum_pct": trades_service.quantize_pct(counters.loss_sum_pct),
        # Источник без открытых позиций их не даёт. null, а не ноль: ноль
        # значил бы «позиций нет», а это другое (Архитектура ч.2 §3.5).
        "unrealized_pct": trades_service.quantize_pct(counters.unrealized_pct),
        "drawdown_full_pct": trades_service.quantize_pct(counters.drawdown_full_pct),
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


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русские окончания. Текст сообщения собирает сервер (Архитектура ч.2 §1.3),
    значит и согласование числа — его работа, а не фронта."""
    rest = abs(n) % 100
    if 10 < rest < 20:
        return many
    last = rest % 10
    if last == 1:
        return one
    if 1 < last < 5:
        return few
    return many


async def _attention(
    counters: dict, connection, unmarked_overdue: list[dict] | None = None
) -> list[dict]:
    """Что требует действия. Один список вместо набора булевых полей."""
    out: list[dict] = []
    unmarked = counters.get("unmarked", 0)
    overdue = len(unmarked_overdue or [])
    if unmarked:
        out.append(
            {
                "code": "unmarked_trades",
                "count": unmarked,
                "message": f"{unmarked} {_plural(unmarked, 'сделка', 'сделки', 'сделок')}"
                " без разметки за сегодня.",
            }
        )
    if overdue:
        # SR-4. Отдельной строкой, а не приписью к предыдущей: «есть
        # неразмеченные» и «висят дольше положенного» — разные новости, и
        # вторая требует действия прямо сейчас (ТЗ 6.5).
        waits = _plural(overdue, "сделка ждёт", "сделки ждут", "сделок ждут")
        out.append(
            {
                "code": "unmarked_overdue",
                "count": overdue,
                "message": f"{overdue} {waits} разметки дольше положенного.",
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
