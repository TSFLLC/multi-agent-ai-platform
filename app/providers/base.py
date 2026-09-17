"""Provider Adapter interface — Section 13.1.

The Model Registry never talks to a specific provider's HTTP API from
business logic — it talks to this interface. OpenRouter is V1's first
implementation (``OpenRouterAdapter``), not an architectural dependency:
nothing in ``ModelRegistryService`` references "openrouter" by name
(Section 13.5). Adding ``DirectAPIAdapter``/``LocalModelAdapter`` later
means implementing this interface, not touching the registry, Agent,
Task, or Flight Recorder contracts.

``invoke()`` is reserved but intentionally unimplemented in MA2 — MA3 owns
real model execution.
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


class ProviderConnectionError(Exception):
    """Network-level or provider-side failure not specific to auth."""


class ProviderAdapter(Protocol):
    def list_models(self) -> List[ModelDescriptor]: ...

    def get_model(self, provider_model_id: str) -> Optional[ModelDescriptor]: ...

    def health_check(self) -> ProviderHealth: ...

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        """Reserved for MA3 — real model execution is out of MA2 scope."""
        ...
