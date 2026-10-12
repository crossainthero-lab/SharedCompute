"""Domain errors. The API layer maps each to an HTTP status and a stable error code."""


class DomainError(Exception):
    status = 400
    code = "invalid_request"

    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


class InvalidInput(DomainError):
    status = 422
    code = "invalid_input"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class InsufficientFunds(DomainError):
    status = 402
    code = "insufficient_funds"


class Unauthorized(DomainError):
    status = 401
    code = "unauthorized"


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class RateLimited(DomainError):
    status = 429
    code = "rate_limited"
