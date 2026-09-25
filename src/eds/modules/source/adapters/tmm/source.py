"""Источник сделок TMM: реализация порта TradeSource поверх REST.

Окно запроса мы просим у провайдера, но не верим ему на слово: после ответа
сделки отсекаются по времени закрытия ещё раз, уже у нас. Причина простая —
имя параметра фильтра в открытой документации не подтверждено, а тихо принятый
и проигнорированный фильтр выглядел бы как исправная работа.
"""

import datetime as dt
import logging

from eds.contracts.source import (
    IncomingAccount,
    IncomingTag,
    IncomingTrade,
    SourceBalance,
    SourceCapabilities,
    SourcePosition,
)
from eds.modules.source.adapters.tmm import mapping
from eds.modules.source.adapters.tmm.rest import (
    ENDPOINT_KEYS,
    ENDPOINT_TAG_COLUMNS,
    ENDPOINT_TAGS,
    ENDPOINT_TRADES,
    EndpointMissing,
    TmmClient,
    rows_of,
)
from eds.platform.errors import AppError

log = logging.getLogger("eds.source.tmm")

PAGE_SIZE = 200
# Предел страниц на один проход. Нужен только на случай, если фильтр по окну
# провайдером не поддержан: тогда мы читаем свежие сделки и отсекаем лишние
# сами, и предел удерживает проход в разумных границах.
MAX_PAGES = 10
# Окно пробы подключения: столько дней показываем трейдеру, чтобы он убедился,
# что ключ рабочий и поля сошлись. В сервис эти сделки не попадают.
PROBE_DAYS = 30
# Когда список ключей недоступен, счета выводим из сделок за год: реже
# торгуемый счёт иначе потерялся бы, и его сделки пропускались бы как чужие.
ACCOUNT_PROBE_DAYS = 365


class TmmSource:
    """Порт TradeSource для TMM. Один экземпляр на один проход сверки."""

    provider = "tmm"

    def __init__(self, client: TmmClient):
        self._client = client
        self._dictionary: mapping.TagDictionary | None = None
        self._tags_available = True
        self._tags_problem: str | None = None
        # Диагностика прохода: её показывает кнопка «Проверить» на экране
        # источника. Без неё расхождение с провайдером пришлось бы искать
        # по логам сервера, до которых у трейдера нет доступа.
        self.mapping_errors: list[str] = []
        self.rows_seen = 0
        self.pages_read = 0
        self.window_filter_honored: bool | None = None
        self.accounts_from_trades = False

    # --- возможности ---

    def capabilities(self) -> SourceCapabilities:
        """Что умеет TMM.

        `provides_tags` не константа: если словарь тегов прочитать не удалось,
        источник объявляет себя источником без тегов, и разметка переходит
        к трейдеру. Это лучше, чем считать сделку с тегом выхода размеченной
        «по системе» — покрытие и дисциплина тогда показывали бы неправду.
        """
        return SourceCapabilities(
            provides_trades=True,
            provides_tags=self._tags_available,
            provides_positions=False,
            provides_balance=False,
            needs_aggregation=False,
            history_depth="full",
        )

    @property
    def tags_problem(self) -> str | None:
        return self._tags_problem

    # --- счета ---

    async def fetch_accounts(self) -> list[IncomingAccount]:
        """Счета трейдера — записи биржевых ключей в TMM.

        Основной путь — список ключей. Если адреса нет, счета выводим из самих
        сделок: `api_key_id` есть в каждой. Обходной путь существует потому,
        что адрес списка ключей взят из общего описания API, а не проверен
        на живом ответе, и подключение не должно ломаться из-за одного адреса.
        """
        try:
            rows = rows_of(await self._client.get(ENDPOINT_KEYS))
        except EndpointMissing:
            log.warning("TMM: адрес %s не найден, счета выводим из сделок", ENDPOINT_KEYS)
            rows = []

        accounts: list[IncomingAccount] = []
        for row in rows:
            try:
                accounts.append(mapping.account_from_row(row))
            except mapping.MappingError as exc:
                log.warning("TMM: запись ключа не разобрана: %s", exc)
        if accounts:
            return accounts

        self.accounts_from_trades = True
        return await self._accounts_from_trades()

    async def _accounts_from_trades(self) -> list[IncomingAccount]:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=ACCOUNT_PROBE_DAYS)
        rows = await self._page(since, None, offset=0)
        seen: dict[str, IncomingAccount] = {}
        for row in rows:
            external = mapping.pick(row, "api_key_id", "apiKeyId", "account_id")
            if external is None:
                continue
            key = str(external)
            seen.setdefault(key, IncomingAccount(external_id=key, name=f"Счёт {key}"))
        return list(seen.values())

    # --- словарь тегов ---

    async def fetch_tags(self) -> list[IncomingTag]:
        dictionary = await self._tag_dictionary()
        return dictionary.entry_tags()

    async def _tag_dictionary(self, *, refresh: bool = False) -> mapping.TagDictionary:
        if self._dictionary is not None and not refresh:
            return self._dictionary

        try:
            tag_rows = rows_of(await self._client.get(ENDPOINT_TAGS))
        except EndpointMissing:
            self._fall_back_without_tags(f"адрес {ENDPOINT_TAGS} не найден")
            self._dictionary = mapping.EMPTY_DICTIONARY
            return self._dictionary

        try:
            column_rows = rows_of(await self._client.get(ENDPOINT_TAG_COLUMNS))
        except EndpointMissing:
            column_rows = []

        dictionary = mapping.tag_dictionary(tag_rows, column_rows)
        if tag_rows and not dictionary.entry_tags():
            # Теги есть, а колонку входа опознать нечем. Смешивать колонки
            # нельзя: тег выхода сделал бы сделку «по системе» без разбора.
            self._fall_back_without_tags(
                "колонку тегов входа не удалось опознать: "
                f"{ENDPOINT_TAG_COLUMNS} не дал ключей колонок"
            )
        else:
            self._tags_available = True
            self._tags_problem = None

        self._dictionary = dictionary
        return dictionary

    def _fall_back_without_tags(self, reason: str) -> None:
        log.warning("TMM: теги недоступны (%s), разметка переходит к трейдеру", reason)
        self._tags_available = False
        self._tags_problem = reason

    # --- сделки ---

    async def probe_trades(self, *, days: int = PROBE_DAYS) -> list[IncomingTrade]:
        """Одна страница последних сделок для показа трейдеру.

        Это не приём: возвращённые сделки нигде не сохраняются. Нужно потому,
        что историю мы не импортируем, и без пробы первое подключение выглядит
        одинаково при рабочем и при неправильно понятом ответе провайдера.
        """
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        dictionary = await self._tag_dictionary()
        rows = await self._page(since, None, offset=0)
        self.rows_seen = len(rows)
        trades, _ = self._map_rows(rows, dictionary)
        return sorted(
            trades, key=lambda t: t.close_time or t.open_time, reverse=True
        )

    async def fetch_trades(
        self, since: dt.datetime, until: dt.datetime | None = None
    ) -> list[IncomingTrade]:
        dictionary = await self._tag_dictionary()
        rows = await self._all_pages(since, until)
        self.rows_seen = len(rows)

        trades, unknown = self._map_rows(rows, dictionary)
        if unknown:
            # Тег мог появиться после того, как мы прочитали словарь.
            # Перечитываем один раз: неизвестный тег входа — это незамеченное
            # нарушение, а второй проход стоит один запрос.
            log.info("TMM: %s неизвестных тегов, перечитываю словарь", len(unknown))
            dictionary = await self._tag_dictionary(refresh=True)
            trades, unknown = self._map_rows(rows, dictionary)
            if unknown:
                log.warning("TMM: теги %s не найдены в словаре", sorted(unknown)[:10])

        return self._within_window(trades, since, until)

    def _map_rows(
        self, rows: list[dict], dictionary: mapping.TagDictionary
    ) -> tuple[list[IncomingTrade], set[str]]:
        self.mapping_errors = []
        trades: list[IncomingTrade] = []
        unknown: set[str] = set()
        for row in rows:
            try:
                trade, unresolved = mapping.trade_from_row(row, dictionary)
            except mapping.MappingError as exc:
                # Одна неразобранная сделка не должна валить проход, но и тонуть
                # в тишине не должна: текст уходит в диагностику подключения.
                self.mapping_errors.append(str(exc))
                log.warning("TMM: сделка не разобрана: %s", exc)
                continue
            unknown.update(unresolved)
            trades.append(trade)
        return trades, unknown

    def _within_window(
        self,
        trades: list[IncomingTrade],
        since: dt.datetime,
        until: dt.datetime | None,
    ) -> list[IncomingTrade]:
        kept: list[IncomingTrade] = []
        outside = 0
        for trade in trades:
            if trade.close_time is None:
                kept.append(trade)
                continue
            if trade.close_time < since or (until and trade.close_time > until):
                outside += 1
                continue
            kept.append(trade)
        if outside:
            self.window_filter_honored = False
            log.info("TMM: %s сделок вне запрошенного окна отсечены у нас", outside)
        elif self.window_filter_honored is None:
            self.window_filter_honored = True
        return kept

    async def _all_pages(
        self, since: dt.datetime, until: dt.datetime | None
    ) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        for page in range(MAX_PAGES):
            batch = await self._page(since, until, offset=offset)
            self.pages_read = page + 1
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        else:
            log.warning("TMM: достигнут предел страниц (%s)", MAX_PAGES)
        return await self._fill_truncated(rows)

    async def _page(
        self, since: dt.datetime, until: dt.datetime | None, *, offset: int
    ) -> list[dict]:
        # sort/order просим явно: при чтении постранично важно, чтобы свежие
        # сделки были на первой странице. Если провайдер эти параметры не знает,
        # он их проигнорирует, а окно мы всё равно проверяем сами.
        params: dict[str, object] = {
            "limit": PAGE_SIZE,
            "offset": offset,
            "sort": "closeTime",
            "order": "desc",
        }
        # Границы с запасом в сутки: фильтр у провайдера по датам, а дата
        # зависит от его часового пояса, который мы не знаем.
        left = (since - dt.timedelta(days=1)).date().isoformat()
        right = ((until or dt.datetime.now(dt.UTC)) + dt.timedelta(days=1)).date().isoformat()
        with_filter = dict(params, closeBetween=f"{left},{right}")
        try:
            return rows_of(await self._client.get(ENDPOINT_TRADES, with_filter))
        except AppError as exc:
            if exc.code != "provider_rejected":
                raise
            # Провайдер не принял фильтр — просим без него и отсекаем окно сами.
            log.warning("TMM: параметры запроса не приняты, читаю без них")
            self.window_filter_honored = False
            bare = {"limit": PAGE_SIZE, "offset": offset}
            return rows_of(await self._client.get(ENDPOINT_TRADES, bare))

    async def _fill_truncated(self, rows: list[dict]) -> list[dict]:
        """Дочитать сделки, пришедшие урезанными (Архитектура ч.1 §4.3)."""
        out: list[dict] = []
        for row in rows:
            if not mapping.truncated(row):
                out.append(row)
                continue
            external = mapping.pick(row, "id", "trade_id", "tradeId")
            try:
                full = rows_of(await self._client.get(f"{ENDPOINT_TRADES}{external}"))
            except (EndpointMissing, AppError) as exc:
                log.warning("TMM: сделку %s дочитать не удалось: %s", external, exc)
                out.append(row)
                continue
            out.append(full[0] if full else row)
        return out

    async def fetch_positions(self) -> list[SourcePosition]:
        """Открытых позиций у этого источника нет — и это ответ, а не заглушка.

        Интерфейс спрашивает возможности, а не имя провайдера, поэтому метод
        обязан быть у всех источников: ветвление по провайдеру на стороне
        вызывающего это ровно то, чего возможности и избегают.
        """
        return []

    async def fetch_balance(self) -> SourceBalance | None:
        """Баланса этот источник не отдаёт. None, а не ноль: ноль — пустой счёт."""
        return None
