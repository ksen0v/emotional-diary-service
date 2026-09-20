"""Каталог событий. Единственное, что модулям разрешено импортировать друг о друге.

Полное описание — Архитектура ч.1 §3. Здесь только имена и формы,
без логики: модуль-продюсер публикует событие, модуль-консьюмер его читает,
и ни один не знает о таблицах другого.
"""

from typing import Final

# --- source ---
SOURCE_STREAM_LOST: Final = "source.stream_lost"
SOURCE_TAG_DICTIONARY_CHANGED: Final = "source.tag_dictionary_changed"

# --- trades ---
TRADES_INGESTED: Final = "trades.ingested"
TRADES_MARKING_CHANGED: Final = "trades.marking_changed"

# --- daybook ---
DAYBOOK_ADMISSION_DECIDED: Final = "daybook.admission_decided"
DAYBOOK_SESSION_OPENED: Final = "daybook.session_opened"
DAYBOOK_DAY_CLOSED: Final = "daybook.day_closed"
DAYBOOK_REVIEW_COMPLETED: Final = "daybook.review_completed"

# --- rules ---
RULES_FIRED: Final = "rules.fired"

# --- incidents ---
INCIDENTS_OPENED: Final = "incidents.opened"
INCIDENTS_LOCK_STARTED: Final = "incidents.lock_started"
INCIDENTS_LOCK_LIFTED: Final = "incidents.lock_lifted"
INCIDENTS_LOCK_BREACHED: Final = "incidents.lock_breached"
INCIDENTS_RESOLVED_RETRO: Final = "incidents.resolved_retro"

# --- streaks ---
STREAKS_CHANGED: Final = "streaks.changed"

# --- служебное, только для шага 0 ---
PLATFORM_TEST_PING: Final = "platform.test_ping"

ALL: Final = (
    SOURCE_STREAM_LOST,
    SOURCE_TAG_DICTIONARY_CHANGED,
    TRADES_INGESTED,
    TRADES_MARKING_CHANGED,
    DAYBOOK_ADMISSION_DECIDED,
    DAYBOOK_SESSION_OPENED,
    DAYBOOK_DAY_CLOSED,
    DAYBOOK_REVIEW_COMPLETED,
    RULES_FIRED,
    INCIDENTS_OPENED,
    INCIDENTS_LOCK_STARTED,
    INCIDENTS_LOCK_LIFTED,
    INCIDENTS_LOCK_BREACHED,
    INCIDENTS_RESOLVED_RETRO,
    STREAKS_CHANGED,
    PLATFORM_TEST_PING,
)
