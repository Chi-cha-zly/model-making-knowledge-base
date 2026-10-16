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
    ):
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.usage, self.outcome_unknown = retryable, usage, outcome_unknown
