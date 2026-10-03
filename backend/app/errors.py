"""The single API error shape.

Every error response is::

    {"error": {"code": "machine_readable_code",
               "message": "A user-safe sentence.",
               "fields": [{"field": "recipients[2]", "code": "suppressed", "message": "..."}],
               "request_id": "..."}}

`fields` is present (possibly empty) for validation failures only. Some errors
add `details` (for example a sync conflict carries the current record).
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_logger, request_id_var

log = get_logger("errors")


class FieldError(BaseModel):
    field: str
    code: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    fields: list[FieldError] | None = None
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class ApiError(Exception):
    status_code = 400
    code = "bad_request"
    message = "The request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        fields: list[FieldError] | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.fields = fields
        self.details = details
        self.headers = headers
        super().__init__(self.message)


class ValidationFailed(ApiError):
    status_code = 422
    code = "validation_failed"
    message = "Some fields are invalid."

    def __init__(self, fields: list[FieldError], message: str | None = None):
        super().__init__(message, fields=fields)


class Unauthenticated(ApiError):
    status_code = 401
    code = "unauthenticated"
    message = "Sign in to continue."


class Forbidden(ApiError):
    status_code = 403
    code = "forbidden"
    message = "You don't have permission to do that."


class NotFound(ApiError):
    status_code = 404
    code = "not_found"
    message = "Not found."


class Conflict(ApiError):
    status_code = 409
    code = "conflict"
    message = "The request conflicts with the current state."


class RateLimited(ApiError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests. Try again later."


def field_error(field: str, message: str, code: str = "invalid") -> FieldError:
    return FieldError(field=field, code=code, message=message)


def _body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "request_id": request_id_var.get()}
    error.update({key: value for key, value in extra.items() if value is not None})
    return {"error": error}


def _location(loc: tuple[Any, ...]) -> str:
    parts = [str(part) for part in loc if part not in ("body", "query", "path", "header")]
    field = ""
    for part in parts:
        field += f"[{part}]" if part.isdigit() else (f".{part}" if field else part)
    return field or "body"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, error: ApiError) -> JSONResponse:
        fields = [f.model_dump() for f in error.fields] if error.fields is not None else None
        return JSONResponse(
            _body(error.code, error.message, fields=fields, details=error.details),
            status_code=error.status_code,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, error: RequestValidationError) -> JSONResponse:
        fields = [
            {
                "field": _location(tuple(item.get("loc", ()))),
                "code": str(item.get("type", "invalid")),
                "message": str(item.get("msg", "Invalid value.")).removeprefix("Value error, "),
            }
            for item in error.errors()
        ]
        return JSONResponse(
            _body("validation_failed", "Some fields are invalid.", fields=fields), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(_: Request, error: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}
        message = error.detail if isinstance(error.detail, str) else "Request failed."
        return JSONResponse(
            _body(codes.get(error.status_code, "http_error"), message),
            status_code=error.status_code,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, error: Exception) -> JSONResponse:
        log.exception("unhandled_error", extra={"event": "unhandled_error"})
        return JSONResponse(
            _body("internal_error", "Something went wrong. Try again later."), status_code=500
        )


# Documented on every route via `responses=ERROR_RESPONSES`.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse, "description": description}
    for status, description in {
        400: "Bad request",
        401: "Not signed in, or the session/API key is invalid, expired or revoked",
        403: "Not allowed for this user, role or account state",
        404: "Not found (also returned for other accounts' resources)",
        409: "Conflict with the current state",
        422: "Validation failed – see `fields`",
        429: "Rate or sending limit reached – see the Retry-After header",
    }.items()
}
