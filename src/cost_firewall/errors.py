"""Typed errors and OpenAI-compatible error envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorEnvelope:
    message: str
    type: str
    code: str
    param: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.type,
                "param": self.param,
                "code": self.code,
            }
        }


class GatewayError(Exception):
    """A client-visible failure with a stable error code."""

    status_code = 500
    error_type = "api_error"
    code = "internal_error"
    param: str | None = None

    def __init__(self, message: str, *, param: str | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        self.param = param
        self.retry_after = retry_after

    def envelope(self) -> ErrorEnvelope:
        return ErrorEnvelope(
            message=self.message, type=self.error_type, code=self.code, param=self.param
        )


class ConfigError(Exception):
    """Invalid operator configuration. Raised at startup, never per request."""


class ValidationError(GatewayError):
    status_code = 400
    error_type = "invalid_request_error"
    code = "invalid_request"


class PerRequestLimitExceeded(GatewayError):
    status_code = 400
    error_type = "invalid_request_error"
    code = "per_request_limit_exceeded"


class AuthError(GatewayError):
    status_code = 401
    error_type = "authentication_error"
    code = "invalid_api_key"


class ModelNotFound(GatewayError):
    status_code = 404
    error_type = "invalid_request_error"
    code = "model_not_found"


class PayloadTooLarge(GatewayError):
    status_code = 413
    error_type = "invalid_request_error"
    code = "payload_too_large"


class BudgetExhausted(GatewayError):
    status_code = 429
    error_type = "insufficient_quota"
    code = "budget_exhausted"


class RateLimited(GatewayError):
    status_code = 429
    error_type = "rate_limit_error"
    code = "rate_limited"


class ConcurrencyLimited(GatewayError):
    status_code = 429
    error_type = "rate_limit_error"
    code = "concurrency_limit"


class BudgetStateUnavailable(GatewayError):
    status_code = 503
    error_type = "api_error"
    code = "budget_state_unavailable"


class UpstreamTimeout(GatewayError):
    status_code = 504
    error_type = "timeout_error"
    code = "upstream_timeout"


class UpstreamFailure(GatewayError):
    status_code = 502
    error_type = "api_error"
    code = "upstream_error"


class ProviderError(Exception):
    """A provider call failed. `error_class` decides whether fallback is allowed."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        error_class: str,
        status_code: int | None = None,
        usage: tuple[int, int] | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.error_class = error_class  # "transient" | "permanent"
        self.status_code = status_code
        self.usage = usage

    @property
    def transient(self) -> bool:
        return self.error_class == "transient"
