"""Каталог событий. Единственное, что модулям разрешено импортировать друг о друге.

Полное описание — Архитектура ч.1 §3. Здесь только имена и формы,
без логики: модуль-продюсер публикует событие, модуль-консьюмер его читает,
и ни один не знает о таблицах другого.
"""

from typing import Final

# --- source ---
SOURCE_STREAM_LOST: Final = "source.stream_lost"
SOURCE_TAG_DICTIONARY_CHANGED: Final = "source.tag_dictionary_changed"
# Подключение появилось, сменило ключ, стало активным или исчезло. Нужно, чтобы
# поток к бирже поднимался в тот же момент, а не в следующий обход расписания:
# первая живая проверка Binance показала, что минута ожидания как раз и съедает
# сделку, которой трейдер проверяет, работает ли сервис.
SOURCE_CONNECTION_CHANGED: Final = "source.connection_changed"

# --- trades ---
TRADES_INGESTED: Final = "trades.ingested"
TRADES_MARKING_CHANGED: Final = "trades.marking_changed"
# Открытая позиция появилась в ленте. Отдельное событие, а не `ingested`:
# у него другой смысл и другие слушатели. `ingested` означает «сделка попала
# в счётчики дня», и открытая в них не попадает (ТЗ 4.5).
TRADES_OPENED: Final = "trades.opened"

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

# --- notifications ---
# Трейдер отправил боту код: аккаунт привязан. Нужно экрану настроек — он
# узнаёт об этом живым событием, а не опросом.
NOTIFY_TELEGRAM_LINKED: Final = "notify.telegram_linked"
# Доверенное лицо нажало «Подтверждаю» — двойное согласие получено (ТЗ 6.8).
NOTIFY_CONTACT_CONFIRMED: Final = "notify.contact_confirmed"

# --- служебное, только для шага 0 ---
PLATFORM_TEST_PING: Final = "platform.test_ping"

ALL: Final = (
    SOURCE_STREAM_LOST,
    SOURCE_TAG_DICTIONARY_CHANGED,
    SOURCE_CONNECTION_CHANGED,
    TRADES_INGESTED,
    TRADES_MARKING_CHANGED,
    TRADES_OPENED,
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
    NOTIFY_TELEGRAM_LINKED,
    NOTIFY_CONTACT_CONFIRMED,
    PLATFORM_TEST_PING,
)
