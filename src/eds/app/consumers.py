"""Консьюмеры шины, которым нужно знать больше одного модуля.

Шина здесь не украшение: изменение словаря тегов происходит в source,
а переразметка сделок — в trades, и связывает их событие, а не прямой вызов.
"""

import datetime as dt
import logging
import uuid

from eds.contracts import events as ev
from eds.contracts.trading_time import trading_day
from eds.modules.source import repo as source_repo
from eds.modules.trades import service as trades_service
from eds.platform import auth, bus
from eds.platform.db import session_factory

log = logging.getLogger("eds.consumers")


async def on_tag_dictionary_changed(event: bus.Event) -> None:
    """Трейдер изменил, какие теги считаются нарушением → переразметить сделки."""
    from eds.app.pipeline import build_context

    user_id = uuid.UUID(event.payload["user_id"])
    async with session_factory()() as s:
        connection = await source_repo.active_connection(s, user_id)
        if connection is None:
            return
        ctx = await build_context(s, user_id, connection)
        changed = await trades_service.remark_all(s, ctx)
        await s.commit()
    log.info("словарь тегов изменён: переразмечено сделок — %s", changed)


def all_consumers() -> list[bus.Consumer]:
    return [
        bus.Consumer(
            "remark_on_tags",
            on_tag_dictionary_changed,
            types=(ev.SOURCE_TAG_DICTIONARY_CHANGED,),
        ),
        bus.Consumer(
            "streak_on_day_change",
            on_day_changed,
            types=(
                ev.TRADES_MARKING_CHANGED,
                ev.DAYBOOK_DAY_CLOSED,
                ev.DAYBOOK_REVIEW_COMPLETED,
            ),
        ),
    ]


async def on_day_changed(event: bus.Event) -> None:
    """Что-то изменилось в прошедшем дне → пересчитать стрик.

    Подписки узкие: разметка, закрытие дня и разбор. На приём каждой сделки
    не подписываемся сознательно — сверка приносит их сотнями, а пересчёт
    за шестьдесят дней на каждую сделку был бы нагрузкой ни за что; текущий
    день в стрике всё равно не участвует, и к его концу всё пересчитается.
    """
    from eds.app import streaks as app_streaks

    user_id = uuid.UUID(event.payload["user_id"])
    async with session_factory()() as s:
        prefs = await auth.prefs_of(s, user_id)
        today = trading_day(
            dt.datetime.now(dt.UTC), prefs.timezone, prefs.day_cutoff
        )
        state = await app_streaks.refresh(s, user_id, prefs, today=today)
        await s.commit()
    log.info("стрик пересчитан по событию %s: сейчас %s", event.type, state.current)
