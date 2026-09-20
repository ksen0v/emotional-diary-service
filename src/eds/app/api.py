"""HTTP оркестрации: то, что задевает несколько модулей.

Сверка стоит здесь, а не в source и не в trades: она соединяет источник,
настройки пользователя и приём сделок, то есть по определению не принадлежит
ни одному модулю.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from eds.app import pipeline
from eds.platform import auth, db

router = APIRouter(prefix="/api/v1", tags=["sync"])


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
    report = await pipeline.sync(s, user.user_id)
    await s.commit()
    return report.as_dict()
