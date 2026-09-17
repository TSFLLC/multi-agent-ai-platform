"""OpenRouterAdapter — Section 13.1/13.5, MA2.

The one place in the codebase that knows OpenRouter's actual HTTP/JSON
shape. Everything else (ModelRegistryService, the API routers, Agent
model_policy) talks to the generic ``ProviderAdapter`` interface.

Pricing note: OpenRouter's ``/models`` response prices per single token
in USD (e.g. ``"0.0000015"``), not per million tokens — this adapter
converts to the platform's per-mtok convention
(``provider_models.cost_input_per_mtok``) at the boundary, once, so
nothing downstream has to know OpenRouter's specific unit convention.

Never hard-codes which model IDs are free — FREE/PAID/UNKNOWN
classification (app.domain.pricing) is derived from whatever pricing
this adapter normalizes out of the *current* catalog response.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import httpx

from app.providers.base import (
    ModelDescriptor,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderHealth,
)

logger = logging.getLogger("app.providers.openrouter")

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_USD_PER_TOKEN_TO_PER_MTOK = Decimal(1_000_000)


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if parsed < 0:
        # Discovered against the live catalog: OpenRouter uses a negative
        # sentinel (observed: "-1") on meta/auto-routing models (e.g.
        # "openrouter/auto-beta") to mean "variable, not a fixed price" —
        # not a real price. Treating it as unknown (never as a literal
        # negative cost) is what the frozen non-negative pricing
        # constraint (provider_models CHECK) already assumes; passing it
        # through as -1,000,000/mtok both misrepresents the model and
        # violates that constraint.
        return None
    return parsed


def _per_mtok(value: Any) -> Optional[Decimal]:
    per_token = _to_decimal(value)
    if per_token is None:
        return None
    return per_token * _USD_PER_TOKEN_TO_PER_MTOK


def normalize_model(raw: Dict[str, Any]) -> ModelDescriptor:
    """Pure function — no I/O — so normalization can be unit-tested
    against fixture payloads without any network access."""
    pricing = raw.get("pricing") or {}
    architecture = raw.get("architecture") or {}

    input_modalities: Optional[List[str]] = None
    output_modalities: Optional[List[str]] = None
    modality = architecture.get("modality")
    if isinstance(modality, str) and "->" in modality:
        in_part, out_part = modality.split("->", 1)
        input_modalities = [m for m in in_part.split("+") if m] or None
        output_modalities = [m for m in out_part.split("+") if m] or None

    supported_params = raw.get("supported_parameters")
    tool_calling_support: Optional[str] = None
    structured_output_support: Optional[bool] = None
    if isinstance(supported_params, list):
        if "tools" in supported_params or "tool_choice" in supported_params:
            tool_calling_support = "basic"
        elif supported_params:
            # The field is present and reliable for this model, and
            # explicitly does not list tool support.
            tool_calling_support = "none"
        if "response_format" in supported_params or "structured_outputs" in supported_params:
            structured_output_support = True

    context_length = raw.get("context_length")

    return ModelDescriptor(
        provider_model_id=raw["id"],
        name=raw.get("name") or raw["id"],
        context_window=context_length if isinstance(context_length, int) else None,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        tool_calling_support=tool_calling_support,
        structured_output_support=structured_output_support,
        cost_input_per_mtok=_per_mtok(pricing.get("prompt")),
        cost_output_per_mtok=_per_mtok(pricing.get("completion")),
        raw=raw,
    )


class OpenRouterAdapter:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: Optional[httpx.Client] = None,
        timeout_seconds: float = 15.0,
    ):
        self.api_key = api_key
        self._client = http_client or httpx.Client(base_url=base_url, timeout=timeout_seconds)

    def _headers(self) -> Dict[str, str]:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get(self, path: str) -> httpx.Response:
        try:
            response = self._client.get(path, headers=self._headers())
        except httpx.RequestError as exc:
            raise ProviderConnectionError(f"Network error contacting OpenRouter: {exc}") from exc

        if response.status_code == 401:
            raise ProviderAuthenticationError("OpenRouter rejected the configured API key (401).")
        if response.status_code >= 400:
            raise ProviderConnectionError(f"OpenRouter returned HTTP {response.status_code} for {path}.")
        return response

    def list_models(self) -> List[ModelDescriptor]:
        response = self._get("/models")
        payload = response.json()
        entries = payload.get("data", [])
        descriptors = []
        for entry in entries:
            try:
                descriptors.append(normalize_model(entry))
            except (KeyError, TypeError) as exc:
                # A single malformed entry must never take down the whole
                # refresh — skip it, keep going.
                logger.warning("openrouter_model_normalize_skipped error=%s", exc)
        return descriptors

    def get_model(self, provider_model_id: str) -> Optional[ModelDescriptor]:
        for descriptor in self.list_models():
            if descriptor.provider_model_id == provider_model_id:
                return descriptor
        return None

    def health_check(self) -> ProviderHealth:
        try:
            self._get("/models")
            return ProviderHealth(status="up")
        except ProviderAuthenticationError as exc:
            return ProviderHealth(status="down", detail=f"authentication_failed: {exc}")
        except ProviderConnectionError as exc:
            return ProviderHealth(status="down", detail=f"connection_failed: {exc}")

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("OpenRouterAdapter.invoke() lands in MA3 — MA2 is catalog-only.")
