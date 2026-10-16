"""Per-call results; absent billing information is unknown, never zero."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CallUsage:
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    input_images: int = 0
    output_images: int = 0
    elapsed_ms: int = 0
    estimated_cost: float | None = None
    currency: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    value: Any
    usage: CallUsage


class NmmProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        usage: CallUsage | None = None,
        outcome_unknown: bool = False,
        timeout_phase: str | None = None,
        request_bytes: int | None = None,
    ):
        if timeout_phase is not None and timeout_phase not in (
            'connect',
            'write',
            'read',
            'pool',
            'overall',
            'http_408',
            'transport',
        ):
            raise ValueError('Invalid timeout phase')
        if request_bytes is not None and (type(request_bytes) is not int or request_bytes < 0):
            raise ValueError('Invalid request byte count')
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.usage, self.outcome_unknown = retryable, usage, outcome_unknown
        self.timeout_phase, self.request_bytes = timeout_phase, request_bytes
