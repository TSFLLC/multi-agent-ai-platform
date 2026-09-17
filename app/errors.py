"""Consistent API error contracts — Section K.

Every error response has the shape ``{"error": {"code", "message",
"detail"}}``. Handlers never leak stack traces, filesystem paths, or
provider credentials to the client — those go to the local structured log
(app.logging_config) instead, tagged with the same error code so the two
can be correlated by an operator without exposing internals over HTTP.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")


class AppError(Exception):
    """Base for domain errors that map to a specific HTTP status/code."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class UnauthorizedError(AppError):
    """No/invalid local auth token (MA1B) — the caller was never
    identified at all."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(AppError):
    """Identified, but not authorized for this project/action (MA1B) —
    either no project_memberships row exists for this user+project, or
    the existing role doesn't permit the requested action."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class InvalidStateTransitionError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "invalid_state_transition"


class IdempotencyConflictError(AppError):
    """A different request body was seen for an in-flight/completed
    Idempotency-Key (Section 25.4/G)."""

    status_code = status.HTTP_409_CONFLICT
    code = "idempotency_conflict"


class LeaseFencingConflictError(AppError):
    """A worker's lease/fencing token no longer matches the row it's
    trying to update — Section 24.4 #12, it lost the job to another
    worker."""

    status_code = status.HTTP_409_CONFLICT
    code = "lease_fencing_conflict"


class ArtifactHashMismatchError(AppError):
    """MA5: the caller's echoed ``artifact_hash`` for a comparison's
    canonical-candidate selection no longer matches that candidate's actual
    artifact content — same TOCTOU-style binding as
    ``approvals.action_fingerprint`` (Section 24.4 #15). Rejected rather
    than silently canonicalizing a different result than the human
    reviewed."""

    status_code = status.HTTP_409_CONFLICT
    code = "artifact_hash_mismatch"


def _error_response(status_code: int, code: str, message: str, detail: Any = None) -> JSONResponse:
    body: Dict[str, Any] = {"error": {"code": code, "message": message}}
    if detail is not None:
        body["error"]["detail"] = jsonable_encoder(detail)
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.info("app_error code=%s path=%s message=%s", exc.code, request.url.path, exc.message)
        return _error_response(exc.status_code, exc.code, exc.message, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "Request validation failed.",
            detail={"errors": exc.errors()},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Preserves FastAPI's own raised HTTPExceptions (e.g. the MA0
        # not_implemented() 501 stubs) under the same envelope shape.
        code = {404: "not_found", 409: "conflict", 501: "not_implemented"}.get(exc.status_code, "http_error")
        return _error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never reflect the exception message/stack trace to the client —
        # it can contain file paths or internal state. Full detail goes to
        # the local log only.
        logger.exception("unhandled_exception path=%s", request.url.path)
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "An internal error occurred."
        )
