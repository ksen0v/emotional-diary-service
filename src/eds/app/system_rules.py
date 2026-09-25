"""Системные триггеры SR-1…SR-4 в работе (ТЗ 6.5).

Их условия тремя показателями ТЗ 6.3 не выражаются — тег нарушения, сделка
при блокировке, торговля без допуска, неразмеченная сделка, — поэтому в базе
`conditions` у них пустые, а условие живёт здесь, в коде обработчика
(Архитектура ч.1 §7).

Обработчик стоит в оркестрации, а не в модуле `rules`, потому что каждый из
четырёх соединяет модули, которые не имеют права знать друг о друге: разметку
из `trades`, допуск и сессию из `daybook`, само правило из `rules` и инцидент
из `incidents`. Модуль `rules` по-прежнему отвечает только на вопрос
«выполнились ли условия» — просто у системных триггеров этот вопрос задаётся
не счётчикам дня, а фактам.

Идемпотентность у всех четырёх одна и та же и стоит в базе дважды: журнал
проверок `rules.evaluations` по ключу `(rule_id, trigger_ref)` и уникальность
инцидента по сделке. Это обязательно: одно и то же событие приходит и из
потока, и из сверки, и повторный проход не должен ни задваивать инциденты,
ни слать второй алерт.

Ветка позднего тега живёт отдельно, в `app/retro.py`: у неё то же условие,
но другой жизненный цикл — окна, которое можно соблюсти, уже нет, и остаётся
только узнать, было ли оно соблюдено. Блокировки она не ставит ни в одной
своей ветке (ТЗ 4.4).

**Чего здесь сознательно нет:**

- **Сигнала доверенному лицу.** SR-2 и SR-3 обязаны его послать (ТЗ 6.5), но
  контакта с двойным согласием ещё не существует. Алерт идёт в лог,
  и ни один экран про отправленный сигнал не говорит.
"""

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import retro
from eds.app.retro import RetroTag
from eds.contracts.rules import Firing, OpenTrade
from eds.contracts.trading_time import day_ends_at, trading_day
from eds.modules.daybook import questions
from eds.modules.daybook import repo as daybook_repo
from eds.modules.incidents import service as incidents
from eds.modules.incidents.models import (
    CODE_LOCK_BREACHED,
    CODE_NO_ADMISSION,
    CODE_VIOLATION,
    LockRow,
)
from eds.modules.rules import repo as rules_repo
from eds.modules.rules import service as rules_service
from eds.modules.rules import system as sysrules
from eds.modules.rules.models import RuleRow
from eds.modules.trades import repo as trades_repo
from eds.platform import auth

log = logging.getLogger("eds.system_rules")


@dataclass
class Sr1Result:
    """Чем кончился проход SR-1 по дню.

    Два поля, потому что у SR-1 две взаимоисключающие ветки, и склеивать их
    в одно число нельзя: блокировка — это то, что происходит сейчас,
    ретропроверка — то, что выяснилось про прошлое. На приёмке спрашивают
    про них по отдельности.
    """

    locks: list[LockRow] = field(default_factory=list)
    # Тип импортирован именем, а не через модуль: поле называется так же,
    # как модуль, и внутри тела класса `retro` — это уже поле.
    retro: list[RetroTag] = field(default_factory=list)

    @property
    def fired(self) -> int:
        return len(self.locks) + len(self.retro)


async def _rule(
    s: AsyncSession, user_id: uuid.UUID, code: str
) -> RuleRow | None:
    """Системное правило пользователя.

    Досоздание вызывается здесь же, а не только на чтении списка правил:
    триггер обязан сработать у трейдера, который ни разу не открывал раздел
    «Правила». Вызов идемпотентен ограничением базы, поэтому лишний проход
    ничего не стоит и ничего не создаёт дважды.
    """
    await rules_service.ensure_system_rules(s, user_id)
    return await rules_repo.by_code(s, user_id, code)


def _firing(
    rule: RuleRow,
    *,
    day: dt.date,
    trigger_ref: str,
    trade_id: uuid.UUID | None,
    snapshot: dict[str, Any],
) -> Firing:
    """Срабатывание системного триггера в той же форме, что и у своего правила.

    Одна форма, потому что дальше путь общий: инцидент, блокировка, исход.
    У системного триггера другое условие, а не другой жизненный цикл, и
    заводить ему второй путь значило бы завести второе место, где чинить
    блокировки.
    """
    return Firing(
        rule_id=rule.id,
        rule_name=rule.name,
        rule_text=rules_service.rule_out(rule)["human_text"],
        rule_version=rule.version,
        day=day,
        trigger_ref=trigger_ref,
        trade_id=trade_id,
        actions=dict(rule.actions),
        unlock=dict(rule.unlock),
        snapshot=snapshot,
    )


async def _claim(
    s: AsyncSession,
    user_id: uuid.UUID,
    rule: RuleRow,
    day: dt.date,
    trigger_ref: str,
    snapshot: dict[str, Any],
) -> bool:
    """Занять повод в журнале проверок. False — его уже обработали.

    Тот же механизм, что у пользовательских правил: уникальность
    `(rule_id, trigger_ref)` стоит в базе, поэтому два одновременных прохода
    не создадут двух инцидентов и не пошлют двух алертов.
    """
    return await rules_repo.record_evaluation(
        s,
        user_id,
        rule.id,
        day,
        trigger_ref=trigger_ref,
        fired=True,
        snapshot=snapshot,
    )


# --- SR-1: тег нарушения ---


async def sr1_violations(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    day: dt.date,
    *,
    now: dt.datetime,
    today: dt.date | None = None,
) -> Sr1Result:
    """Сделка отмечена нарушением → блокировка до конца её дня или ретропроверка.

    Блокировка принадлежит торговому дню сделки и держится до конца этого дня
    (ТЗ 4.4). Отсюда единственная проверка, которая здесь важна: **окно дня
    ещё открыто или уже закрылось.**

    Окно открыто — блокировка включается сразу, до границы дня, и compliance
    идёт в реальном времени. Окно закрыто — блокировки нет, и вместо неё идёт
    ретропроверка того дня: были ли сделки после размеченной. Исход у неё
    любой из двух, инцидент записывается в тот прошлый день, а серия
    пересчитывается от него и до вчера (`app/retro.py`).

    Одно и то же условие, две развязки — и держать их рядом важнее, чем
    разложить по файлам: вопрос «а что будет, если тег придёт позже» задаётся
    именно здесь, и ответ должен быть виден на этом же экране.
    """
    window_until = day_ends_at(day, prefs.timezone, prefs.day_cutoff)

    rule = await _rule(s, user_id, sysrules.SR1)
    if rule is None:
        return Sr1Result()

    if now >= window_until:
        # Поздний тег: окно того дня истекло. Блокировки нет ни в одной ветке.
        return Sr1Result(
            retro=await retro.run(
                s,
                user_id,
                prefs,
                rule,
                day,
                now=now,
                today=today or trading_day(now, prefs.timezone, prefs.day_cutoff),
            )
        )

    started: list[LockRow] = []
    for trade_id, symbol, open_time, close_time in await trades_repo.violations_of_day(
        s, user_id, day
    ):
        trigger_ref = f"marking:{trade_id}"
        snapshot = {
            "symbol": symbol,
            "open_time": open_time.isoformat(),
            "close_time": close_time.isoformat() if close_time else None,
        }
        if not await _claim(s, user_id, rule, day, trigger_ref, snapshot):
            continue

        _incident, lock = await incidents.open_from_firing(
            s,
            user_id,
            _firing(
                rule,
                day=day,
                trigger_ref=trigger_ref,
                trade_id=trade_id,
                snapshot=snapshot,
            ),
            window_until=window_until,
            shadow=prefs.shadow_mode,
            now=now,
            code=CODE_VIOLATION,
            extra={
                "system_code": sysrules.SR1,
                "symbol": symbol,
                "trade_open_time": open_time.isoformat(),
            },
        )
        if lock is not None:
            started.append(lock)
            log.info("SR-1: блокировка %s по сделке %s", lock.id, symbol)
    return Sr1Result(locks=started)


# --- SR-2: сделка во время блокировки ---


async def sr2_breaches(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    lock: LockRow,
    trades: list[OpenTrade],
    *,
    now: dt.datetime,
) -> int:
    """Сделка открыта во время активной блокировки → отдельный инцидент.

    Отдельный, а не только исход у той блокировки, которую нарушили: в
    прототипе `Incidents.dc.html` это две строки — «Несистемная сделка»
    с исходом «нарушено» и рядом «Сделка во время блокировки». И это верно
    по смыслу: нарушенная блокировка отвечает на вопрос «чем кончился тот
    инцидент», а SR-2 — на вопрос «что трейдер сделал». Второе тяжелее
    первого и в истории должно стоять само по себе (ТЗ 6.5).

    По инциденту на каждую сделку внутри окна, а не один на блокировку:
    три сделки в тильте — это три нарушения, и склеивать их в одно значило бы
    показать в истории меньше, чем было.

    Стрик рвётся отдельно и не здесь: день с нарушенной блокировкой не
    зачитывается по правилу ТЗ 7.1, и считает это модуль `streaks` по числу
    таких инцидентов за день.
    """
    if not trades:
        return 0
    rule = await _rule(s, user_id, sysrules.SR2)
    if rule is None:
        return 0

    opened = 0
    for trade in trades:
        trigger_ref = f"breach:{trade.trade_id}"
        snapshot = {
            "symbol": trade.symbol,
            "open_time": trade.open_time.isoformat(),
            "lock_id": str(lock.id),
            "lock_rule_name": lock.rule_name,
        }
        if not await _claim(s, user_id, rule, lock.day, trigger_ref, snapshot):
            continue

        incident = await incidents.open_fact(
            s,
            user_id,
            code=CODE_LOCK_BREACHED,
            day=lock.day,
            rule_id=rule.id,
            rule_name=rule.name,
            rule_text=rules_service.rule_out(rule)["human_text"],
            details={
                "system_code": sysrules.SR2,
                "trade_id": str(trade.trade_id),
                "symbol": trade.symbol,
                "trade_open_time": trade.open_time.isoformat(),
                "breached_lock_id": str(lock.id),
                "breached_rule_name": lock.rule_name,
                "snapshot": snapshot,
            },
            shadow=prefs.shadow_mode,
            now=now,
        )
        if incident is not None:
            opened += 1
            log.info(
                "SR-2: сделка %s открыта в %s во время блокировки %s",
                trade.symbol,
                trade.open_time,
                lock.id,
            )
    return opened


# --- SR-3: торговля без допуска ---


async def sr3_no_admission(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    day: dt.date,
    *,
    now: dt.datetime,
) -> bool:
    """Есть сделки, а пройденного чека нет → инцидент «торговля без допуска».

    Два разных входа в один инцидент, и путать их нельзя:

    - `admission = denied` — чек пройден, балл ниже нижнего порога, сессия
      не открывается (ТЗ 5.2);
    - чека нет вовсе — трейдер сел торговать, не открыв сессию. ТЗ 5.2
      говорит про этот случай «то же самое», поэтому инцидент тот же.

    Блокировки у SR-3 нет. ТЗ 5.2 и 6.5 перечисляют его действия дважды и
    блокировки не называют ни разу: день без допуска уже закрыт экраном
    «допуска на сегодня нет», и вторая закрытая дверь поверх первой ничего
    не добавляет. Расхождение с карточкой SR-3 в прототипе `RuleBuilder`,
    где стоит чип «разбор», закрыто в пользу ТЗ и названо вслух — решать
    Владу, вернуть его обратно стоит одну строку в `rules/system.py`.

    Исход известен сразу: соблюдать нечего, торговля уже состоялась, поэтому
    инцидент закрывается как `нарушено` (ТЗ 9.2 — исходов два).

    Стрик: день со сделками без допуска не зачитывается по правилу ТЗ 7.1.
    Сегодняшний день в стрике не участвует, поэтому сброс виден не сегодня,
    а на следующий день. Это не баг: стрик всегда считается по завершённым
    дням, и сегодняшнее нарушение может случиться ещё раз через минуту.
    """
    summary = (await trades_repo.day_summaries(s, user_id, day, day)).get(day)
    if not summary or summary["trades"] == 0:
        return False

    day_row = await daybook_repo.day_of(s, user_id, day)
    admission = day_row.admission if day_row else None
    if admission not in (None, "denied"):
        return False

    rule = await _rule(s, user_id, sysrules.SR3)
    if rule is None:
        return False

    check = await daybook_repo.check_of(s, user_id, day)
    snapshot = {
        "trades": summary["trades"],
        "admission": admission,
        "score": check.score if check else None,
        "min_score": prefs.min_score,
        "max_score": questions.MAX_SCORE,
    }
    trigger_ref = f"no_admission:{day.isoformat()}"
    if not await _claim(s, user_id, rule, day, trigger_ref, snapshot):
        # Инцидент за этот день уже записан. Число сделок в нём осталось
        # тем, каким было в момент записи, и это правильно: история не
        # переписывается (ТЗ 9.2).
        return False

    incident = await incidents.open_fact(
        s,
        user_id,
        code=CODE_NO_ADMISSION,
        day=day,
        rule_id=rule.id,
        rule_name=rule.name,
        rule_text=rules_service.rule_out(rule)["human_text"],
        details={
            "system_code": sysrules.SR3,
            "trade_id": None,
            "snapshot": snapshot,
        },
        shadow=prefs.shadow_mode,
        now=now,
    )
    if incident is None:
        return False
    log.info(
        "SR-3: %s сделок без допуска за %s (допуск %s)",
        summary["trades"],
        day,
        admission or "чека не было",
    )
    return True


# --- SR-4: неразмеченная сделка ---


async def sr4_unmarked(
    s: AsyncSession,
    user_id: uuid.UUID,
    day: dt.date,
    *,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    """Сделка закрыта и не размечена дольше N минут → напоминание.

    Напоминание, а не инцидент. Инцидент по ТЗ 2 — это нарушение,
    срабатывание триггера, нарушение блокировки или торговля без допуска;
    забытая разметка не из этого списка, и запись её в историю засорила бы
    ленту тем, что закрывается одним кликом.

    Отсчёт идёт от времени ЗАКРЫТИЯ сделки: именно тогда её стало можно
    разметить. N берётся из настроек самого триггера (`remind_after_minutes`,
    по умолчанию 15 — ТЗ 6.5), а не из константы: это редактируемое поле.

    Алерт уходит в лог, пока нет Telegram, а на экране «Сегодня» появляется строка
    в `attention`. Возвращается список сделок — из него собирается и то,
    и другое.
    """
    rule = await _rule(s, user_id, sysrules.SR4)
    if rule is None:
        return []
    minutes = (rule.actions or {}).get("remind_after_minutes")
    if not minutes:
        return []

    cutoff = now - dt.timedelta(minutes=int(minutes))
    overdue = await trades_repo.unmarked_closed_before(s, user_id, day, cutoff)
    if not overdue:
        return []

    out: list[dict[str, Any]] = []
    for trade_id, symbol, close_time in overdue:
        out.append(
            {
                "trade_id": str(trade_id),
                "symbol": symbol,
                "close_time": close_time.isoformat() if close_time else None,
            }
        )
        trigger_ref = f"unmarked:{trade_id}"
        # Алерт на сделку один: повод записывается в журнал проверок, и
        # второй раз о той же сделке сервис не напомнит. Иначе экран
        # «Сегодня», который читается раз в минуту, слал бы напоминание
        # каждую минуту — и его перестали бы читать.
        if await _claim(
            s,
            user_id,
            rule,
            day,
            trigger_ref,
            {"symbol": symbol, "minutes": int(minutes)},
        ):
            log.info(
                "SR-4 (Telegram ещё не подключён): сделка %s без разметки "
                "дольше %s минут",
                symbol,
                minutes,
            )
    return out
