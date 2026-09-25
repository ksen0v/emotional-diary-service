"""Сборка сделок из исполнений. Чистая функция от данных к данным.

Главная новая работа шага «Binance». Биржа отдаёт филлы — частичные исполнения
ордеров, — а весь сервис построен на сделках: счётчики дня, серии убытков,
разметка, цена эмоций. Между одним и другим стоит этот файл.

**Почему чистая функция, а не запись в базу по ходу дела.** Филлы — истина,
сделки — производная от них (Архитектура ч.1 §5.5). Если агрегатор будет писать
сделки в момент приёма, первая же ошибка в логике останется в данных навсегда,
и поправить её будет нечем: исходных филлов уже не будет. Поэтому филлы
храним всегда, а сделки пересобираются из них в любой момент — после
исправления ошибки, после пропущенного события, после чего угодно.

Из того же свойства следуют два инварианта, которые проверяются тестами:
повторная подача тех же филлов ничего не меняет, и порядок прихода филлов
на результат не влияет.

**Что здесь не считается.** Проценты от депозита: для них нужен баланс на
момент открытия, а он приходит отдельными снимками и в чистую функцию не лезет.
Здесь есть только `return_pct` — арифметика без источника данных.
"""

import datetime as dt
from dataclasses import dataclass, field, replace
from decimal import Decimal

ZERO = Decimal("0")

# Режим позиций. В one-way у всех филлов `BOTH`; `LONG`/`SHORT` означают,
# что у трейдера включён hedge, который мы не проверяли (ТЗ 4.5).
ONE_WAY = "BOTH"

# Комиссию считаем только в USDT (Архитектура ч.1 §5.5). Комиссия в BNB
# требовала бы курса BNB на момент каждого филла — отдельной интеграции
# ради строки в расчёте.
COMMISSION_ASSET = "USDT"

BUY = "BUY"
SELL = "SELL"


@dataclass(frozen=True)
class Fill:
    """Исполнение в том виде, в котором его принимает агрегатор.

    Перевод из ответа Binance — в `mapping.py`; сюда приходит уже наше.
    """

    external_id: int
    order_id: int
    symbol: str
    position_side: str
    side: str
    price: Decimal
    qty: Decimal
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str
    trade_time: dt.datetime

    @property
    def signed_qty(self) -> Decimal:
        """Покупка увеличивает позицию, продажа уменьшает — независимо от режима.

        Знак и есть весь учёт направления: в one-way лонг это путь от нуля вверх
        и обратно, шорт — вниз и обратно. В hedge то же самое внутри своей
        группы `position_side`, поэтому отдельной ветки для hedge нет.
        """
        return self.qty if self.side == BUY else -self.qty


@dataclass(frozen=True)
class AggregatedTrade:
    """Закрытая сделка — round-trip позиции от нуля до нуля."""

    symbol: str
    position_side: str
    side: str  # long | short
    open_time: dt.datetime
    close_time: dt.datetime
    qty: Decimal  # суммарный объём входа
    exit_qty: Decimal  # суммарный объём выхода; у закрытой сделки равен входу
    entry_price: Decimal  # средневзвешенный вход
    realized_pnl: Decimal
    commission_usdt: Decimal
    funding: Decimal
    first_fill_id: int
    last_fill_id: int
    fill_ids: tuple[int, ...]

    @property
    def profit_usd(self) -> Decimal:
        """Результат сделки.

        `realizedPnl` от Binance авторитетнее любого собственного расчёта
        по ценам: биржа знает про частичные исполнения, маркировку и округления
        больше, чем мы. Комиссия вычитается, фандинг прибавляется со своим знаком.
        """
        return self.realized_pnl - self.commission_usdt + self.funding

    @property
    def external_id(self) -> str:
        """Идентификатор сделки для идемпотентности приёма.

        Сделка у биржи объекта не имеет, поэтому ключ собираем из того, что
        не меняется при пересборке: символ, сторона позиции и первый филл.
        Второй раз собранная из тех же филлов сделка получит тот же ключ
        и не задвоится (`unique(user_id, source, external_id)`).
        """
        return f"{self.symbol}:{self.position_side}:{self.first_fill_id}"


@dataclass
class OpenPosition:
    """Незакрытая позиция: то, что агрегатор донёс до конца списка филлов.

    Кладётся в `source.aggregate_state.open_position` и на следующем проходе
    продолжается с того же места, чтобы не перечитывать историю с нуля.
    """

    symbol: str
    position_side: str
    side: str
    qty: Decimal
    exit_qty: Decimal
    signed_qty: Decimal
    open_time: dt.datetime
    entry_cost: Decimal
    entry_qty: Decimal
    realized_pnl: Decimal
    commission_usdt: Decimal
    last_fill_id: int
    first_fill_id: int
    fill_ids: list[int] = field(default_factory=list)

    @property
    def entry_price(self) -> Decimal:
        return self.entry_cost / self.entry_qty if self.entry_qty else ZERO

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "position_side": self.position_side,
            "side": self.side,
            "qty": str(self.qty),
            "exit_qty": str(self.exit_qty),
            "signed_qty": str(self.signed_qty),
            "open_time": self.open_time.isoformat(),
            "entry_cost": str(self.entry_cost),
            "entry_qty": str(self.entry_qty),
            "realized_pnl": str(self.realized_pnl),
            "commission_usdt": str(self.commission_usdt),
            "last_fill_id": self.last_fill_id,
            "first_fill_id": self.first_fill_id,
            "fill_ids": list(self.fill_ids),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "OpenPosition":
        return cls(
            symbol=raw["symbol"],
            position_side=raw["position_side"],
            side=raw["side"],
            qty=Decimal(raw["qty"]),
            exit_qty=Decimal(raw.get("exit_qty", "0")),
            signed_qty=Decimal(raw["signed_qty"]),
            open_time=dt.datetime.fromisoformat(raw["open_time"]),
            entry_cost=Decimal(raw["entry_cost"]),
            entry_qty=Decimal(raw["entry_qty"]),
            realized_pnl=Decimal(raw["realized_pnl"]),
            commission_usdt=Decimal(raw["commission_usdt"]),
            last_fill_id=raw["last_fill_id"],
            first_fill_id=raw["first_fill_id"],
            fill_ids=list(raw.get("fill_ids", [])),
        )


@dataclass
class AggregateResult:
    """Что получилось из списка филлов."""

    trades: list[AggregatedTrade] = field(default_factory=list)
    open_positions: dict[tuple[str, str], OpenPosition] = field(default_factory=dict)
    hedge_detected: bool = False
    commission_in_other_asset: set[str] = field(default_factory=set)

    @property
    def last_fill_ids(self) -> dict[tuple[str, str], int]:
        """Последний филл закрытой сделки по каждой позиции — курсор пересчёта."""
        out: dict[tuple[str, str], int] = {}
        for trade in self.trades:
            key = (trade.symbol, trade.position_side)
            out[key] = max(out.get(key, 0), trade.last_fill_id)
        return out


def aggregate(
    fills: list[Fill],
    *,
    carried: dict[tuple[str, str], OpenPosition] | None = None,
) -> AggregateResult:
    """Собрать сделки из филлов.

    `carried` — незакрытые позиции с прошлого прохода. Без них пришлось бы
    каждый раз читать филлы с начала истории, а с ними проход обрабатывает
    только новое.

    Порядок филлов на входе произвольный: внутри они сортируются по времени
    и идентификатору. Это не удобство, а требование — сверка через REST
    и поток отдают их по-разному, а результат обязан быть один.
    """
    result = AggregateResult()
    if carried:
        result.open_positions = {key: replace(pos) for key, pos in carried.items()}

    for fill in _ordered(_deduplicated(fills)):
        if fill.position_side != ONE_WAY:
            # Молча считать непроверенный режим — худший вариант: ошибка
            # проявится через месяц как необъяснимая сделка в ленте.
            result.hedge_detected = True
        if fill.commission and fill.commission_asset != COMMISSION_ASSET:
            result.commission_in_other_asset.add(fill.commission_asset)
        _apply(result, fill)

    result.trades.sort(key=lambda t: (t.close_time, t.first_fill_id))
    return result


def _deduplicated(fills: list[Fill]) -> list[Fill]:
    """Один филл учитывается один раз.

    Тот же филл приходит и потоком, и сверкой (Архитектура ч.1 §5.6),
    поэтому дубль — это норма приёма, а не сбой.
    """
    seen: dict[int, Fill] = {}
    for fill in fills:
        seen.setdefault(fill.external_id, fill)
    return list(seen.values())


def _ordered(fills: list[Fill]) -> list[Fill]:
    """По времени, при совпадении — по идентификатору.

    Идентификатор во втором ключе не украшение: филлы одного ордера часто
    приходят с одной меткой времени до миллисекунды, и без устойчивого
    второго ключа порядок зависел бы от того, как их вернул провайдер.
    """
    return sorted(fills, key=lambda f: (f.trade_time, f.external_id))


def _apply(result: AggregateResult, fill: Fill) -> None:
    key = (fill.symbol, fill.position_side)
    position = result.open_positions.get(key)
    commission = fill.commission if fill.commission_asset == COMMISSION_ASSET else ZERO

    if position is None:
        result.open_positions[key] = _opened(fill, commission)
        return

    before = position.signed_qty
    after = before + fill.signed_qty

    if _crosses_zero(before, after):
        # Переворот одним филлом: часть объёма закрывает текущую позицию,
        # остаток открывает следующую. Две сделки в истории, а не одна
        # странная — иначе в ленте оказалась бы сделка, которая сначала
        # лонг, а потом шорт, и объяснить её было бы нечем.
        closing_qty = abs(before)
        share = closing_qty / fill.qty if fill.qty else ZERO
        closing_commission = commission * share
        # `realizedPnl` биржа считает по закрываемой части — целиком отдаём
        # закрываемой сделке. Комиссию делим по объёму: она берётся со всего
        # филла, и оставить её на одной из двух сделок значило бы исказить обе.
        _absorb(position, fill, qty=closing_qty, commission=closing_commission)
        position.signed_qty = ZERO
        result.trades.append(_closed(position, fill))

        remainder = fill.qty - closing_qty
        tail = replace(
            fill,
            qty=remainder,
            realized_pnl=ZERO,
            commission=fill.commission - (fill.commission * share),
        )
        result.open_positions[key] = _opened(
            tail, commission - closing_commission
        )
        return

    _absorb(position, fill, qty=fill.qty, commission=commission)
    position.signed_qty = after

    if after == ZERO:
        result.trades.append(_closed(position, fill))
        del result.open_positions[key]


def _crosses_zero(before: Decimal, after: Decimal) -> bool:
    """Позиция сменила знак, не остановившись на нуле."""
    return before != ZERO and after != ZERO and (before > ZERO) != (after > ZERO)


def _opened(fill: Fill, commission: Decimal) -> OpenPosition:
    long_side = fill.signed_qty > ZERO
    return OpenPosition(
        symbol=fill.symbol,
        position_side=fill.position_side,
        side="long" if long_side else "short",
        qty=fill.qty,
        exit_qty=ZERO,
        signed_qty=fill.signed_qty,
        open_time=fill.trade_time,
        entry_cost=fill.price * fill.qty,
        entry_qty=fill.qty,
        realized_pnl=fill.realized_pnl,
        commission_usdt=commission,
        last_fill_id=fill.external_id,
        first_fill_id=fill.external_id,
        fill_ids=[fill.external_id],
    )


def _absorb(
    position: OpenPosition, fill: Fill, *, qty: Decimal, commission: Decimal
) -> None:
    """Вобрать филл в открытую позицию.

    Доливка двигает средневзвешенный вход, частичный выход — нет: цена входа
    это про то, по чём вошли, и выход её не меняет. Отличаем по знаку: филл
    в сторону позиции наращивает её, встречный сокращает.
    """
    adding = (fill.signed_qty > ZERO) == (position.signed_qty > ZERO)
    if adding:
        position.qty += qty
        position.entry_cost += fill.price * qty
        position.entry_qty += qty
    else:
        position.exit_qty += qty
    position.realized_pnl += fill.realized_pnl
    position.commission_usdt += commission
    position.last_fill_id = fill.external_id
    position.fill_ids.append(fill.external_id)


def _closed(position: OpenPosition, fill: Fill) -> AggregatedTrade:
    return AggregatedTrade(
        symbol=position.symbol,
        position_side=position.position_side,
        side=position.side,
        open_time=position.open_time,
        close_time=fill.trade_time,
        qty=position.qty,
        exit_qty=position.exit_qty,
        entry_price=position.entry_price,
        realized_pnl=position.realized_pnl,
        commission_usdt=position.commission_usdt,
        funding=ZERO,
        first_fill_id=position.first_fill_id,
        last_fill_id=position.last_fill_id,
        fill_ids=tuple(position.fill_ids),
    )


# --- фандинг и проценты ---


@dataclass(frozen=True)
class IncomeRow:
    """Начисление из `GET /fapi/v1/income`."""

    symbol: str | None
    income_type: str
    income: Decimal
    asset: str
    happened_at: dt.datetime
    external_id: int | None = None


FUNDING = "FUNDING_FEE"


def apply_funding(
    trades: list[AggregatedTrade], income: list[IncomeRow]
) -> list[AggregatedTrade]:
    """Разложить фандинг по сделкам, которые были открыты в момент списания.

    Фандинг списывается с позиции, а не со сделки, поэтому привязать его можно
    только по времени и символу. Начисление вне всякой сделки (позиция была
    открыта до подключения) просто никуда не попадёт — это честнее, чем
    приписать его ближайшей по времени.
    """
    if not income:
        return trades
    rows = [r for r in income if r.income_type == FUNDING and r.asset == COMMISSION_ASSET]
    if not rows:
        return trades

    out: list[AggregatedTrade] = []
    for trade in trades:
        total = sum(
            (
                row.income
                for row in rows
                if row.symbol == trade.symbol
                and trade.open_time <= row.happened_at <= trade.close_time
            ),
            ZERO,
        )
        out.append(replace(trade, funding=total) if total else trade)
    return out


def return_pct(profit_usd: Decimal, balance: Decimal | None) -> Decimal | None:
    """Результат сделки в процентах от депозита.

    Здесь Binance лучше TMM: баланс настоящий, а не восстановленный из
    процентов, посчитанных каждый от своей базы (ТЗ 4.6). Нет снимка баланса —
    возвращаем None, а не ноль: ноль означал бы «сделка ничего не изменила».
    """
    if balance is None or balance <= ZERO:
        return None
    return (profit_usd / balance) * Decimal(100)
