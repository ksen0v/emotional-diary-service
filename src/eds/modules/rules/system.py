"""Системные триггеры SR-1…SR-4: встроенные, отключить нельзя (ТЗ 6.5).

Их условия тремя показателями не выражаются — тег нарушения, сделка при
блокировке, торговля без допуска, неразмеченная сделка, — поэтому в базе
`conditions` у них пустые, а условие живёт в коде обработчика (Архитектура ч.1 §7).
Здесь лежит то, что у них всё-таки настраивается, и то, как они называются
словами.

Что редактируется, названо списком `editable`, а не «всё, кроме»: фронт должен
показывать заблокированные поля заблокированными, а не узнавать об отказе
после нажатия «Сохранить».

Срабатывать они начнут на шаге 10. Сейчас это карточки со настройками и
честным нулём в счётчике — в них нет ни строчки механики.
"""

from dataclasses import dataclass, field
from typing import Any

SR1 = "SR-1"
SR2 = "SR-2"
SR3 = "SR-3"
SR4 = "SR-4"


@dataclass(frozen=True)
class SystemRule:
    code: str
    name: str
    if_template: str
    summary: str
    actions: dict[str, Any]
    unlock: dict[str, Any]
    editable: tuple[str, ...] = field(default=())

    def if_text(self, actions: dict[str, Any]) -> str:
        minutes = (actions or {}).get("remind_after_minutes")
        if "{minutes}" not in self.if_template:
            return self.if_template
        return self.if_template.format(minutes=minutes)


# Порядок задаёт порядок карточек в списке: сверху то, что срабатывает чаще.
SYSTEM_RULES: tuple[SystemRule, ...] = (
    SystemRule(
        code=SR1,
        name="Несистемная сделка",
        if_template="сделка отмечена как нарушение",
        # Текст карточки взят из прототипа дословно.
        summary="Блокировка до конца дня",
        actions={"alert": True, "lock": {"enabled": True, "minutes": None}, "buddy": False},
        unlock={"timer": True, "review": True, "buddy": False},
        editable=("actions.buddy", "actions.lock.minutes", "unlock"),
    ),
    SystemRule(
        code=SR2,
        name="Сделка при блокировке",
        if_template="сделка открыта во время активной блокировки",
        summary="Сброс стрика, сигнал другу",
        actions={"alert": True, "lock": {"enabled": True, "minutes": None}, "buddy": True},
        unlock={"timer": True, "review": True, "buddy": False},
        editable=("actions.buddy", "actions.lock.minutes", "unlock"),
    ),
    SystemRule(
        code=SR3,
        name="Торговля без допуска",
        if_template="есть сделки без пройденного пре-маркет чека",
        summary="Сброс стрика, сигнал другу",
        # Длительность не настраивается: день без допуска закрыт целиком
        # (ТЗ 5.2), и «заблокировать на 30 минут» противоречило бы этому.
        actions={"alert": True, "lock": {"enabled": True, "minutes": None}, "buddy": True},
        unlock={"timer": False, "review": True, "buddy": False},
        editable=("actions.buddy", "unlock"),
    ),
    SystemRule(
        code=SR4,
        name="Неразмеченная сделка",
        if_template="сделка закрыта и не размечена дольше {minutes} минут",
        summary="Напоминание разметить сделку",
        actions={
            "alert": True,
            "lock": {"enabled": False, "minutes": None},
            "buddy": False,
            "remind_after_minutes": 15,
        },
        unlock={"timer": False, "review": False, "buddy": False},
        editable=("actions.remind_after_minutes",),
    ),
)

BY_CODE: dict[str, SystemRule] = {r.code: r for r in SYSTEM_RULES}
ORDER: dict[str, int] = {r.code: i for i, r in enumerate(SYSTEM_RULES)}

REMIND_MIN = 1
REMIND_MAX = 240
