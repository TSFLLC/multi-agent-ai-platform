"""Provider Adapter interface — Section 13.1.

The Model Registry never talks to a specific provider's HTTP API from
business logic — it talks to this interface. OpenRouter is V1's first
implementation (``OpenRouterAdapter``), not an architectural dependency:
nothing in ``ModelRegistryService`` references "openrouter" by name
(Section 13.5). Adding ``DirectAPIAdapter``/``LocalModelAdapter`` later
means implementing this interface, not touching the registry, Agent,
Task, or Flight Recorder contracts.

``invoke()`` (MA3) is real model execution — every OpenRouter-specific
request/response detail still lives only in ``OpenRouterAdapter``; the
execution service depends on ``InvokeRequest``/``InvokeResponse`` here,
never on OpenRouter's own JSON shape.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class ModelDescriptor:
    """Normalized shape every ProviderAdapter.list_models()/get_model()
    call returns, regardless of the underlying provider's own JSON shape.

    Fields the provider doesn't reliably expose are ``None`` — never
    guessed (Owner instruction, Section 3).
    """

    provider_model_id: str
    name: str
    context_window: Optional[int] = None
    input_modalities: Optional[List[str]] = None
    output_modalities: Optional[List[str]] = None
    tool_calling_support: Optional[str] = None  # matches app.db.enums.ToolCallingSupport values
    structured_output_support: Optional[bool] = None
    cost_input_per_mtok: Optional[Decimal] = None
    cost_output_per_mtok: Optional[Decimal] = None
    # The untouched provider payload for this model — never persisted
    # wholesale, but useful for debugging a normalization gap.
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderHealth:
    status: str  # "up" | "degraded" | "down" — matches app.db.enums.HealthStatus
    detail: Optional[str] = None


class ProviderAuthenticationError(Exception):
    """The provider rejected our credentials specifically (e.g. HTTP 401)
    — distinct from a generic network/provider failure so a connectivity
    test can tell the operator which one happened."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class ProviderConnectionError(Exception):
    """Network-level or provider-side failure not specific to auth."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class ProviderTimeoutError(ProviderConnectionError):
    """The request exceeded the caller's timeout — distinct from a
    generic connection error so the execution service can categorize it
    for the Agent Run Attempt's error record."""


class ProviderInvalidResponseError(ProviderConnectionError):
    """The provider responded (2xx) but the body didn't contain what a
    normal completion must have (e.g. no choices) — never surfaced as a
    silent empty result."""


@dataclass
class InvokeRequest:
    """Generic chat-style invocation request — the shape the execution
    service builds, regardless of provider. Never carries a raw API key;
    the adapter already holds its own credential."""

    provider_model_id: str
    user_prompt: str
    system_prompt: Optional[str] = None
    timeout_seconds: float = 60.0
    max_tokens: Optional[int] = None
    response_format: Optional[Dict[str, Any]] = None


@dataclass
class InvokeResponse:
    """Normalized result of one real model call — Section 6 "Model calls"
    contract fields the execution service needs. ``cost_amount`` is only
    populated when the provider itself reports actual spend; otherwise the
    execution service computes an estimate from the pricing snapshot and
    flags it accordingly (Acceptance Criterion 4 — never an ambiguous
    number)."""

    text: str
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    latency_ms: int = 0
    provider_request_id: Optional[str] = None
    cost_amount: Optional[Decimal] = None
    provider_http_status: Optional[int] = None
    finish_reason: Optional[str] = None
    tokens_total: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    def list_models(self) -> List[ModelDescriptor]: ...

    def get_model(self, provider_model_id: str) -> Optional[ModelDescriptor]: ...

    def health_check(self) -> ProviderHealth: ...

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        """Real model execution (MA3). Raises ProviderAuthenticationError/
        ProviderTimeoutError/ProviderConnectionError/
        ProviderInvalidResponseError on failure — never returns a
        response object representing a failed call."""
        ...
