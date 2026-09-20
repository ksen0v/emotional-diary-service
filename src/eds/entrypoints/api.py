"""Процесс api: HTTP и страница состояния.

На шаге 0 здесь же живёт демонстрационный консьюмер шины: он ничего не делает,
кроме сдвига своего курсора, и нужен, чтобы на странице было видно, что событие
дошло от продюсера до консьюмера. На шаге 4 консьюмеры переедут в процесс worker.
"""

import asyncio
import contextlib
import datetime as dt
import logging
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from eds.contracts import events as ev
from eds.modules.identity.api import router as identity_router
from eds.platform import bus, db, errors, log
from eds.platform.config import settings
from eds.version import STEP, STEP_NAME, VERSION

logger = logging.getLogger("eds.api")

WEB = Path(__file__).resolve().parent.parent / "web"

_delivered: dict[str, int] = {"demo": 0}


async def _demo_handler(event: bus.Event) -> None:
    """Обработчик шага 0: считает доставленные события в памяти процесса."""
    _delivered["demo"] = _delivered.get("demo", 0) + 1
    logger.info("демо-консьюмер получил %s (id=%s)", event.type, event.id)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    log.setup(settings().log_level)
    logger.info("запуск api, шаг %s — %s, версия %s", STEP, STEP_NAME, VERSION)

    consumer = bus.Consumer("demo", _demo_handler)
    task = asyncio.create_task(consumer.run(), name="consumer-demo")
    app.state.consumer = consumer
    try:
        yield
    finally:
        consumer.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await db.dispose()
        logger.info("api остановлен")


app = FastAPI(title="Emotional Diary Service", version=VERSION, lifespan=lifespan)
errors.install(app)
app.include_router(identity_router)


@app.get("/api/v1/health")
async def health() -> dict:
    database = await db.db_state()
    shina = await bus.bus_state()
    return {
        "service": "Emotional Diary Service",
        "version": VERSION,
        "step": {"number": STEP, "name": STEP_NAME},
        "env": settings().env,
        "server_time": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "secret_key_set": settings().secret_key_set,
        "db": database,
        "bus": shina | {"delivered_in_process": _delivered.get("demo", 0)},
        "ok": bool(database["connected"] and database["revision"]),
    }


@app.post("/api/v1/dev/test-event")
async def test_event(s: AsyncSession = Depends(db.session)) -> dict:
    """Кнопка на странице состояния: опубликовать событие в шину."""
    event_id = await bus.publish(s, ev.PLATFORM_TEST_PING, {"from": "status_page"})
    await s.commit()
    return {"published_id": event_id}


@app.get("/", response_class=HTMLResponse)
async def status_page() -> str:
    return (WEB / "status.html").read_text(encoding="utf-8")


def main() -> None:
    log.setup(settings().log_level)
    uvicorn.run(
        "eds.entrypoints.api:app",
        host="0.0.0.0",
        port=8000,
        log_config=None,
        reload=False,
    )


if __name__ == "__main__":
    main()
