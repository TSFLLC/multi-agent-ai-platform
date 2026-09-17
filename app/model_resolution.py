"""Model resolution — Section 14, MA3.

Manual selection is the primary MA3 path. Auto selection implements only
the smallest deterministic algorithm needed to honor the frozen
FREE_ONLY/PREFER_FREE/ANY contract — eligibility first, policy second,
stable deterministic tie-break. No historical-performance ranking, no
scoring, no AI-chooses-AI (that is MA8's "intelligent routing," explicitly
out of scope here).

UNKNOWN pricing is never silently treated as FREE, and is excluded from
FREE_ONLY/PREFER_FREE eligibility entirely — an automatic policy cannot
safely reason about a price it doesn't have. ANY may still select an
UNKNOWN-priced model (that's what "ANY" means), but only as the last
resort after FREE and PAID candidates, so a real price is always
preferred when one exists.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import HealthStatus, ModelStatus, PricingClassification, RouterFreePolicy
from app.domain.pricing import classify_pricing
from app.models.providers import Model, Provider, ProviderModel, ProviderModelSnapshot

_TIER_ORDER = {
    PricingClassification.FREE: 0,
    PricingClassification.PAID: 1,
    PricingClassification.UNKNOWN: 2,
}


class ModelUnavailableError(Exception):
    """A manually-selected provider model does not exist, its model is
    not ACTIVE, or its provider is DOWN."""


class NoEligibleModelError(Exception):
    """No candidate satisfies the requested auto policy — fail clearly,
    never silently fall back to a model the policy explicitly excludes."""


@dataclass
class ResolvedModel:
    provider_model: ProviderModel
    model: Model
    provider: Provider
    pricing_classification: PricingClassification
    rationale: str
    eligible_candidate_ids: List[str]


def resolve_manual(db: Session, provider_model_id: str) -> ResolvedModel:
    provider_model = db.get(ProviderModel, provider_model_id)
    if provider_model is None:
        raise ModelUnavailableError(f"Provider model {provider_model_id} does not exist.")

    model = db.get(Model, provider_model.model_id)
    provider = db.get(Provider, provider_model.provider_id)
    if model is None or provider is None:
        raise ModelUnavailableError(
            f"Provider model {provider_model_id} has a dangling model/provider reference."
        )

    if model.status != ModelStatus.ACTIVE:
        raise ModelUnavailableError(
            f"Model {model.canonical_model_id!r} is not active (status={model.status.value!r})."
        )
    if provider.health_status == HealthStatus.DOWN:
        raise ModelUnavailableError(f"Provider {provider.name!r} is currently down.")

    classification = classify_pricing(provider_model.cost_input_per_mtok, provider_model.cost_output_per_mtok)
    return ResolvedModel(
        provider_model=provider_model,
        model=model,
        provider=provider,
        pricing_classification=classification,
        rationale=f"manual selection of provider_model {provider_model_id}",
        eligible_candidate_ids=[provider_model.id],
    )


def _eligible_candidates(db: Session) -> List[Tuple[ProviderModel, Model, Provider, PricingClassification]]:
    stmt = (
        select(ProviderModel, Model, Provider)
        .join(Model, ProviderModel.model_id == Model.id)
        .join(Provider, ProviderModel.provider_id == Provider.id)
        .where(Model.status == ModelStatus.ACTIVE, Provider.health_status != HealthStatus.DOWN)
    )
    return [
        (pm, model, provider, classify_pricing(pm.cost_input_per_mtok, pm.cost_output_per_mtok))
        for pm, model, provider in db.execute(stmt).all()
    ]


def _tie_break_key(item: Tuple[ProviderModel, Model, Provider, PricingClassification]):
    provider_model, model, _provider, classification = item
    cost = (
        provider_model.cost_input_per_mtok
        if provider_model.cost_input_per_mtok is not None
        else Decimal("Infinity")
    )
    # Deterministic, documented, and deliberately boring: tier (free
    # before paid before unknown), then cheapest input price, then a
    # stable alphabetical tie-break. Never a learned/scored ranking.
    return (_TIER_ORDER[classification], cost, model.canonical_model_id)


def resolve_auto(db: Session, policy: RouterFreePolicy) -> ResolvedModel:
    candidates = _eligible_candidates(db)
    if not candidates:
        raise NoEligibleModelError(
            "No active provider model is currently available from any enabled provider."
        )

    if policy == RouterFreePolicy.FREE_ONLY:
        eligible = [c for c in candidates if c[3] == PricingClassification.FREE]
        if not eligible:
            raise NoEligibleModelError(
                "FREE_ONLY policy: no currently eligible FREE model is available — refusing to "
                "silently fall back to a paid or unknown-priced model."
            )
    elif policy == RouterFreePolicy.PREFER_FREE:
        eligible = [c for c in candidates if c[3] in (PricingClassification.FREE, PricingClassification.PAID)]
        if not eligible:
            raise NoEligibleModelError(
                "PREFER_FREE policy: no eligible FREE or PAID model is available. UNKNOWN-priced "
                "models are excluded from automatic selection because their cost cannot be budgeted."
            )
    elif policy == RouterFreePolicy.ANY:
        eligible = candidates
    else:  # pragma: no cover - defensive, RouterFreePolicy is a closed enum
        raise ValueError(f"Unknown RouterFreePolicy: {policy!r}")

    eligible.sort(key=_tie_break_key)
    provider_model, model, provider, classification = eligible[0]

    rationale = (
        f"auto policy={policy.value} selected={model.canonical_model_id!r} "
        f"tier={classification.value} among {len(eligible)} eligible of {len(candidates)} total candidates"
    )
    return ResolvedModel(
        provider_model=provider_model,
        model=model,
        provider=provider,
        pricing_classification=classification,
        rationale=rationale,
        eligible_candidate_ids=[c[0].id for c in eligible],
    )


def freeze_snapshot(db: Session, resolved: ResolvedModel) -> ProviderModelSnapshot:
    """Section 24.4 #8: a fresh, immutable snapshot created at Router
    resolution time — never reused from an earlier catalog refresh — so
    the exact pricing/capability context this specific execution saw can
    never silently drift if the catalog changes later."""
    snapshot = ProviderModelSnapshot(
        provider_model_id=resolved.provider_model.id,
        model_id=resolved.model.id,
        provider_id=resolved.provider.id,
        pricing_input_per_mtok=resolved.provider_model.cost_input_per_mtok,
        pricing_output_per_mtok=resolved.provider_model.cost_output_per_mtok,
        context_window=resolved.model.context_window,
        capability_snapshot={
            "tool_calling_support": (
                resolved.model.tool_calling_support.value if resolved.model.tool_calling_support else None
            ),
            "structured_output_support": resolved.model.structured_output_support,
            "vision_capability": resolved.model.vision_capability,
        },
    )
    db.add(snapshot)
    db.flush()
    return snapshot
