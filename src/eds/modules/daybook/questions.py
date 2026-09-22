"""Состав пре-маркет чека. Пять вопросов из ТЗ 5.2, шкала 1–5, максимум 25.

Каталог лежит на сервере, а не во фронте: формулировки будут меняться, и менять
их без релиза фронта — единственный способ не превратить правку слова в задачу
на выпуск. Вопросы без стопоров: допуск определяется только суммой (ТЗ 5.2).
"""

from dataclasses import dataclass

MIN_ANSWER = 1
MAX_ANSWER = 5


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    short: str
    low: str
    high: str
    hint: str = ""
    inverted: bool = False
    weight: int = 1

    def points(self, answer: int) -> int:
        """Вклад ответа в балл. Всегда «больше — лучше».

        Переворачивать арифметику не нужно, и это важная деталь: у обратного
        вопроса переворачиваются ПОДПИСИ шкалы — «1 — сильное желание
        отыграться», «5 — нет желания». Единая шкала «больше — лучше» держится
        подписями, а не вычитанием, поэтому один и тот же ответ означает одно
        и то же и на экране, и в базе, и в правилах.
        """
        return answer * self.weight


# Порядок, формулировки и подписи — из ТЗ 5.2 и прототипа Э-05. Подписи
# по краям обязательны: у обратного вопроса переворот должен быть виден
# из подписей, а не из сноски.
QUESTIONS: tuple[Question, ...] = (
    Question(
        id="sleep",
        text="Сколько ты спал?",
        short="Сон",
        low="Меньше 4 часов",
        high="Выспался",
    ),
    Question(
        id="emotion",
        text="Как оцениваешь своё эмоциональное состояние прямо сейчас?",
        short="Эмоциональное состояние",
        low="На грани",
        high="Спокоен",
    ),
    Question(
        id="yesterday",
        text="Насколько отпустил вчерашний результат?",
        short="Вчерашний результат",
        low="Не отпустил",
        high="Полностью",
    ),
    Question(
        id="revenge",
        text="Есть ли желание отыграться?",
        short="Желание отыграться",
        low="Сильное",
        high="Нет",
        hint="Обратная шкала: чем сильнее желание, тем ниже балл.",
        inverted=True,
    ),
    Question(
        id="plan",
        text="Есть ли у тебя план на сегодня?",
        short="План на день",
        low="Нет плана",
        high="План готов",
    ),
)

BY_ID = {q.id: q for q in QUESTIONS}
MAX_SCORE = sum(q.weight * MAX_ANSWER for q in QUESTIONS)

# Ниже этого вклада ответ показывается трейдеру как просадивший балл (Э-05).
WEAK_AT_OR_BELOW = 2


def as_dicts() -> list[dict]:
    return [
        {
            "id": q.id,
            "text": q.text,
            "short": q.short,
            "min": MIN_ANSWER,
            "max": MAX_ANSWER,
            "labels": {str(MIN_ANSWER): q.low, str(MAX_ANSWER): q.high},
            "hint": q.hint,
            "inverted": q.inverted,
        }
        for q in QUESTIONS
    ]


def score_of(answers: dict[str, int]) -> int:
    return sum(BY_ID[key].points(value) for key, value in answers.items())


def weak_answers(answers: dict[str, int]) -> list[dict]:
    """Ответы, просадившие балл. Единственное место, где сервис даёт повод подумать."""
    out = []
    for q in QUESTIONS:
        if q.id not in answers:
            continue
        points = q.points(answers[q.id])
        if points <= WEAK_AT_OR_BELOW:
            out.append({"id": q.id, "short": q.short, "points": points, "max": MAX_ANSWER})
    return out
