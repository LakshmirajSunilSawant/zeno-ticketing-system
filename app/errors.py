# WHY a custom exception type + central handlers: business rules raise a plain Python exception,
# and exactly one place decides how that becomes HTTP. Routers stay readable and no handler can
# forget the error envelope.
from typing import Any


class AppError(Exception):
    """Base for expected, client-facing failures. Anything NOT derived from this is a bug and
    becomes a generic 500 with no internals leaked."""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Conflict(AppError):
    """409 - used for "not enough seats" and for state transitions that no longer make sense
    (e.g. cancelling an already-cancelled booking). 409 is the right code because the request was
    well-formed; it conflicts with current server state."""

    status_code = 409
    code = "conflict"


def error_payload(code: str, message: str, request_id: str | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}
