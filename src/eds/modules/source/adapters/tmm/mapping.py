"""Перевод данных TMM в формы порта источника.

Здесь нет ни HTTP, ни базы — только чистые функции от словаря к дата-классу.
Так проверяется самое рискованное место шага: если `profit_deposit` понят
неверно или миллисекунды прочитаны как секунды, ломается вся арифметика
процентов и границы торгового дня, а поймать это на живом API нечем.

Имена полей ищутся по списку вариантов. Причина не в аккуратности: полную
спецификацию TMM прочитать не удалось (документация рисуется скриптом,
а выгрузки OpenAPI по обычным адресам нет), поэтому snake_case и camelCase
принимаются оба. Там, где вариант угадан, стоит пометка.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from eds.contracts.source import IncomingAccount, IncomingTag, IncomingTrade

# Метки времени у TMM в миллисекундах (Архитектура ч.1 §4.1). Порог отделяет
# миллисекунды от секунд: 10^11 мс — это 1973 год, а 10^11 секунд — 5138-й.
# Ни одна сделка не попадает между, поэтому ошибка в единицах не пройдёт молча.
MS_THRESHOLD = 10**11

ENTRY_COLUMN = "entry_reason"

LONG = ("long", "buy", "l", "b", "1")
SHORT = ("short", "sell", "s", "2")


class MappingError(ValueError):
    """Сделку TMM не удалось перевести в нашу форму."""


@dataclass(frozen=True)
class TagDictionary:
    """Словарь тегов трейдера: внешний id → тег с ключом колонки.

    Держим целиком, а не только колонку входа: только по полному словарю видно,
    что тег на сделке нам известен, но относится к другой колонке. Иначе
    неизвестный id и «тег выхода» выглядели бы одинаково, и мы перечитывали бы
    словарь на каждой сделке с тегом выхода.
    """

    tags: dict[str, IncomingTag]

    def entry_tags(self) -> list[IncomingTag]:
        return [t for t in self.tags.values() if t.column_key == ENTRY_COLUMN]

    def knows(self, external_id: str) -> bool:
        return external_id in self.tags


EMPTY_DICTIONARY = TagDictionary(tags={})


def pick(row: dict[str, Any], *names: str) -> Any:
    """Первое непустое значение из списка возможных имён поля."""
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MappingError(f"не число: {value!r}") from exc


def to_datetime(value: Any) -> dt.datetime | None:
    """Метка времени TMM → aware datetime в UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.lstrip("-").isdigit():
            # Строка ISO — принимаем, хотя по спецификации приходят числа.
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
        value = int(text)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MappingError(f"не метка времени: {value!r}") from exc
    if number <= 0:
        return None
    seconds = number / 1000 if number > MS_THRESHOLD else number
    return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)


def side_of(value: Any) -> str:
    text = str(value).strip().lower()
    if text in LONG:
        return "long"
    if text in SHORT:
        return "short"
    raise MappingError(f"неизвестное направление сделки: {value!r}")


def is_open(row: dict[str, Any]) -> bool:
    """Открыта ли позиция.

    Поле `process` у TMM означает «сделка в процессе» (Архитектура ч.1 §4.4).
    Отсутствие времени закрытия считаем тем же самым: в MVP принимаются только
    закрытые сделки, и сомнительный случай должен читаться как «открыта»,
    иначе незакрытая сделка попадёт в метрики дня с недосчитанным PnL.
    """
    flag = pick(row, "process", "is_open", "isOpen", "in_process", "inProcess")
    if isinstance(flag, bool):
        if flag:
            return True
    elif flag is not None and str(flag).strip().lower() in ("1", "true", "open", "process"):
        return True
    return to_datetime(pick(row, "close_time", "closeTime", "closed_at")) is None


def account_from_row(row: dict[str, Any]) -> IncomingAccount:
    """Счёт из записи биржевого ключа TMM.

    Состав полей подтверждён на живом аккаунте: apiKeyId, name, exchange,
    market, маска ключа, состояние.
    """
    external = pick(row, "id", "api_key_id", "apiKeyId", "key_id", "keyId")
    if external is None:
        raise MappingError(f"в записи ключа нет идентификатора: {sorted(row)}")
    name = pick(row, "name", "title", "label") or f"Счёт {external}"
    market = pick(row, "market", "market_type", "marketType")
    exchange = pick(row, "exchange", "exchange_name", "exchangeName")
    return IncomingAccount(
        external_id=str(external),
        name=str(name),
        exchange=str(exchange) if exchange else None,
        market=str(market).lower() if market else None,
    )


def tag_dictionary(
    tag_rows: list[dict[str, Any]], column_rows: list[dict[str, Any]]
) -> TagDictionary:
    """Собрать словарь тегов, приписав каждому устойчивый ключ колонки.

    Колонки сопоставляем по `key`, а не по id: ключ объявлен устойчивым
    (Архитектура ч.1 §4.4), а id встроенных колонок — деталь реализации TMM.
    У колонок без ключа (например пользовательский «Rating») ключа нет,
    и теги из них в разметку не попадут — так и задумано: нарушение
    определяется тегом входа.
    """
    keys: dict[str, str] = {}
    for column in column_rows:
        column_id = pick(column, "id", "column_id", "columnId", "category_id")
        key = pick(column, "key", "slug", "code")
        if column_id is not None and key:
            keys[str(column_id)] = str(key)

    tags: dict[str, IncomingTag] = {}
    for row in tag_rows:
        tag_id = pick(row, "id", "tag_id", "tagId")
        name = pick(row, "name", "title", "label")
        if tag_id is None or name is None:
            continue
        column_id = pick(
            row, "column_id", "columnId", "tag_column_id", "tagColumnId", "category_id"
        )
        key = pick(row, "column_key", "columnKey", "key")
        column_key = str(key) if key else keys.get(str(column_id), "")
        tags[str(tag_id)] = IncomingTag(
            external_id=str(tag_id), name=str(name), column_key=column_key
        )
    return TagDictionary(tags=tags)


def tag_ids_of(row: dict[str, Any]) -> list[str]:
    """Идентификаторы тегов сделки.

    Форма поля `tags` в payload не подтверждена, поэтому принимаются все три
    вероятные: список чисел, список строк и список объектов.
    """
    raw = pick(row, "tags", "tag_ids", "tagIds", "entry_tags", "entryTags")
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get(ENTRY_COLUMN) or raw.get("entry") or list(raw.values())
    if not isinstance(raw, list):
        raw = [raw]

    ids: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            value = pick(item, "id", "tag_id", "tagId")
            if value is not None:
                ids.append(str(value))
            continue
        if isinstance(item, list):
            ids.extend(str(x) for x in item)
            continue
        ids.append(str(item))
    return ids


def entry_tags_of(
    row: dict[str, Any], dictionary: TagDictionary
) -> tuple[tuple[IncomingTag, ...], list[str]]:
    """Теги входа сделки и список идентификаторов, которых нет в словаре.

    Неизвестные возвращаются наружу, а не проглатываются: источник по ним
    решает, не пора ли перечитать словарь. Молча выброшенный тег входа означал
    бы сделку без разметки — то есть нарушение, которое сервис не заметил.
    """
    found: list[IncomingTag] = []
    unknown: list[str] = []
    for tag_id in tag_ids_of(row):
        tag = dictionary.tags.get(tag_id)
        if tag is None:
            unknown.append(tag_id)
        elif tag.column_key == ENTRY_COLUMN:
            found.append(tag)
    return tuple(found), unknown


def trade_from_row(
    row: dict[str, Any], dictionary: TagDictionary = EMPTY_DICTIONARY
) -> tuple[IncomingTrade, list[str]]:
    """Сделка TMM → IncomingTrade. Второе значение — неизвестные id тегов."""
    external = pick(row, "id", "trade_id", "tradeId")
    if external is None:
        raise MappingError(f"в сделке нет идентификатора: {sorted(row)}")

    account = pick(row, "api_key_id", "apiKeyId", "account_id", "accountId")
    if account is None:
        raise MappingError(f"в сделке {external} нет счёта")

    symbol = pick(row, "symbol", "ticker", "pair")
    if not symbol:
        raise MappingError(f"в сделке {external} нет инструмента")

    profit = to_decimal(pick(row, "net_profit", "netProfit", "profit"))
    if profit is None:
        raise MappingError(f"в сделке {external} нет результата")

    # profit_deposit — PnL в процентах от депозита. Основание всех процентных
    # условий сервиса, поэтому отсутствие поля — ошибка, а не ноль по умолчанию:
    # ноль означал бы «сделка не сдвинула счёт», и правила промолчали бы.
    deposit_pct = to_decimal(pick(row, "profit_deposit", "profitDeposit", "accountReturnPct"))
    if deposit_pct is None:
        raise MappingError(f"в сделке {external} нет profit_deposit")

    open_time = to_datetime(pick(row, "open_time", "openTime", "opened_at"))
    if open_time is None:
        raise MappingError(f"в сделке {external} нет времени открытия")
    close_time = to_datetime(pick(row, "close_time", "closeTime", "closed_at"))

    tags, unknown = entry_tags_of(row, dictionary)

    return (
        IncomingTrade(
            external_id=str(external),
            account_external_id=str(account),
            symbol=str(symbol).upper(),
            side=side_of(pick(row, "side", "direction", "position_side")),
            profit_usd=profit,
            account_return_pct=deposit_pct,
            open_time=open_time,
            close_time=close_time,
            percent=to_decimal(pick(row, "percent", "profit_percent", "profitPercent")),
            size_usd=to_decimal(pick(row, "volume", "size", "trade_size", "tradeSize")),
            leverage=to_decimal(pick(row, "leverage", "lever")),
            duration_sec=duration_of(row, open_time, close_time),
            is_open=is_open(row),
            tags=tags,
            raw=row,
        ),
        unknown,
    )


def duration_of(
    row: dict[str, Any], open_time: dt.datetime, close_time: dt.datetime | None
) -> int | None:
    """Длительность сделки в секундах.

    Считаем по двум меткам времени, а поле `duration` берём только если
    закрытия нет. Так единицы измерения перестают быть вопросом: обе метки
    мы уже привели сами, а про `duration` документация говорит «миллисекунды»,
    и проверить это нечем.
    """
    if close_time is not None:
        return max(0, int((close_time - open_time).total_seconds()))
    raw = pick(row, "duration", "duration_sec", "durationSec")
    if raw is None:
        return None
    number = float(raw)
    return int(number / 1000) if number > 10**6 else int(number)


def truncated(row: dict[str, Any]) -> bool:
    """Пришёл ли payload урезанным (Архитектура ч.1 §4.3).

    Урезанные ордера нам безразличны — мы их не используем. Урезанный payload
    означает, что сделку надо дочитать по её адресу.
    """
    return bool(pick(row, "payload_truncated", "payloadTruncated"))
