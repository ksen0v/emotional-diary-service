"""Консьюмеры шины, которым нужно знать больше одного модуля.

Шина здесь не украшение: изменение словаря тегов происходит в source,
а переразметка сделок — в trades, и связывает их событие, а не прямой вызов.
"""

import logging
import uuid

from eds.contracts import events as ev
from eds.modules.source import repo as source_repo
from eds.modules.trades import service as trades_service
from eds.platform import bus
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
    ]
