"""Один формат ошибок на всё API (Архитектура ч.2 §1.3).

Тело ошибки: {"error": {"code", "message", "details"}}. code — машинный и стабильный,
message — готовый русский текст, который фронт показывает как есть. Это правило
существует, чтобы одно и то же состояние не было описано двумя способами
в вебе и в Telegram.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# 422: данные валидны по форме, но бессмысленны по сути.
# Своя константа, а не из starlette: там имя уже один раз переименовали.
UNPROCESSABLE = 422


class AppError(Exception):
    """Ошибка с кодом и готовым текстом. Всё остальное — 500."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


def body(code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


# --- частые ошибки, чтобы текст не расползался по коду ---


def unauthenticated() -> AppError:
    return AppError(
        "unauthenticated",
        "Сессия не найдена или истекла. Войди заново.",
        status.HTTP_401_UNAUTHORIZED,
    )


def csrf_invalid() -> AppError:
    return AppError(
        "csrf_invalid",
        "Запрос отклонён: не совпал защитный токен. Обнови страницу.",
        status.HTTP_403_FORBIDDEN,
    )


def not_found(what: str = "Объект не найден.") -> AppError:
    return AppError("not_found", what, status.HTTP_404_NOT_FOUND)


def install(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", []) if p not in ("body", "query")]
            fields.append({"field": ".".join(loc), "problem": err.get("msg", "")})
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=body(
                "validation_failed",
                "Данные заполнены неверно.",
                {"fields": fields},
            ),
        )
