"""Фейковый источник сделок.

Существует не ради тестов, а ради самой разработки: сценарий «два значимых стопа
подряд, потом поздний тег на первом» из реальной торговли пришлось бы ждать днями,
а воспроизвести точно — никогда. Здесь он воспроизводится за секунду.

Ведёт себя как настоящий источник: сделки лежат в таблице source.fake_feed
и отдаются по запросу окном по времени закрытия, а не выдаются из памяти.
Поэтому перезапуск процесса на него не влияет.
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.source import (
    IncomingAccount,
    IncomingTag,
    IncomingTrade,
    SourceCapabilities,
)
from eds.modules.source.models import FakeFeedItem

ACCOUNT_EXTERNAL_ID = "fake-account-1"
ACCOUNT_NAME = "Тестовый счёт"


class FakeSource:
    """Реализация порта TradeSource поверх таблицы source.fake_feed."""

    provider = "fake"

    def __init__(self, session: AsyncSession, connection_id: uuid.UUID):
        self._s = session
        self._connection_id = connection_id

    def capabilities(self) -> SourceCapabilities:
        # Фейк изображает TMM: готовые сделки и теги, без позиций и баланса.
        return SourceCapabilities(
            provides_trades=True,
            provides_tags=True,
            provides_positions=False,
            provides_balance=False,
            needs_aggregation=False,
            history_depth="full",
        )

    async def fetch_accounts(self) -> list[IncomingAccount]:
        return [
            IncomingAccount(
                external_id=ACCOUNT_EXTERNAL_ID,
                name=ACCOUNT_NAME,
                exchange="fake",
                market="futures",
            )
        ]

    async def fetch_trades(
        self, since: dt.datetime, until: dt.datetime | None = None
    ) -> list[IncomingTrade]:
        query = select(FakeFeedItem).where(
            FakeFeedItem.connection_id == self._connection_id,
            FakeFeedItem.close_time >= since,
        )
        if until is not None:
            query = query.where(FakeFeedItem.close_time <= until)
        rows = await self._s.execute(query.order_by(FakeFeedItem.close_time))
        return [_from_payload(row.payload) for row in rows.scalars()]


def _from_payload(payload: dict) -> IncomingTrade:
    tags = tuple(
        IncomingTag(
            external_id=str(tag["external_id"]),
            name=tag["name"],
            column_key=tag.get("column_key", "entry_reason"),
        )
        for tag in payload.get("tags", [])
    )
    return IncomingTrade(
        external_id=str(payload["external_id"]),
        account_external_id=payload.get("account_external_id", ACCOUNT_EXTERNAL_ID),
        symbol=payload["symbol"],
        side=payload["side"],
        profit_usd=Decimal(str(payload["profit_usd"])),
        account_return_pct=Decimal(str(payload["account_return_pct"])),
        open_time=dt.datetime.fromisoformat(payload["open_time"]),
        close_time=(
            dt.datetime.fromisoformat(payload["close_time"])
            if payload.get("close_time")
            else None
        ),
        percent=_decimal(payload.get("percent")),
        size_usd=_decimal(payload.get("size_usd")),
        leverage=_decimal(payload.get("leverage")),
        duration_sec=payload.get("duration_sec"),
        is_open=bool(payload.get("is_open", False)),
        tags=tags,
        raw=payload,
    )


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))
