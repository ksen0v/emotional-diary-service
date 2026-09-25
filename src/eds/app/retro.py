"""Поздний тег: ретропроверка закрытого дня и ретропересчёт (ТЗ 4.4).

Самая тяжёлая логика сервиса, и тяжесть у неё не в алгоритме, а в том, что
она меняет прошлое. Тег, поставленный после конца торгового дня сделки,
разбирает уже закрытый день, а серия дней считается до сегодня — значит
изменение одного дня в середине истории переписывает всё, что после него.

Живёт в оркестрации по той же причине, что и остальные системные триггеры:
соединяет разметку из `trades`, правило из `rules`, инцидент из `incidents`
и отметки дней из `streaks`. Отдельным файлом, а не внутри `system_rules.py`,
потому что это не ещё одно условие, а второй жизненный цикл SR-1: у живого
тега есть окно, которое можно соблюсти, у позднего окна уже нет и остаётся
только узнать, было ли оно соблюдено.

**Чего здесь нет и не будет:**

- **Блокировки.** Ни сегодняшней, ни «задним числом». Окно истекло (ТЗ 4.4),
  и это не формальность: поздняя разметка и без блокировки обходится дороже
  живой, потому что ретропроверка покрывает весь остаток того дня, а не
  тридцать минут.
- **Правок уже записанного.** Инциденты только добавляются, даже если
  ретропроверка противоречит более раннему выводу (ТЗ 9.2). Ни один
  инцидент здесь не обновляется и не удаляется.
- **Сегодняшнего дня.** Поздний тег не трогает ни счётчики сегодня, ни
  допуск, ни активную блокировку. Пересчёт идёт от дня сделки и до
  вчерашнего включительно — сегодняшний день в серии не участвует вообще.
- **Отправки уведомления.** Текст собирается здесь и уходит в журнал.
  Telegram — шаг 13.

**Что важно знать про стрик, потому что это неочевидно и легко прочитать
как ошибку.** День с поздним тегом не зачитывается **в обеих ветках**, а не
только в нарушенной: тег — это нарушение, а день с нарушением не зачитывается
по ТЗ 7.1 и без всякой ретропроверки, с шага 7. Исход инцидента отвечает на
другой вопрос — торговал ли трейдер после той сделки, то есть соблюл ли он
окно, которого уже не видел. Поэтому `соблюдено` здесь **не значит**
«стрик уцелел»: оно значит «сверх самого тега трейдер ничего не сделал».
"""

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import streaks as app_streaks
from eds.modules.incidents import service as incidents
from eds.modules.incidents.models import BREACHED, CODE_RETRO_TAG, KEPT
from eds.modules.rules import repo as rules_repo
from eds.modules.rules import service as rules_service
from eds.modules.rules import system as sysrules
from eds.modules.rules.models import RuleRow
from eds.modules.streaks import repo as streaks_repo
from eds.modules.trades import repo as trades_repo
from eds.platform import auth

log = logging.getLogger("eds.retro")


@dataclass
class RetroTag:
    """Результат ретропроверки одной размеченной сделки закрытого дня."""

    trade_id: uuid.UUID
    symbol: str
    day: dt.date
    # Граница окна: время закрытия сделки, после которого торговать было нельзя.
    edge: dt.datetime
    breached: bool
    trades_after: int
    incident_id: uuid.UUID

    def as_dict(self) -> dict:
        return {
            "trade_id": str(self.trade_id),
            "symbol": self.symbol,
            "day": self.day.isoformat(),
            "outcome": BREACHED if self.breached else KEPT,
            "trades_after": self.trades_after,
            "incident_id": str(self.incident_id),
        }


async def late_tags(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    rule: RuleRow,
    day: dt.date,
    *,
    now: dt.datetime,
) -> list[RetroTag]:
    """Ретропроверка размеченных сделок закрытого дня (Архитектура ч.1 §7).

    Алгоритм ровно такой, как в архитектуре: взять сделки того дня, открытые
    позже закрытия размеченной, и записать инцидент с исходом — были такие
    сделки или нет. Блокировки нет ни в одной ветке.

    Живой тег сюда не попадает. Проверка стоит по журналу проверок: если у
    этой сделки уже есть повод `marking:<id>`, значит SR-1 разобрал её, пока
    окно было открыто, и инцидент с блокировкой уже записан. Без этой
    проверки сверка, принёсшая вчерашний день, записала бы второй инцидент
    на то же событие — и лента показала бы трейдеру два нарушения там, где
    было одно.
    """
    done = await rules_repo.evaluations_of_day(s, user_id, day)
    out: list[RetroTag] = []

    for trade_id, symbol, open_time, close_time in await trades_repo.violations_of_day(
        s, user_id, day
    ):
        if (rule.id, f"marking:{trade_id}") in done:
            continue

        # Окно считается от закрытия сделки (ТЗ 4.4). Сделки без времени
        # закрытия у закрытого дня быть не должно, но если она есть, окном
        # становится открытие: так выборка не станет шире, чем надо.
        edge = close_time or open_time
        after = await trades_repo.opened_after_in_day(s, user_id, day, edge)

        trigger_ref = f"retro:{trade_id}"
        snapshot = {
            "symbol": symbol,
            "open_time": open_time.isoformat(),
            "close_time": close_time.isoformat() if close_time else None,
            "tagged_at": now.isoformat(),
            "trades_after": len(after),
        }
        if not await rules_repo.record_evaluation(
            s, user_id, rule.id, day, trigger_ref=trigger_ref, fired=True, snapshot=snapshot
        ):
            continue

        incident = await incidents.open_fact(
            s,
            user_id,
            code=CODE_RETRO_TAG,
            day=day,
            rule_id=rule.id,
            rule_name=rule.name,
            rule_text=rules_service.rule_out(rule)["human_text"],
            details={
                "system_code": sysrules.SR1,
                "trade_id": str(trade_id),
                "symbol": symbol,
                "trade_open_time": open_time.isoformat(),
                "trade_close_time": close_time.isoformat() if close_time else None,
                "tagged_at": now.isoformat(),
                "retro": True,
                "not_locked": "day_window_closed",
                "trades_after": [
                    {
                        "trade_id": str(row[0]),
                        "symbol": row[1],
                        "open_time": row[2].isoformat(),
                    }
                    for row in after
                ],
                "snapshot": snapshot,
            },
            shadow=prefs.shadow_mode,
            now=now,
            # Алерт только там, где есть о чём: ретропроверка без сделок
            # после тега — это «обошлось», и будить этим трейдера не за что.
            alert=bool(after),
            outcome=BREACHED if after else KEPT,
        )
        if incident is None:
            # Инцидент уже записан: повод занят другим проходом.
            continue

        out.append(
            RetroTag(
                trade_id=trade_id,
                symbol=symbol,
                day=day,
                edge=edge,
                breached=bool(after),
                trades_after=len(after),
                incident_id=incident.id,
            )
        )
        log.info(
            "ретропроверка %s за %s: сделок после %s — %s, исход %s",
            symbol,
            day,
            edge,
            len(after),
            BREACHED if after else KEPT,
        )
    return out


async def recount(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    day: dt.date,
    *,
    today: dt.date,
) -> tuple[int, int]:
    """Пересчитать отметки и серию от изменённого дня до вчера. Было → стало.

    Окно начинается с самого дня, а не с «шестидесяти дней назад»: поздний
    тег может прийти на день старше обычного окна пересчёта, и тогда
    пересчёт по умолчанию прошёл бы мимо него.

    Единственный путь пересчёта — `day_marks`: серия не хранится
    инкрементально именно для того, чтобы изменение дня в середине истории
    правильно переписывало всё, что после него (Архитектура ч.2 §4.4). Здесь
    легче всего получить стрик, который «скачет» между перезагрузками, и
    защита от этого одна — не пытаться поправить серию с конца.

    Сегодня в окно не входит: день ещё идёт, и оценивать его нечестно.
    """
    state = await streaks_repo.state_of(s, user_id)
    before = state.current if state else 0

    span = (today - day).days
    window = [day + dt.timedelta(days=offset) for offset in range(max(span, 0))]
    state = await app_streaks.refresh(s, user_id, prefs, today=today, days=window)
    return before, state.current


async def run(
    s: AsyncSession,
    user_id: uuid.UUID,
    prefs: auth.UserPrefs,
    rule: RuleRow,
    day: dt.date,
    *,
    now: dt.datetime,
    today: dt.date,
) -> list[RetroTag]:
    """Ретропроверка и ретропересчёт одним проходом.

    Порядок важен: сначала инциденты, потом пересчёт. Наоборот числа «было →
    стало» в уведомлении описывали бы пересчёт, которого ещё не случилось.

    Уведомление уходит тогда, когда серия действительно изменилась, а не
    тогда, когда исход вышел нарушенным. Это не одно и то же: день с поздним
    тегом не зачитывается в обеих ветках (тег — это нарушение, ТЗ 7.1), и
    сказать «стрик пересчитан» надо ровно в том случае, когда он пересчитался.
    """
    tags = await late_tags(s, user_id, prefs, rule, day, now=now)
    if not tags:
        return []

    before, after = await recount(s, user_id, prefs, day, today=today)
    if before != after:
        # Telegram — шаг 13. До него текст уходит в журнал: это честнее,
        # чем очередь, из которой никто не читает.
        log.info(
            "уведомление retro_violation (шаг 13 отправит в Telegram): %s",
            incidents.retro_violation_text(day, before, after),
        )
    return tags
