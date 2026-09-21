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
    inverted: bool = False
    weight: int = 1

    def points(self, answer: int) -> int:
        """Вклад ответа в балл.

        У обратного вопроса шкала переворачивается здесь, а не во фронте:
        иначе один интерфейс считал бы «5 — хорошо», другой «5 — плохо»,
        и балл зависел бы от того, откуда пришёл ответ.
        """
        value = MIN_ANSWER + MAX_ANSWER - answer if self.inverted else answer
        return value * self.weight


# Порядок — из ТЗ: сон, эмоции, вчерашний результат, желание отыграться, план.
# Подписи по краям обязательны: у обратного вопроса переворот должен быть виден
# из подписей, а не из сноски (решение дизайна, Э-05).
QUESTIONS: tuple[Question, ...] = (
    Question(
        id="sleep",
        text="Сколько ты спал?",
        short="Сон",
        low="Почти не спал",
        high="Выспался",
    ),
    Question(
        id="emotion",
        text="Как оцениваешь своё эмоциональное состояние прямо сейчас?",
        short="Эмоции",
        low="На взводе",
        high="Спокоен",
    ),
    Question(
        id="yesterday",
        text="Насколько отпустил вчерашний результат?",
        short="Вчерашний результат",
        low="Думаю только о нём",
        high="Не вспоминаю",
    ),
    Question(
        id="revenge",
        text="Есть ли желание отыграться?",
        short="Желание отыграться",
        low="Нет такого желания",
        high="Сильное, хочу вернуть своё",
        inverted=True,
    ),
    Question(
        id="plan",
        text="Есть ли у тебя план на сегодня?",
        short="План",
        low="Плана нет",
        high="План готов, уровни размечены",
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
