"""Тексты уведомлений: каталог, дефолты, проверка и подстановка.

Тексты пишет трейдер (ТЗ 9.5, Архитектура ч.2 §3.10). Сервер по-прежнему
отдаёт готовую строку — фронт ничего не собирает, — но собирает её из шаблона
пользователя, а не из константы в коде.

**Что проверяется и что нет.** Проверяется только техническая корректность:
известные подстановки, целые скобки, непустая строка не длиннее 500 символов.
Смысл написанного — дело трейдера: что написал, то и придёт. Никаких проверок
на осмысленность, никакого отката на дефолт, никаких «вы уверены». Причина
простая: это сообщение человека самому себе, и сервис, который правит такие
сообщения, ведёт себя как надзиратель. Плюс любая проверка «на осмысленность»
ошибается: «Хватит.» — отличный текст, который отсеяла бы любая эвристика.

**Единственное жёсткое ограничение — ТЗ 9.5: в сообщениях доверенному лицу
сумм нет.** Ни подстановкой, ни вручную. У buddy-шаблонов денежных подстановок
нет в списке разрешённых, а попытка вписать их руками отклоняется отдельным
кодом ошибки, а не общим «неизвестная подстановка»: причина у отказа другая,
и трейдер должен прочитать именно её.

**Дефолты — не заглушки.** Большинство никогда не откроет этот раздел, и для
них дефолт и есть голос сервиса. Тексты и состав каталога взяты из прототипа
`NotifyTexts.dc.html` дословно.
"""

from dataclasses import dataclass
from typing import Any

from eds.modules.notifications.models import BUDDY, SELF
from eds.platform.errors import UNPROCESSABLE, AppError

BODY_MAX = 500

# Денежные подстановки. Список нужен, чтобы отказ доверенному лицу назывался
# своим именем. Здесь и те, которых в каталоге нет вовсе: трейдер вписывает
# руками, и «{pnl}» он напишет с той же вероятностью, что «{profit_usd}».
MONEY_PLACEHOLDERS = frozenset(
    {
        "profit_usd",
        "profit",
        "pnl",
        "percent",
        "equity_pct",
        "loss_sum_pct",
        "drawdown_pct",
        "unrealized_pct",
        "unrealized_usd",
        "account_return_pct",
        "size_usd",
        "emotion_cost_usd",
        "balance",
    }
)


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    # Когда отправляется — подпись рядом с названием события на экране.
    when: str
    default: str
    placeholders: tuple[str, ...]
    # Кому придёт — строка под предпросмотром.
    to: str
    channel: str
    buddy: bool = False


CATALOG: tuple[Template, ...] = (
    Template(
        key="violation_detected",
        name="Сделка отмечена нарушением",
        when="сразу после тега",
        default=(
            "Сделка {symbol} в {open_time} отмечена как нарушение. "
            "Результат: {profit_usd}."
        ),
        placeholders=("symbol", "open_time", "profit_usd", "day"),
        to="Тебе в Telegram",
        channel=SELF,
    ),
    # Двенадцатый ключ, которого в прототипе нет, и это названное отступление.
    # Причина: ТЗ 6.4 даёт правилу действие «алерт трейдеру» отдельно от
    # блокировки, и фраза правила обещает «придёт алерт в Telegram». Правило
    # только с алертом не начинает блокировку, а значит ни один из одиннадцати
    # текстов прототипа ему не подходит — и обещание осталось бы невыполненным.
    Template(
        key="rule_fired",
        name="Сработало правило",
        when="когда у правила только алерт",
        default="Сработало: {rule_name}.",
        placeholders=("rule_name", "day"),
        to="Тебе в Telegram",
        channel=SELF,
    ),
    Template(
        key="lock_started",
        name="Началась блокировка",
        when="в момент срабатывания правила",
        default="Сработало: {rule_name}. Блокировка до {until}.",
        placeholders=("rule_name", "minutes", "until", "day"),
        to="Тебе в Telegram и на экран",
        channel=SELF,
    ),
    Template(
        key="lock_lifted",
        name="Блокировка снята",
        when="когда условия выполнены",
        default="Блокировка снята: {reason}.",
        placeholders=("rule_name", "reason", "day"),
        to="Тебе в Telegram",
        channel=SELF,
    ),
    Template(
        key="lock_breached",
        name="Сделка во время блокировки",
        when="сразу",
        default=(
            "Открыта сделка {symbol} в {open_time} во время блокировки. "
            "Инцидент записан как нарушенный."
        ),
        placeholders=("symbol", "open_time", "rule_name"),
        to="Тебе в Telegram",
        channel=SELF,
    ),
    Template(
        key="no_admission",
        name="Допуска нет",
        when="после чека",
        default=(
            "Допуска на сегодня нет. {score} из 25, порог {min_score}. "
            "Сессия не открыта."
        ),
        placeholders=("score", "min_score", "day"),
        to="Тебе в Telegram",
        channel=SELF,
    ),
    Template(
        key="retro_violation",
        name="Поздний тег закрыл прошлый день",
        when="при появлении тега",
        default=(
            "Тег зафиксировал нарушение за {day}. "
            "Стрик пересчитан: {streak_before} → {streak_after}."
        ),
        placeholders=("day", "streak_before", "streak_after"),
        to="Тебе в Telegram",
        channel=SELF,
    ),
    Template(
        key="streak_broken",
        name="Стрик оборван",
        when="на границе дня",
        default="Стрик оборван: {reason}. Было дней: {streak_before}.",
        placeholders=("streak_before", "reason", "day"),
        to="Тебе в Telegram",
        channel=SELF,
    ),
    Template(
        key="unmarked_reminder",
        name="Сделки без разметки",
        when="через N минут",
        default="{count} сделок без разметки дольше {minutes} минут.",
        placeholders=("count", "minutes"),
        to="Тебе в Telegram",
        channel=SELF,
    ),
    # Этот текст говорит прямо, что правила не работают. Соблазн смягчить
    # («возможны задержки») велик, но именно он защищает от худшего сценария:
    # трейдер уверен, что блокировки его прикрывают, а поток умер два часа назад.
    Template(
        key="sync_lost",
        name="Синхронизация молчит",
        when="при открытой сессии",
        default="Синхронизация молчит {minutes} минут. Правила сейчас не работают.",
        placeholders=("minutes",),
        to="Тебе в Telegram",
        channel=SELF,
    ),
    Template(
        key="buddy_signal",
        name="Сигнал доверенному лицу",
        when="при нарушении",
        default=(
            "{trader_name} нарушил своё правило и, возможно, сейчас в тильте. "
            "Сработало: {rule_name}. Свяжись с ним и попроси закрыть терминал."
        ),
        placeholders=("trader_name", "rule_name", "day"),
        to="Доверенному лицу в Telegram",
        channel=BUDDY,
        buddy=True,
    ),
    # Здесь условие, а не просьба: «подтверди, только если поговорил с ним».
    # Без этой фразы друг нажмёт кнопку из вежливости, и внешний контроль
    # превратится в кнопку «снять блокировку», до которой трейдер додумается
    # на второй раз.
    Template(
        key="buddy_confirm_request",
        name="Просьба подтвердить снятие",
        when="по кнопке",
        default=(
            "{trader_name} просит подтвердить снятие блокировки. "
            "Сработало: {rule_name}. Подтверди, только если поговорил с ним."
        ),
        placeholders=("trader_name", "rule_name"),
        to="Доверенному лицу в Telegram",
        channel=BUDDY,
        buddy=True,
    ),
)

BY_KEY: dict[str, Template] = {t.key: t for t in CATALOG}

# Данные предпросмотра — из прототипа дословно. Предпросмотр нужен потому, что
# иначе трейдер увидит свой текст первый раз в момент блокировки, то есть
# в худший момент для правок.
SAMPLES: dict[str, str] = {
    "symbol": "BTCUSDT",
    "open_time": "14:31",
    "profit_usd": "−$84.20",
    "day": "19 сентября",
    "rule_name": "2 стопа подряд",
    "minutes": "30",
    "until": "15:12",
    "reason": "условия выполнены",
    "score": "11",
    "min_score": "13",
    "streak_before": "14",
    "streak_after": "1",
    "count": "3",
    "trader_name": "Владислав",
}


class PlaceholderMissing(RuntimeError):
    """Продюсер не дал значения для подстановки из шаблона.

    Это ошибка кода, а не трейдера: шаблон с неизвестной подстановкой
    в базу не попадает, значит значение обязан дать тот, кто ставит
    уведомление в очередь. Проверяется тестом на каждый ключ каталога.
    """


def _bad(code: str, message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(code, message, UNPROCESSABLE, details)


def tokens(body: str) -> list[str]:
    """Подстановки в порядке появления. Заодно проверка целости скобок."""
    found: list[str] = []
    depth = 0
    current = ""
    for char in body:
        if char == "{":
            if depth:
                raise _bad("malformed_template", "В тексте открытая скобка внутри {}.")
            depth = 1
            current = ""
            continue
        if char == "}":
            if not depth:
                raise _bad("malformed_template", "В тексте закрывающая скобка без открывающей.")
            depth = 0
            name = current.strip()
            if not name:
                raise _bad("malformed_template", "В тексте пустая подстановка {}.")
            found.append(name)
            continue
        if depth:
            current += char
    if depth:
        raise _bad("malformed_template", "В тексте незакрытая скобка {.")
    return found


def validate(key: str, body: str) -> str:
    """Проверить текст шаблона. Возвращает его же, без изменений."""
    template = BY_KEY.get(key)
    if template is None:
        raise _bad("not_found", f"Событие «{key}» неизвестно.", {"key": key})
    return validate_body(body, allowed=template.placeholders, buddy=template.buddy)


def validate_body(body: str, *, allowed: tuple[str, ...], buddy: bool) -> str:
    cleaned = (body or "").strip()
    if not cleaned:
        raise _bad("validation_failed", "Текст уведомления не может быть пустым.")
    if len(cleaned) > BODY_MAX:
        raise _bad(
            "validation_failed",
            f"Текст длиннее {BODY_MAX} символов.",
            {"max": BODY_MAX, "given": len(cleaned)},
        )
    unknown = [name for name in tokens(cleaned) if name not in allowed]
    if unknown:
        money = [name for name in unknown if name in MONEY_PLACEHOLDERS]
        if buddy and money:
            raise _bad(
                "forbidden_placeholder",
                "В сообщении доверенному лицу сумм нет: друг видит факт, "
                "а не финансы. Убери " + ", ".join(f"{{{n}}}" for n in money) + ".",
                {"forbidden": money},
            )
        raise _bad(
            "unknown_placeholder",
            "Такой подстановки у этого события нет: "
            + ", ".join(f"{{{n}}}" for n in unknown)
            + ". Доступные — под полем.",
            {"unknown": unknown, "available": list(allowed)},
        )
    return cleaned


def render(body: str, values: dict[str, Any]) -> str:
    """Подставить значения. Ветки «если текст плохой» здесь нет.

    Либо подставляет и отправляет, либо падает на подстановке, для которой
    не дали значения. Такой шаблон в базу не попадёт — валидация не пустит, —
    поэтому падение здесь означает ошибку продюсера, а не трейдера.
    """
    out = body
    for name in tokens(body):
        if name not in values:
            raise PlaceholderMissing(name)
        out = out.replace("{" + name + "}", str(values[name]))
    return out


def preview(body: str, *, extra: dict[str, Any] | None = None) -> tuple[str, dict]:
    """Предпросмотр на подставных данных: текст и сами данные."""
    sample = dict(SAMPLES)
    if extra:
        sample.update(extra)
    used = {name: sample.get(name, "—") for name in tokens(body)}
    return render(body, {**sample, **used}), used
