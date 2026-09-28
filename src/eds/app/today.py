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

    # Считается один раз: блок источника нужен и экрану, и списку «что требует
    # действия», а спрашивать состояние потока дважды значило бы однажды
    # показать в двух местах разное.
    source = await _source_block(s, connection)

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
        "source": source,
        "attention": await _attention(
            counters,
            connection,
            live["unmarked_overdue"],
            source,
            session_open=bool(
                day_row
                and day_row.session_opened_at
                and not day_row.session_closed_at
            ),
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
    stream = _stream_block(connection)
    contact = _last_contact(run, stream)
    return {
        "provider": connection.provider,
        "sync_state": connection.state,
        "account": next(
            (a.name for a in await source_repo.accounts_of(s, connection.id)), None
        ),
        "last_event_at": run.started_at if run else None,
        # Поток к бирже отдельно от сверки, потому что это разные вопросы.
        # Раньше экран знал только «когда была последняя сверка», и поток,
        # не поднявшийся ни разу, выглядел на нём точно так же, как рабочий.
        "stream": stream,
        "idle_sec": (
            None
            if contact is None
            else int((dt.datetime.now(dt.UTC) - contact).total_seconds())
        ),
        # «Устарел» — это ошибка подключения или поток, который должен быть
        # поднят и не поднят. Молчание живого потока устареванием не считается:
        # трейдер не торгует непрерывно, и тишина при подключённом сокете —
        # нормальное состояние, а не сбой. Ровно на этом различии сервис
        # однажды уже соврал сам себе и рвал здоровое соединение.
        "stale": connection.state == "error"
        or bool(stream and stream["expected"] and not stream["connected"]),
        "capabilities": dict(connection.capabilities),
    }


def _stream_block(connection) -> dict | None:
    """Что сейчас с потоком событий от биржи."""
    from eds.app import stream as app_stream

    row = app_stream.registry.state_of(connection.id)
    # Возможность источника, а не `if provider == 'binance'` (контракт
    # `SourceCapabilities`). Поднятый поток тоже считается: у подключений,
    # созданных до появления этого флага, в capabilities его нет, и без второй
    # половины условия их статус пришлось бы «оживлять» пересохранением ключа.
    expected = bool(dict(connection.capabilities).get("provides_stream")) or (
        row is not None
    )
    if not expected:
        return None
    if row is None:
        return {
            "expected": True,
            "connected": False,
            "opened_at": None,
            "last_event_at": None,
            "reconnects": 0,
            "last_error": "поток ещё не поднят",
        }
    return {
        "expected": True,
        "connected": bool(row["connected"]),
        "opened_at": row["opened_at"],
        "last_event_at": row["last_event_at"],
        "reconnects": row["reconnects"],
        "last_error": row["last_error"],
    }


def _last_contact(run, stream: dict | None) -> dt.datetime | None:
    """Когда сервис в последний раз что-то знал об источнике.

    Самое свежее из трёх: сверка, открытие потока, событие из потока. Считать
    простой только по сверке значило бы объявлять простоем те минуты, когда
    поток открыт и всё в порядке.
    """
    times = [_aware(run.started_at) if run else None]
    if stream is not None:
        times.append(_parsed(stream["opened_at"]) if stream["connected"] else None)
        times.append(_parsed(stream["last_event_at"]))
    known = [t for t in times if t is not None]
    return max(known) if known else None


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _parsed(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return _aware(dt.datetime.fromisoformat(value))
    except ValueError:
        return None


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
    counters: dict,
    connection,
    unmarked_overdue: list[dict] | None = None,
    source: dict | None = None,
    session_open: bool = False,
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
    idle = _idle_alert(source, session_open)
    if idle is not None:
        out.append(idle)
    return out


# Простой синка дольше пяти минут при открытой сессии — алерт (ТЗ 9.6).
IDLE_ALERT_SEC = 5 * 60


def _idle_alert(source: dict | None, session_open: bool) -> dict | None:
    """Сервис давно ничего не знает об источнике, а трейдер торгует.

    Только при открытой сессии: вне сессии тишина ничего не значит, и алерт
    был бы шумом. Живой поток простоем не считается — его «тишина» означает,
    что сделок нет, а не что связи нет; иначе алерт горел бы весь день
    у любого, кто торгует не каждую минуту.
    """
    if source is None or not session_open:
        return None
    stream = source.get("stream")
    if stream and stream["connected"]:
        return None
    idle = source.get("idle_sec")
    if idle is None or idle < IDLE_ALERT_SEC:
        return None
    return {
        "code": "source_idle",
        "count": idle // 60,
        "message": (
            f"Сервис не получал сделок {idle // 60} "
            f"{_plural(idle // 60, 'минуту', 'минуты', 'минут')}. "
            "Пока связи нет, правила считать нечем — проверь подключение."
        ),
    }
