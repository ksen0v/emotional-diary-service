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
    SourceBalance,
    SourceCapabilities,
    SourcePosition,
)
from eds.modules.source.models import FakeFeedItem

ACCOUNT_EXTERNAL_ID = "fake-account-1"
ACCOUNT_NAME = "Тестовый счёт"


class FakeSource:
    """Реализация порта TradeSource поверх таблицы source.fake_feed."""

    provider = "fake"

    def __init__(
        self,
        session: AsyncSession,
        connection_id: uuid.UUID,
        declared: dict | None = None,
    ):
        self._s = session
        self._connection_id = connection_id
        self._declared = declared or {}

    def capabilities(self) -> SourceCapabilities:
        """По умолчанию фейк изображает TMM: готовые сделки и теги.

        Обе возможности переключаются, чтобы он умел изображать и Binance —
        источник без тегов и с открытыми позициями. Это не игрушка: своя
        разметка и честная просадка иначе проверялись бы только на живом ключе,
        то есть редко и руками.
        """
        return SourceCapabilities(
            provides_trades=True,
            provides_tags=bool(self._declared.get("provides_tags", True)),
            provides_positions=bool(self._declared.get("provides_positions", False)),
            provides_balance=bool(self._declared.get("provides_positions", False)),
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

    async def fetch_tags(self) -> list[IncomingTag]:
        """Теги, встречавшиеся в ленте. Своего словаря у фейка нет."""
        rows = await self._s.execute(
            select(FakeFeedItem.payload).where(
                FakeFeedItem.connection_id == self._connection_id
            )
        )
        seen: dict[str, IncomingTag] = {}
        for payload in rows.scalars():
            for tag in payload.get("tags", []):
                external_id = str(tag["external_id"])
                seen.setdefault(
                    external_id,
                    IncomingTag(
                        external_id=external_id,
                        name=tag["name"],
                        column_key=tag.get("column_key", "entry_reason"),
                    ),
                )
        return list(seen.values())

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


    async def fetch_positions(self) -> list[SourcePosition]:
        """Открытые позиции, поданные через dev-панель.

        Лежат там же, где настоящие, — в `source.positions`. Отдельного
        хранилища у фейка нет сознательно: иначе он проверял бы не тот путь,
        по которому пойдут настоящие данные.
        """
        from eds.modules.source.models import Position as PositionRow

        if not self.capabilities().provides_positions:
            return []
        res = await self._s.execute(
            select(PositionRow).where(PositionRow.connection_id == self._connection_id)
        )
        return [
            SourcePosition(
                symbol=row.symbol,
                position_side=row.position_side,
                qty=row.qty,
                entry_price=row.entry_price,
                mark_price=row.mark_price,
                unrealized_usd=row.unrealized_usd,
                liquidation=row.liquidation,
            )
            for row in res.scalars()
        ]

    async def fetch_balance(self) -> SourceBalance | None:
        """Баланс из последнего снимка. None — источник его не отдаёт."""
        from eds.modules.source.models import BalanceSnapshot

        if not self.capabilities().provides_balance:
            return None
        res = await self._s.execute(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.connection_id == self._connection_id)
            .order_by(BalanceSnapshot.taken_at.desc())
            .limit(1)
        )
        row = res.scalar_one_or_none()
        if row is None:
            return None
        return SourceBalance(
            wallet_usdt=row.wallet_usdt,
            equity_usdt=row.equity_usdt,
            taken_at=row.taken_at,
        )


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
