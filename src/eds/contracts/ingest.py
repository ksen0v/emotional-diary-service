"""Что нужно знать приёмнику сделок, чтобы не читать чужие схемы.

Модуль trades не обращается ни к настройкам пользователя, ни к словарю тегов:
всё это собирает оркестратор (eds.app) и передаёт сюда. Так граница между
модулями остаётся настоящей, а не нарисованной.
"""

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class IngestContext:
    user_id: uuid.UUID
    source: str
    connection_id: uuid.UUID
    account_ids: dict[str, uuid.UUID]  # внешний id счёта → наш id
    violation_tag_ids: frozenset[str]
    timezone: str
    day_cutoff: dt.time
    significance_pct: Decimal
    ingest_from: dt.datetime


@dataclass
class IngestReport:
    """Итог приёма порции. Числа нужны и в ответе API, и в логе сверки."""

    received: int = 0
    inserted: int = 0
    remarked: int = 0
    unchanged: int = 0
    skipped_before_ingest_from: int = 0
    skipped_open: int = 0
    skipped_unknown_account: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "inserted": self.inserted,
            "remarked": self.remarked,
            "unchanged": self.unchanged,
            "skipped_before_ingest_from": self.skipped_before_ingest_from,
            "skipped_open": self.skipped_open,
            "skipped_unknown_account": self.skipped_unknown_account,
        }
