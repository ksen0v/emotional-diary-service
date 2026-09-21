"""HTTP оркестрации: то, что задевает несколько модулей.

Сверка стоит здесь, а не в source и не в trades: она соединяет источник,
настройки пользователя и приём сделок, то есть по определению не принадлежит
ни одному модулю.
"""

import contextlib

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import pipeline, today
from eds.platform import auth, db

router = APIRouter(prefix="/api/v1", tags=["sync"])


@router.get("/today")
async def today_screen(
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Весь главный экран одним запросом (Архитектура ч.2 §3.5).

    Без response_model: половина блоков появляется на следующих шагах, и
    описывать их схемой сейчас значило бы фиксировать форму того, чего нет.
    """
    data = await today.build(s, user.user_id, prefs)
    # Чтение, которое пишет: закрывает сессии дней, которые уже кончились.
    # Это не побочный эффект ради удобства, а работа процесса границы дня,
    # которого пока нет, поэтому коммит здесь обязателен.
    await s.commit()
    return data


@router.post("/sync")
async def sync(
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> dict:
    """Забрать сделки у активного источника и принять их.

    На шаге 4 этот же путь будет вызываться по расписанию и после обрыва потока;
    кнопка в интерфейсе остаётся как способ проверить руками.
    """
    try:
        report = await pipeline.sync(s, user.user_id)
    except Exception:
        # Неудачную сверку тоже надо сохранить: без записи в журнале
        # на вопрос «почему сделки не приехали» нечем ответить.
        with contextlib.suppress(Exception):
            await s.commit()
        raise
    await s.commit()
    return report.as_dict()
