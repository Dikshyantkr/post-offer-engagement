"""Application error types and the single global exception handler set.

Every error response uses the same envelope: {"error": {code, message, details}}.
Unexpected exceptions are logged server-side but never surface their details to
the client — only the four AppError subclasses below carry client-facing detail.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"

    def __init__(self, message: str, details: Any = None) -> None:
        self.message = message
        self.details = details
        super().__init__(message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class BadStateTransitionError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_state_transition"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ConfigurationError(AppError):
    """Server-side data the app depends on is missing or unusable — e.g. the
    journey_stages template was never seeded. A 500 is correct: the client's
    request was fine, the deployment is not."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "configuration_error"


class LLMProviderError(AppError):
    """Reserved for Module 5. Nothing in Module 2 raises this yet, but the
    handler is wired up now so the AI service can use it without touching
    error-handling plumbing later."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "llm_provider_unavailable"


def _envelope(code: str, message: str, details: Any = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, jsonable_encoder(exc.details)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope(
                "validation_error", "Request validation failed.", jsonable_encoder(exc.errors())
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("http_error", str(exc.detail), None),
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # Logged in full server-side; the client gets the shape of the problem
        # without the SQL, constraint names, or parameter values.
        logger.warning(
            "Integrity error on %s %s: %s", request.method, request.url.path, exc.orig
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_envelope(
                "conflict",
                "The request conflicts with an existing record.",
                None,
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception while processing %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("internal_error", "An unexpected error occurred.", None),
        )
