"""Источник сделок Binance: порт `TradeSource` поверх REST и агрегатора.

Отличие от TMM принципиальное: у TMM есть готовые сделки, здесь их нет.
Поэтому источник не «читает и переводит», а собирает: тянет исполнения,
кладёт их к себе и пересобирает из них сделки.

Из этого следует устройство, которое иначе выглядело бы странно: у источника
есть сессия базы. Он пишет филлы, начисления и снимки баланса — это его сырьё,
а не результат. Сделки наружу отдаются обычными `IncomingTrade`, и модуль
`trades` по-прежнему не знает, откуда они взялись.

Проба подключения работает без базы: там ещё нечего сохранять, и сделки
собираются в памяти, только чтобы показать трейдеру, что ключ рабочий.
"""

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts.source import (
    IncomingAccount,
    IncomingTag,
    IncomingTrade,
    SourceBalance,
    SourceCapabilities,
    SourcePosition,
)
from eds.modules.source.adapters.binance import mapping
from eds.modules.source.adapters.binance.aggregate import (
    AggregatedTrade,
    Fill,
    IncomeRow,
    aggregate,
    apply_funding,
    return_pct,
)
from eds.modules.source.adapters.binance.rest import (
    ENDPOINT_BALANCE,
    ENDPOINT_INCOME,
    ENDPOINT_POSITIONS,
    ENDPOINT_RESTRICTIONS,
    ENDPOINT_USER_TRADES,
    PAGE_LIMIT,
    BinanceClient,
    at,
    ms,
    windows,
)
from eds.platform.errors import AppError

log = logging.getLogger("eds.source.binance")

# У Binance нет «счетов» в смысле TMM: ключ открывает один фьючерсный счёт.
# Идентификатор постоянный, потому что на него ссылаются принятые сделки,
# и меняться он не должен никогда.
ACCOUNT_EXTERNAL_ID = "usdm-futures"
ACCOUNT_NAME = "Binance USDⓈ-M Futures"

MARKET = "futures"
PROBE_DAYS = 7
# Запас, на который сверка заходит назад от последнего известного исполнения.
OVERLAP = dt.timedelta(hours=1)
ZERO = Decimal("0")


class BinanceSource:
    """Порт TradeSource для Binance. Один экземпляр на один проход."""

    provider = "binance"

    def __init__(
        self,
        client: BinanceClient,
        *,
        s: AsyncSession | None = None,
        connection_id=None,
        user_id=None,
    ):
        self._client = client
        self._s = s
        self._connection_id = connection_id
        self._user_id = user_id
        # Диагностика прохода: её показывает кнопка «Проверить» и отчёт сверки.
        # Без неё расхождение с биржей пришлось бы искать по логам сервера.
        self.mapping_errors: list[str] = []
        self.fills_seen = 0
        self.fills_new = 0
        self.symbols_seen: list[str] = []
        self.hedge_detected = False
        self.commission_assets: set[str] = set()
        self.rebuilt_positions: list[str] = []

    # --- возможности ---

    def capabilities(self) -> SourceCapabilities:
        """Что умеет Binance.

        Здесь всё константы, в отличие от TMM: биржа либо отдаёт позиции
        и баланс, либо не отвечает вовсе. Тегов у неё нет никогда — отсюда
        своя разметка в нашей ленте (ТЗ 4.2).
        """
        return SourceCapabilities(
            provides_trades=True,
            provides_tags=False,
            provides_positions=True,
            provides_balance=True,
            needs_aggregation=True,
            history_depth="days:90",
        )

    # --- ключ ---

    async def check_key(self) -> mapping.KeyVerdict:
        """Прочитать права ключа. Отдельный базовый адрес — это SAPI, не fapi."""
        raw = await self._client.signed(ENDPOINT_RESTRICTIONS, sapi=True)
        if not isinstance(raw, dict):
            raise AppError(
                "provider_bad_response",
                "Binance ответил на запрос прав ключа не тем, что мы умеем читать.",
                502,
            )
        return mapping.check_key(raw)

    # --- счета и теги ---

    async def fetch_accounts(self) -> list[IncomingAccount]:
        return [
            IncomingAccount(
                external_id=ACCOUNT_EXTERNAL_ID,
                name=ACCOUNT_NAME,
                exchange="binance",
                market=MARKET,
            )
        ]

    async def fetch_tags(self) -> list[IncomingTag]:
        """Тегов у биржи нет. Пустой список — это ответ, а не отсутствие ответа.

        Из него интерфейс и узнаёт, что разметка переезжает в нашу ленту:
        `provides_tags = False` в возможностях, и `PUT /trades/{id}/marking`
        начинает работать.
        """
        return []

    # --- баланс и позиции ---

    async def fetch_balance(self) -> SourceBalance | None:
        """Баланс счёта. None — прочитать не удалось.

        Не исключение: без баланса сделки всё равно принимаются, просто без
        процентов от депозита. Уронить из-за этого весь проход было бы хуже.
        """
        try:
            rows = await self._client.signed(ENDPOINT_BALANCE)
            if not isinstance(rows, list):
                raise AppError(
                    "provider_bad_response",
                    "Binance ответил не тем на запрос баланса.",
                    502,
                )
            return mapping.balance_from_rows(rows, taken_at=dt.datetime.now(dt.UTC))
        except (AppError, mapping.MappingError) as exc:
            log.warning("Binance: баланс не прочитан (%s)", exc)
            return None

    async def fetch_positions(self) -> list[SourcePosition]:
        rows = await self._client.signed(ENDPOINT_POSITIONS)
        if not isinstance(rows, list):
            return []
        out: list[SourcePosition] = []
        for row in rows:
            try:
                position = mapping.position_from_row(row)
            except mapping.MappingError as exc:
                self.mapping_errors.append(str(exc))
                continue
            if position.is_open:
                out.append(position)
        return out

    # --- сделки ---

    async def probe_trades(self, *, days: int = PROBE_DAYS) -> list[IncomingTrade]:
        """Последние сделки для показа трейдеру. Ничего не сохраняет.

        Нужна по той же причине, что у TMM: историю мы не импортируем, поэтому
        сразу после подключения лента пуста, и проверить «ключ рабочий, суммы
        сошлись» иначе нечем. При источнике, который собирает сделки сам,
        это ещё важнее: посмотреть надо не на ключ, а на работу агрегатора.
        """
        until = dt.datetime.now(dt.UTC)
        since = until - dt.timedelta(days=days)
        fills, income = await self._download(since, until)
        result = aggregate([f for f, _ in fills])
        self.hedge_detected = result.hedge_detected
        self.commission_assets = result.commission_in_other_asset
        trades = apply_funding(result.trades, income)
        balance = await self.fetch_balance()
        return [
            self._as_incoming(trade, balance.wallet_usdt if balance else None)
            for trade in sorted(trades, key=lambda t: t.close_time, reverse=True)
        ]

    async def fetch_trades(
        self, since: dt.datetime, until: dt.datetime | None = None
    ) -> list[IncomingTrade]:
        """Забрать исполнения за окно и пересобрать из них сделки.

        Порядок ровно такой: сначала сохраняем сырьё, потом собираем сделки
        из сохранённого. Собирать «на лету» из только что скачанного нельзя —
        тогда сделка, начатая в прошлом окне, потеряла бы своё начало.
        """
        if self._s is None or self._connection_id is None:
            raise AppError(
                "not_supported",
                "Приём сделок Binance требует подключения в базе.",
                500,
            )
        until = until or dt.datetime.now(dt.UTC)
        from eds.modules.source import repo

        since = await self._window_start(since, until)
        fills, income = await self._download(since, until)

        inserted = await repo.save_fills(
            self._s, self._connection_id, self._user_id, fills
        )
        await repo.save_income(self._s, self._connection_id, income)
        self.fills_seen = len(fills)
        self.fills_new = len(inserted)

        balance = await self.fetch_balance()
        if balance is not None:
            await repo.save_balance(
                self._s,
                self._connection_id,
                balance.taken_at,
                balance.wallet_usdt,
                balance.equity_usdt,
            )

        trades = await self._rebuild(inserted)
        return trades

    async def accept_fills(
        self, fills: list[tuple[Fill, dict]]
    ) -> list[IncomingTrade]:
        """Принять исполнения, пришедшие потоком, и пересобрать сделки.

        То же, что делает сверка, минус сеть. Отдельный вход нужен потому, что
        поток приносит по одному исполнению, а спрашивать из-за него биржу
        заново значило бы платить весом запросов за то, что уже пришло.
        """
        if self._s is None or self._connection_id is None:
            raise AppError(
                "not_supported", "Приём исполнений требует подключения в базе.", 500
            )
        from eds.modules.source import repo

        inserted = await repo.save_fills(
            self._s, self._connection_id, self._user_id, fills
        )
        self.fills_seen = len(fills)
        self.fills_new = len(inserted)
        if not inserted:
            return []
        return await self._rebuild(inserted)

    async def _window_start(
        self, ingest_from: dt.datetime, until: dt.datetime
    ) -> dt.datetime:
        """С какого момента спрашивать исполнения.

        Первый проход берёт всё от точки подключения. Дальше — только хвост
        с запасом от последнего известного исполнения: сверка смотрит последние
        сутки (Архитектура ч.1 §5.6), а перечитывать всю историю каждые десять
        минут значило бы платить весом запросов за данные, которые уже лежат.

        Запас в час не декоративный: он перекрывает и часы, разошедшиеся
        с биржей, и исполнение, доехавшее позже соседних.
        """
        from eds.modules.source import repo

        last = await repo.last_fill_time(self._s, self._connection_id)
        if last is None:
            return ingest_from
        start = max(ingest_from, last - OVERLAP)
        return min(start, until)

    async def _rebuild(self, inserted: list[Fill]) -> list[IncomingTrade]:
        """Пересобрать сделки из сохранённых исполнений.

        Каждая позиция пересчитывается с последнего закрытого нуля: `last_fill_id`
        в `aggregate_state` — это последний филл последней закрытой сделки, и всё
        после него собирается заново на каждом проходе. Поэтому доехавший позже
        филл встаёт на место сам, без отдельной починки.

        Если новый филл оказался СТАРШЕ этой границы — значит мы пропустили его
        тогда и закрыли позицию не там, где надо. Такую позицию пересобираем
        целиком, с нуля: результат важнее экономии запроса к своей же базе.
        """
        from eds.modules.source import repo

        states = await repo.aggregate_states(self._s, self._connection_id)
        touched: dict[tuple[str, str], int] = {}
        for fill in inserted:
            key = (fill.symbol, fill.position_side)
            touched[key] = min(touched.get(key, fill.external_id), fill.external_id)

        keys = set(touched) | set(states)
        out: list[AggregatedTrade] = []
        for key in sorted(keys):
            symbol, position_side = key
            state = states.get(key)
            border = state.last_fill_id if state else 0
            if key in touched and touched[key] <= border:
                log.warning(
                    "Binance: %s %s — филл старше границы, пересобираю позицию целиком",
                    symbol,
                    position_side,
                )
                self.rebuilt_positions.append(f"{symbol} {position_side}")
                border = 0

            fills = await repo.fills_after(
                self._s, self._connection_id, symbol, position_side, border
            )
            if not fills:
                continue
            result = aggregate(fills)
            self.hedge_detected = self.hedge_detected or result.hedge_detected
            self.commission_assets |= result.commission_in_other_asset
            out.extend(result.trades)

            closed_to = max(
                (t.last_fill_id for t in result.trades), default=border
            )
            open_position = result.open_positions.get(key)
            await repo.save_aggregate_state(
                self._s,
                self._connection_id,
                symbol,
                position_side,
                closed_to,
                open_position.as_dict() if open_position else None,
            )

        if not out:
            return []

        income = await repo.income_between(
            self._s,
            self._connection_id,
            min(t.open_time for t in out),
            max(t.close_time for t in out),
        )
        out = apply_funding(out, income)

        incoming: list[IncomingTrade] = []
        for trade in sorted(out, key=lambda t: t.close_time):
            balance = await repo.balance_at(
                self._s, self._connection_id, trade.open_time
            )
            incoming.append(self._as_incoming(trade, balance))
        return incoming

    # --- сеть ---

    async def _download(
        self, since: dt.datetime, until: dt.datetime
    ) -> tuple[list[tuple[Fill, dict]], list[IncomeRow]]:
        """Двухходовой импорт: сначала символы, потом исполнения по каждому.

        `userTrades` требует символ — одним запросом «все мои исполнения»
        не спросить. Список символов даёт `income`, которому символ не нужен:
        по нему видно, где за окно вообще была активность. Без этого пришлось бы
        перебирать все четыреста с лишним торгуемых пар (Архитектура ч.1 §5.4).
        """
        income: list[IncomeRow] = []
        for left, right in windows(since, until):
            income.extend(await self._income_page(left, right))

        symbols = mapping.symbols_of(income)
        self.symbols_seen = symbols

        fills: list[tuple[Fill, dict]] = []
        for symbol in symbols:
            for left, right in windows(since, until):
                fills.extend(await self._user_trades(symbol, left, right))
        return fills, income

    async def _income_page(
        self, since: dt.datetime, until: dt.datetime
    ) -> list[IncomeRow]:
        rows = await self._client.signed(
            ENDPOINT_INCOME,
            {"startTime": ms(since), "endTime": ms(until), "limit": PAGE_LIMIT},
        )
        if not isinstance(rows, list):
            return []
        out: list[IncomeRow] = []
        for row in rows:
            try:
                out.append(mapping.income_from_row(row))
            except mapping.MappingError as exc:
                self.mapping_errors.append(str(exc))
        return out

    async def _user_trades(
        self, symbol: str, since: dt.datetime, until: dt.datetime
    ) -> list[tuple[Fill, dict]]:
        """Исполнения по символу за окно, страницами.

        Страницы идут по времени, а не по `fromId`, как написано в Архитектуре
        ч.1 §5.4: Binance не принимает `fromId` вместе с окном, а окно нам
        нужно обязательно — сверка смотрит последние сутки, а не всю историю.
        Отступление названо в README.
        """
        out: list[tuple[Fill, dict]] = []
        left = since
        for _ in range(20):
            rows = await self._client.signed(
                ENDPOINT_USER_TRADES,
                {
                    "symbol": symbol,
                    "startTime": ms(left),
                    "endTime": ms(until),
                    "limit": PAGE_LIMIT,
                },
            )
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                try:
                    out.append((mapping.fill_from_row(row), row))
                except mapping.MappingError as exc:
                    self.mapping_errors.append(str(exc))
            if len(rows) < PAGE_LIMIT:
                break
            # Следующая страница — с миллисекунды после последнего исполнения.
            # Дубли на стыке страниц не страшны: филл учитывается по своему
            # идентификатору, и повтор отсекается ключом в базе.
            last = max(int(row["time"]) for row in rows)
            left = at(last + 1)
            if left >= until:
                break
        return out

    # --- перевод в общий вид ---

    def _as_incoming(
        self, trade: AggregatedTrade, balance: Decimal | None
    ) -> IncomingTrade:
        notional = trade.entry_price * trade.qty
        pct = return_pct(trade.profit_usd, balance)
        return IncomingTrade(
            external_id=trade.external_id,
            account_external_id=ACCOUNT_EXTERNAL_ID,
            symbol=trade.symbol,
            side=trade.side,
            profit_usd=trade.profit_usd,
            # Нет снимка баланса — нет и процента от депозита. Ноль здесь
            # означал бы «сделка ничего не изменила», и она провалилась бы
            # мимо счётчиков дня незамеченной.
            account_return_pct=pct if pct is not None else ZERO,
            open_time=trade.open_time,
            close_time=trade.close_time,
            percent=(trade.profit_usd / notional * 100) if notional else None,
            size_usd=notional,
            # Плечо биржа отдаёт по позиции, а не по исполнению, и для закрытой
            # сделки его уже не спросить. Пусто честнее выдуманного числа.
            leverage=None,
            duration_sec=int((trade.close_time - trade.open_time).total_seconds()),
            is_open=False,
            tags=(),
            raw={
                "realized_pnl": str(trade.realized_pnl),
                "commission_usdt": str(trade.commission_usdt),
                "funding": str(trade.funding),
                "entry_price": str(trade.entry_price),
                "qty": str(trade.qty),
                "position_side": trade.position_side,
                "fill_ids": list(trade.fill_ids),
                "balance_at_open": str(balance) if balance is not None else None,
            },
        )
