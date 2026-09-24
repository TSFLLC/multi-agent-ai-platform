"""Model resolution / Router Foundation — Section 14, MA3, MA8.1.

This module is the platform's single model router. Every Agent Run —
single-agent, build/review, comparison candidate, workflow AGENT node and
EVALUATION node evaluator — reaches it through
``app.services.execution_service.AgentExecutionService`` via ``route()``.
There is no second router.

Manual selection is authoritative: an operator-chosen provider model is
validated and used, or the run fails with the reason — it is never replaced
by an automatic pick. Auto selection is the smallest deterministic algorithm
that honors the frozen FREE_ONLY/PREFER_FREE/ANY contract — eligibility
first, policy second, stable documented tie-break. No historical-performance
ranking, no scoring, no AI-chooses-AI (MA8.2+ is where evidence-based
suitability is allowed to enter).

UNKNOWN pricing is never silently treated as FREE, and is excluded from
FREE_ONLY/PREFER_FREE eligibility entirely — an automatic policy cannot
safely reason about a price it doesn't have. ANY may still select an
UNKNOWN-priced model (that's what "ANY" means), but only as the last
resort after FREE and PAID candidates, so a real price is always
preferred when one exists.

MA8.1 adds explanation, not new selection behavior: ``route()`` evaluates
every catalog candidate against the same rules the MA3 resolver used,
records a structured verdict (with ``ExclusionReason`` codes) for each, and
returns a ``RoutingDecision`` that ``record_routing_decision`` persists to
the MA0 ``model_routing_decisions`` table. Decision records carry only
catalog identifiers, names and pricing classifications — never a provider
credential, base URL or secret reference.
"""

import enum
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import (
    HealthStatus,
    ModelSelectionMode,
    ModelStatus,
    PricingClassification,
    RouterFreePolicy,
)
from app.domain.pricing import classify_pricing
from app.models.execution import ModelRoutingDecision
from app.models.providers import Model, Provider, ProviderModel, ProviderModelSnapshot
from app.routing_evidence import (
    EVIDENCE_STRATEGY_V1,
    EvidenceProfile,
    RoutingContext,
    load_active_policy,
    load_evidence,
)

logger = logging.getLogger("app.model_resolution")

# Recorded on every decision so strategies can be told apart when reading
# history: the MA8.1 order, or the MA8.2 evidence order layered on top of it
# (app.routing_evidence.EVIDENCE_STRATEGY_V1).
ROUTING_STRATEGY = "ma8.1-deterministic-v1"

# decision.evidence["status"] values (MA8.2). Every status except APPLIED
# means the MA8.1 order was used unchanged.
EVIDENCE_APPLIED = "applied"  # evidence ordering was used
EVIDENCE_INSUFFICIENT = "insufficient"  # no candidate met the minimum evidence
EVIDENCE_UNAVAILABLE = "unavailable"  # evidence could not be loaded
EVIDENCE_DISABLED = "disabled"  # no active router policy version

# Human-readable statement of the tie-break, recorded with each decision.
TIE_BREAK_RULE = (
    "pricing tier (free, paid, unknown), then input price per Mtok (unknown last), "
    "then canonical model id, then provider name, then provider-model id"
)

# Decision records keep at most this many candidates per list (eligible,
# excluded); exact counts are always recorded. Keeps a record small when an
# ANY policy sees the whole catalog.
MAX_RECORDED_CANDIDATES = 25

_TIER_ORDER = {
    PricingClassification.FREE: 0,
    PricingClassification.PAID: 1,
    PricingClassification.UNKNOWN: 2,
}


class ExclusionReason(str, enum.Enum):
    """Why a candidate was not eligible, or why routing failed. Stored as
    strings inside decision records (not a DB enum column)."""

    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"  # provider model id not in the catalog
    MODEL_INACTIVE = "MODEL_INACTIVE"  # models.status != ACTIVE
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"  # providers.health_status == DOWN
    PROVIDER_MODEL_UNAVAILABLE = "PROVIDER_MODEL_UNAVAILABLE"  # provider_models.availability_status == DOWN
    NOT_FREE = "NOT_FREE"  # PAID under FREE_ONLY
    PRICING_UNKNOWN = "PRICING_UNKNOWN"  # UNKNOWN under FREE_ONLY / PREFER_FREE
    MANUAL_MODEL_INVALID = "MANUAL_MODEL_INVALID"  # manual policy with no model id
    POLICY_INVALID = "POLICY_INVALID"  # malformed model policy
    NO_ELIGIBLE_MODEL = "NO_ELIGIBLE_MODEL"  # auto policy found nothing eligible
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"  # model lacks a required capability


class RoutingError(Exception):
    """Base for routing failures. ``code`` is an ``ExclusionReason``;
    ``decision`` (when set) is the failed ``RoutingDecision`` so the caller
    can still record what was considered."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[ExclusionReason] = None,
        decision: Optional["RoutingDecision"] = None,
    ):
        super().__init__(message)
        self.code = code
        self.decision = decision


class ModelUnavailableError(RoutingError):
    """A manually-selected provider model does not exist, its model is
    not ACTIVE, its provider or offering is DOWN, or the policy itself is
    malformed."""


class NoEligibleModelError(RoutingError):
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


@dataclass(frozen=True)
class CandidateVerdict:
    """One catalog candidate as the router saw it at decision time."""

    provider_model_id: str
    canonical_model_id: Optional[str]
    provider_id: Optional[str]
    provider_name: Optional[str]
    pricing_classification: Optional[PricingClassification]
    exclusion_reasons: Tuple[ExclusionReason, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.exclusion_reasons

    def to_json(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "provider_model_id": self.provider_model_id,
            "canonical_model_id": self.canonical_model_id,
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "pricing_classification": (
                self.pricing_classification.value if self.pricing_classification else None
            ),
        }
        if self.exclusion_reasons:
            entry["exclusion_reasons"] = [reason.value for reason in self.exclusion_reasons]
        return entry


@dataclass
class RoutingDecision:
    """The router's output: what was selected (if anything), from what, and
    why. ``eligible`` is in tie-break order, so ``eligible[0]`` is the pick
    and the rest are the preserved alternatives."""

    selection_mode: ModelSelectionMode
    requested_policy: Optional[RouterFreePolicy]
    reason: str
    selected: Optional[ResolvedModel] = None
    eligible: List[CandidateVerdict] = field(default_factory=list)
    excluded: List[CandidateVerdict] = field(default_factory=list)
    requested_provider_model_id: Optional[str] = None
    failure_code: Optional[ExclusionReason] = None
    strategy: str = ROUTING_STRATEGY
    router_policy_version_id: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None

    @property
    def fallback_used(self) -> Optional[bool]:
        """PREFER_FREE only: True when the pick is not FREE (the policy's
        explicit paid fallback). None for every other policy/mode."""
        if self.requested_policy != RouterFreePolicy.PREFER_FREE or self.selected is None:
            return None
        return self.selected.pricing_classification != PricingClassification.FREE

    @property
    def free_preference_satisfied(self) -> Optional[bool]:
        """FREE_ONLY/PREFER_FREE only: whether a FREE model was selected."""
        if self.requested_policy not in (RouterFreePolicy.FREE_ONLY, RouterFreePolicy.PREFER_FREE):
            return None
        if self.selected is None:
            return False
        return self.selected.pricing_classification == PricingClassification.FREE

    def exclusion_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for verdict in self.excluded:
            for reason in verdict.exclusion_reasons:
                counts[reason.value] = counts.get(reason.value, 0) + 1
        return dict(sorted(counts.items()))

    def details_json(self) -> Dict[str, Any]:
        selected = self.selected
        return {
            "outcome": "selected" if selected is not None else "failed",
            "failure_code": self.failure_code.value if self.failure_code else None,
            "requested_provider_model_id": self.requested_provider_model_id,
            "selected_provider_model_id": selected.provider_model.id if selected else None,
            "selected_canonical_model_id": selected.model.canonical_model_id if selected else None,
            "selected_provider_id": selected.provider.id if selected else None,
            "selected_pricing_classification": (selected.pricing_classification.value if selected else None),
            "eligible_count": len(self.eligible),
            "excluded_count": len(self.excluded),
            "exclusion_counts": self.exclusion_counts(),
            "excluded_candidates": [v.to_json() for v in self.excluded[:MAX_RECORDED_CANDIDATES]],
            "free_preference_satisfied": self.free_preference_satisfied,
            "fallback_used": self.fallback_used,
            "tie_break": TIE_BREAK_RULE if self.selection_mode == ModelSelectionMode.AUTO else None,
            "evidence": self.evidence,
        }


# -- eligibility ---------------------------------------------------------------


def _catalog_exclusions(
    provider_model: ProviderModel, model: Model, provider: Provider
) -> List[ExclusionReason]:
    """Policy-independent eligibility: what the catalog itself says about
    whether this offering can be invoked right now. A DEGRADED provider or
    offering is still eligible (only DOWN is excluded) — same as MA3."""
    reasons: List[ExclusionReason] = []
    if model.status != ModelStatus.ACTIVE:
        reasons.append(ExclusionReason.MODEL_INACTIVE)
    if provider.health_status == HealthStatus.DOWN:
        reasons.append(ExclusionReason.PROVIDER_UNAVAILABLE)
    if provider_model.availability_status == HealthStatus.DOWN:
        reasons.append(ExclusionReason.PROVIDER_MODEL_UNAVAILABLE)
    return reasons


def _policy_exclusions(
    policy: RouterFreePolicy, classification: PricingClassification
) -> List[ExclusionReason]:
    if policy == RouterFreePolicy.FREE_ONLY:
        if classification == PricingClassification.PAID:
            return [ExclusionReason.NOT_FREE]
        if classification == PricingClassification.UNKNOWN:
            return [ExclusionReason.PRICING_UNKNOWN]
        return []
    if policy == RouterFreePolicy.PREFER_FREE:
        if classification == PricingClassification.UNKNOWN:
            return [ExclusionReason.PRICING_UNKNOWN]
        return []
    if policy == RouterFreePolicy.ANY:
        return []
    raise ValueError(f"Unknown RouterFreePolicy: {policy!r}")  # pragma: no cover - closed enum


def _capability_exclusions(model: Model, policy: Dict[str, Any]) -> List[ExclusionReason]:
    """Apply normalized Model Registry capability constraints.

    Explicitly unsupported values do not satisfy a required capability.
    Unknown values remain unknown so older/manual registry entries are not
    silently reclassified; the caller only enables provider enforcement when
    support is explicitly confirmed.
    """
    required = policy.get("required_capabilities") or {}
    excluded = policy.get("excluded_capabilities") or {}
    for key, expected in required.items():
        actual = getattr(model, key, None)
        allowed = expected if isinstance(expected, list) else [expected]
        if actual is not None and actual not in allowed:
            return [ExclusionReason.CAPABILITY_UNSUPPORTED]
    for key, forbidden in excluded.items():
        actual = getattr(model, key, None)
        values = forbidden if isinstance(forbidden, list) else [forbidden]
        if actual in values:
            return [ExclusionReason.CAPABILITY_UNSUPPORTED]
    return []


def _verdict(
    provider_model: ProviderModel,
    model: Model,
    provider: Provider,
    classification: PricingClassification,
    reasons: List[ExclusionReason],
) -> CandidateVerdict:
    return CandidateVerdict(
        provider_model_id=provider_model.id,
        canonical_model_id=model.canonical_model_id,
        provider_id=provider.id,
        provider_name=provider.name,
        pricing_classification=classification,
        exclusion_reasons=tuple(reasons),
    )


def _tie_break_key(item: Tuple[ProviderModel, Model, Provider, PricingClassification]):
    provider_model, model, provider, classification = item
    cost = (
        provider_model.cost_input_per_mtok
        if provider_model.cost_input_per_mtok is not None
        else Decimal("Infinity")
    )
    # Deterministic, documented, and deliberately boring (TIE_BREAK_RULE):
    # tier (free before paid before unknown), then cheapest input price,
    # then stable identifiers. The last two keys only matter when one
    # canonical model is offered by several providers. Never a
    # learned/scored ranking.
    return (
        _TIER_ORDER[classification],
        cost,
        model.canonical_model_id,
        provider.name or "",
        provider_model.id,
    )


# -- routing ---------------------------------------------------------------------


def route(
    db: Session, model_policy: Optional[Dict[str, Any]], context: Optional[RoutingContext] = None
) -> RoutingDecision:
    """Resolve a model policy (the ``agent_versions.model_policy`` /
    ``agent_runs.model_policy_override_json`` shape) to a provider model.

    Returns a ``RoutingDecision`` with ``selected`` set, or raises
    ``ModelUnavailableError`` / ``NoEligibleModelError`` whose ``decision``
    describes the failed attempt. An absent ``mode`` means manual — the
    MA3 default.

    ``context`` (MA8.2) scopes historical evidence for AUTO routing. Without
    it, or without an active router policy version, AUTO routing is exactly
    MA8.1. MANUAL never consults evidence."""
    policy = model_policy or {}
    mode = policy.get("mode", ModelSelectionMode.MANUAL.value)

    if mode == ModelSelectionMode.MANUAL.value:
        provider_model_id = policy.get("manual_provider_model_id")
        if not provider_model_id:
            message = "Agent Version model_policy is manual but has no manual_provider_model_id."
            decision = RoutingDecision(
                selection_mode=ModelSelectionMode.MANUAL,
                requested_policy=None,
                reason=message,
                failure_code=ExclusionReason.MANUAL_MODEL_INVALID,
            )
            raise ModelUnavailableError(message, code=decision.failure_code, decision=decision)
        return route_manual(db, provider_model_id, policy=policy)

    if mode == ModelSelectionMode.AUTO.value:
        auto_policy_value = policy.get("auto_policy")
        try:
            free_policy = RouterFreePolicy(auto_policy_value) if auto_policy_value else None
        except ValueError:
            free_policy = None
        if free_policy is None:
            message = (
                "Agent Version model_policy is auto but has no valid auto_policy "
                f"(got {auto_policy_value!r})."
            )
            decision = RoutingDecision(
                selection_mode=ModelSelectionMode.AUTO,
                requested_policy=None,
                reason=message,
                failure_code=ExclusionReason.POLICY_INVALID,
            )
            raise ModelUnavailableError(message, code=decision.failure_code, decision=decision)
        return route_auto(db, free_policy, context=context, model_policy=policy)

    message = f"model_policy mode must be 'manual' or 'auto' (got {mode!r})."
    decision = RoutingDecision(
        selection_mode=ModelSelectionMode.MANUAL,
        requested_policy=None,
        reason=message,
        failure_code=ExclusionReason.POLICY_INVALID,
    )
    raise ModelUnavailableError(message, code=decision.failure_code, decision=decision)


def route_manual(
    db: Session, provider_model_id: str, *, policy: Optional[Dict[str, Any]] = None
) -> RoutingDecision:
    """Validate an operator-chosen provider model. Never substitutes: an
    ineligible manual choice fails with its reason."""

    def fail(message: str, code: ExclusionReason, verdict: CandidateVerdict) -> None:
        decision = RoutingDecision(
            selection_mode=ModelSelectionMode.MANUAL,
            requested_policy=None,
            reason=message,
            excluded=[verdict],
            requested_provider_model_id=provider_model_id,
            failure_code=code,
        )
        raise ModelUnavailableError(message, code=code, decision=decision)

    provider_model = db.get(ProviderModel, provider_model_id)
    model = db.get(Model, provider_model.model_id) if provider_model is not None else None
    provider = db.get(Provider, provider_model.provider_id) if provider_model is not None else None
    if provider_model is None or model is None or provider is None:
        verdict = CandidateVerdict(
            provider_model_id=provider_model_id,
            canonical_model_id=model.canonical_model_id if model is not None else None,
            provider_id=provider.id if provider is not None else None,
            provider_name=provider.name if provider is not None else None,
            pricing_classification=None,
            exclusion_reasons=(ExclusionReason.MODEL_NOT_FOUND,),
        )
        message = (
            f"Provider model {provider_model_id} does not exist."
            if provider_model is None
            else f"Provider model {provider_model_id} has a dangling model/provider reference."
        )
        fail(message, ExclusionReason.MODEL_NOT_FOUND, verdict)

    classification = classify_pricing(provider_model.cost_input_per_mtok, provider_model.cost_output_per_mtok)
    reasons = _catalog_exclusions(provider_model, model, provider)
    if not reasons:
        reasons.extend(_capability_exclusions(model, policy or {}))
    verdict = _verdict(provider_model, model, provider, classification, reasons)
    if ExclusionReason.MODEL_INACTIVE in reasons:
        fail(
            f"Model {model.canonical_model_id!r} is not active (status={model.status.value!r}).",
            ExclusionReason.MODEL_INACTIVE,
            verdict,
        )
    if ExclusionReason.PROVIDER_UNAVAILABLE in reasons:
        fail(f"Provider {provider.name!r} is currently down.", ExclusionReason.PROVIDER_UNAVAILABLE, verdict)
    if ExclusionReason.PROVIDER_MODEL_UNAVAILABLE in reasons:
        fail(
            f"Provider model {provider_model_id} ({model.canonical_model_id!r} via {provider.name!r}) "
            "is currently unavailable.",
            ExclusionReason.PROVIDER_MODEL_UNAVAILABLE,
            verdict,
        )
    if ExclusionReason.CAPABILITY_UNSUPPORTED in reasons:
        fail(
            f"Model {model.canonical_model_id!r} does not satisfy the requested capabilities.",
            ExclusionReason.CAPABILITY_UNSUPPORTED,
            verdict,
        )

    resolved = ResolvedModel(
        provider_model=provider_model,
        model=model,
        provider=provider,
        pricing_classification=classification,
        rationale=(
            f"manual selection of provider_model {provider_model_id} ({model.canonical_model_id!r} via "
            f"{provider.name!r}, pricing={classification.value}): operator-chosen model validated "
            "(model ACTIVE, provider and offering not DOWN); no automatic substitution"
        ),
        eligible_candidate_ids=[provider_model.id],
    )
    return RoutingDecision(
        selection_mode=ModelSelectionMode.MANUAL,
        requested_policy=None,
        reason=resolved.rationale,
        selected=resolved,
        eligible=[verdict],
        requested_provider_model_id=provider_model_id,
    )


def _all_candidates(db: Session) -> List[Tuple[ProviderModel, Model, Provider, PricingClassification]]:
    stmt = (
        select(ProviderModel, Model, Provider)
        .join(Model, ProviderModel.model_id == Model.id)
        .join(Provider, ProviderModel.provider_id == Provider.id)
    )
    return [
        (pm, model, provider, classify_pricing(pm.cost_input_per_mtok, pm.cost_output_per_mtok))
        for pm, model, provider in db.execute(stmt).all()
    ]


def route_auto(
    db: Session,
    policy: RouterFreePolicy,
    context: Optional[RoutingContext] = None,
    model_policy: Optional[Dict[str, Any]] = None,
) -> RoutingDecision:
    """Deterministic auto selection under a FREE_ONLY/PREFER_FREE/ANY policy,
    optionally reordered by MA8.2 evidence inside that policy's boundary."""
    eligible: List[Tuple[ProviderModel, Model, Provider, PricingClassification]] = []
    excluded: List[CandidateVerdict] = []
    catalog_eligible_count = 0
    for item in _all_candidates(db):
        provider_model, model, provider, classification = item
        reasons = _catalog_exclusions(provider_model, model, provider)
        if not reasons:
            reasons = _capability_exclusions(model, model_policy or {})
        if not reasons:
            catalog_eligible_count += 1
            reasons = _policy_exclusions(policy, classification)
        if reasons:
            excluded.append(_verdict(provider_model, model, provider, classification, reasons))
        else:
            eligible.append(item)

    eligible.sort(key=_tie_break_key)
    excluded.sort(key=lambda v: (v.canonical_model_id or "", v.provider_name or "", v.provider_model_id))

    if not eligible:
        if catalog_eligible_count == 0:
            message = "No active provider model is currently available from any enabled provider."
        elif policy == RouterFreePolicy.FREE_ONLY:
            message = (
                "FREE_ONLY policy: no currently eligible FREE model is available — refusing to "
                "silently fall back to a paid or unknown-priced model."
            )
        elif policy == RouterFreePolicy.PREFER_FREE:
            message = (
                "PREFER_FREE policy: no eligible FREE or PAID model is available. UNKNOWN-priced "
                "models are excluded from automatic selection because their cost cannot be budgeted."
            )
        else:  # pragma: no cover - ANY excludes nothing on pricing
            message = f"{policy.value} policy: no eligible model is available."
        decision = RoutingDecision(
            selection_mode=ModelSelectionMode.AUTO,
            requested_policy=policy,
            reason=f"{message} ({len(excluded)} candidate(s) excluded)",
            excluded=excluded,
            failure_code=ExclusionReason.NO_ELIGIBLE_MODEL,
        )
        raise NoEligibleModelError(message, code=decision.failure_code, decision=decision)

    deterministic_pick = eligible[0]
    evidence, active, profiles = _apply_evidence(db, policy, context, eligible)
    eligible_verdicts = [_verdict(pm, m, p, c, []) for pm, m, p, c in eligible]
    evidence_applied = evidence is not None and evidence["status"] == EVIDENCE_APPLIED

    provider_model, model, provider, classification = eligible[0]
    if policy == RouterFreePolicy.PREFER_FREE and classification != PricingClassification.FREE:
        policy_clause = "no eligible FREE model, so PREFER_FREE's explicit fallback to a PAID model was used"
    else:
        policy_clause = f"pricing {classification.value.upper()} is permitted by {policy.value.upper()}"
    if evidence_applied:
        ordering_clause = (
            f"ordered by {EVIDENCE_STRATEGY_V1} (router policy v{active.version}) inside the policy "
            f"boundary, ties by the {ROUTING_STRATEGY} tie-break ({TIE_BREAK_RULE})"
        )
    else:
        ordering_clause = f"ranked first by the {ROUTING_STRATEGY} tie-break ({TIE_BREAK_RULE})"
    rationale = (
        f"auto policy={policy.value} selected={model.canonical_model_id!r} via {provider.name!r} "
        f"tier={classification.value} among {len(eligible)} eligible of {len(eligible) + len(excluded)} "
        f"total candidates: model ACTIVE, provider and offering not DOWN, {policy_clause}; {ordering_clause}"
    )
    if evidence is not None and evidence["status"] != EVIDENCE_DISABLED:
        evidence["deterministic_pick_provider_model_id"] = deterministic_pick[0].id
        evidence["evidence_changed_selection"] = deterministic_pick[0].id != provider_model.id
        rationale += ". " + _evidence_explanation(evidence, active, profiles, eligible[0], deterministic_pick)
    resolved = ResolvedModel(
        provider_model=provider_model,
        model=model,
        provider=provider,
        pricing_classification=classification,
        rationale=rationale,
        eligible_candidate_ids=[c[0].id for c in eligible],
    )
    return RoutingDecision(
        selection_mode=ModelSelectionMode.AUTO,
        requested_policy=policy,
        reason=rationale,
        selected=resolved,
        eligible=eligible_verdicts,
        excluded=excluded,
        strategy=EVIDENCE_STRATEGY_V1 if evidence_applied else ROUTING_STRATEGY,
        router_policy_version_id=active.router_policy_version_id if active is not None else None,
        evidence=evidence,
    )


# -- MA8.2 evidence ordering ---------------------------------------------------------


def _evidence_key(policy: RouterFreePolicy, profile: EvidenceProfile, config, item):
    """Evidence only moves a candidate inside its policy's boundary:
    PREFER_FREE keeps every FREE candidate ahead of every PAID one, ANY keeps
    UNKNOWN pricing as the last resort (MA3), FREE_ONLY holds only FREE
    candidates. Remaining ties fall through to the MA8.1 key."""
    classification = item[3]
    if policy == RouterFreePolicy.PREFER_FREE:
        boundary = (0 if classification == PricingClassification.FREE else 1,)
    elif policy == RouterFreePolicy.ANY:
        boundary = (1 if classification == PricingClassification.UNKNOWN else 0,)
    else:
        boundary = ()
    return boundary + profile.sort_key(config) + _tie_break_key(item)


def _apply_evidence(db: Session, policy: RouterFreePolicy, context: Optional[RoutingContext], eligible):
    """Reorders ``eligible`` in place when evidence applies. Never raises:
    any failure keeps the MA8.1 order and is recorded as ``unavailable``.
    Returns (evidence record or None, active policy or None, profiles)."""
    if context is None:
        return None, None, {}
    try:
        active = load_active_policy(db)
    except Exception as exc:  # noqa: BLE001 - learning data must never block execution
        logger.warning("routing_evidence_policy_unavailable error=%s", type(exc).__name__)
        return {"status": EVIDENCE_UNAVAILABLE, "error": type(exc).__name__}, None, {}
    if active is None:
        return {"status": EVIDENCE_DISABLED}, None, {}

    config = active.config
    record: Dict[str, Any] = {
        "strategy": EVIDENCE_STRATEGY_V1,
        "router_policy_version": active.version,
        "config": config.to_json(),
        "agent_role": context.agent_role,
    }
    try:
        profiles = load_evidence(
            db, context=context, provider_model_ids=[item[0].id for item in eligible], config=config
        )
    except Exception as exc:  # noqa: BLE001 - learning data must never block execution
        logger.warning("routing_evidence_unavailable error=%s", type(exc).__name__)
        return {**record, "status": EVIDENCE_UNAVAILABLE, "error": type(exc).__name__}, active, {}

    record["profiles"] = [
        profiles[item[0].id].to_json(config)
        for item in eligible
        if profiles[item[0].id].observations or profiles[item[0].id].evaluated_runs
    ][:MAX_RECORDED_CANDIDATES]
    if not any(profile.is_sufficient(config) for profile in profiles.values()):
        record["status"] = EVIDENCE_INSUFFICIENT
        return record, active, profiles

    eligible.sort(key=lambda item: _evidence_key(policy, profiles[item[0].id], config, item))
    record["status"] = EVIDENCE_APPLIED
    return record, active, profiles


def _describe_profile(profile: Optional[EvidenceProfile], config) -> str:
    if profile is None or not (profile.observations or profile.evaluated_runs):
        return "no relevant history"
    parts = [
        (
            f"{profile.observations} observed call(s) ({profile.completed} completed, "
            f"{profile.provider_failures} provider failure(s)), reliability {profile.reliability(config)}"
        ),
        (
            f"{profile.evaluated_runs} evaluated run(s) (MET {profile.met}, PARTIAL {profile.partial}, "
            f"NOT_MET {profile.not_met}; {profile.not_applicable} NOT_APPLICABLE not counted), "
            f"quality {profile.quality(config)}"
        ),
    ]
    if profile.latencies_ms:
        parts.append(f"median latency {int(median(profile.latencies_ms))} ms")
    return "; ".join(parts)


def _evidence_explanation(evidence, active, profiles, selected, deterministic_pick) -> str:
    if evidence["status"] == EVIDENCE_UNAVAILABLE:
        return "Evidence routing: evidence could not be loaded, so the MA8.1 order was used"
    config = active.config
    scope = (
        f"{EVIDENCE_STRATEGY_V1}, role={evidence['agent_role']!r}, last {config.history_window_days} "
        f"day(s), minimum {config.min_observations} call(s) or {config.min_evaluated_runs} evaluated run(s)"
    )
    if evidence["status"] == EVIDENCE_INSUFFICIENT:
        return (
            f"Evidence routing ({scope}): no candidate has the minimum evidence, so the MA8.1 order was "
            "used; selected model: " + _describe_profile(profiles.get(selected[0].id), config)
        )
    text = f"Evidence routing ({scope}); observed evidence for the selected model: " + _describe_profile(
        profiles.get(selected[0].id), config
    )
    if deterministic_pick[0].id != selected[0].id:
        text += (
            f". The MA8.1 order alone would have selected {deterministic_pick[1].canonical_model_id!r}, which "
            "remained eligible: " + _describe_profile(profiles.get(deterministic_pick[0].id), config)
        )
    return text


def resolve_manual(db: Session, provider_model_id: str) -> ResolvedModel:
    """MA3 contract: validate a manual choice, raising ModelUnavailableError."""
    return route_manual(db, provider_model_id).selected


def resolve_auto(db: Session, policy: RouterFreePolicy) -> ResolvedModel:
    """MA3 contract: auto-select under ``policy``, raising NoEligibleModelError."""
    return route_auto(db, policy).selected


# -- persistence -------------------------------------------------------------------


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


def record_routing_decision(
    db: Session,
    *,
    agent_run_id: str,
    decision: RoutingDecision,
    snapshot: Optional[ProviderModelSnapshot] = None,
    agent_run_attempt_id: Optional[str] = None,
) -> ModelRoutingDecision:
    """Persist one routing decision (successful or failed) for an Agent Run
    attempt. Rows are append-only: a retry routes again and gets its own
    row; earlier rows are never modified. ``router_policy_version_id`` is the
    MA8.2 policy version consulted for evidence (NULL for MANUAL, or when none
    is active); ``routing_strategy`` is the algorithm that ordered the
    candidates."""
    details = decision.details_json()
    details["agent_run_attempt_id"] = agent_run_attempt_id
    row = ModelRoutingDecision(
        agent_run_id=agent_run_id,
        router_policy_version_id=decision.router_policy_version_id,
        selection_mode=decision.selection_mode,
        requested_policy=decision.requested_policy.value if decision.requested_policy else None,
        routing_strategy=decision.strategy,
        eligible_candidates=[v.to_json() for v in decision.eligible[:MAX_RECORDED_CANDIDATES]],
        selected_provider_model_snapshot_id=snapshot.id if snapshot is not None else None,
        rationale=decision.reason[:2000],
        details=details,
    )
    db.add(row)
    db.flush()
    return row
