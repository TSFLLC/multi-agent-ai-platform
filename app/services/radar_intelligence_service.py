"""Deterministic AIL.2B Radar intelligence services."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.enums import HealthStatus, ModelStatus, PlanItemState
from app.errors import ConflictError, ForbiddenError, InvalidStateTransitionError, NotFoundError
from app.models.artifacts_eval import Evaluation
from app.models.taxonomy import TaxonomyTerm
from app.models.execution import ModelCall
from app.models.identity import Project, ProjectMembership, User
from app.models.providers import Model, Provider, ProviderModel
from app.models.radar import (
    AttentionSample,
    AttentionState,
    Claim,
    ClaimOrigin,
    ClaimStatus,
    ClaimType,
    Development,
    DevelopmentConcept,
    DevelopmentConceptState,
    DevelopmentModel,
    DevelopmentTerm,
    RadarItem,
    RadarItemState,
    RadarReasonCode,
    RadarSource,
    RadarSourceClass,
    TriageDecision,
    TriageDecisionKind,
)
from app.models.tasks import AgentRun, Task, TaskRun
from app.models.learner import LearnerInterest, LearningPlanItem
from app.services.radar_service import (
    RadarClaimService,
    RadarItemService,
    RadarSourceService,
    RadarValidationError,
    Freshness,
    derive_freshness,
    derive_verification,
    content_hash,
    normalize_content,
)


VALID_TERM_VOCABULARIES = frozenset(
    {"lane", "topic", "capability", "platform_component", "provider", "role"}
)


@dataclass(frozen=True)
class AttentionMetricPolicy:
    rising_ratio: Decimal
    high_value: Decimal
    sustained_windows: int = 3


# These are measurement policies, not a universal popularity score. Unknown
# metrics intentionally produce UNKNOWN until a source/metric policy is added.
ATTENTION_METRIC_POLICIES: Dict[str, AttentionMetricPolicy] = {
    "mentions": AttentionMetricPolicy(Decimal("0.25"), Decimal("100")),
    "downloads": AttentionMetricPolicy(Decimal("0.25"), Decimal("1000")),
    "likes": AttentionMetricPolicy(Decimal("0.25"), Decimal("500")),
    "stars": AttentionMetricPolicy(Decimal("0.25"), Decimal("500")),
}


class RadarIntelligenceService:
    def __init__(self, db: Session):
        self.db = db

    def add_term(self, development_id: str, term_id: str, created_by: str) -> DevelopmentTerm:
        development = self.db.get(Development, development_id)
        term = self.db.get(TaxonomyTerm, term_id)
        if development is None:
            raise NotFoundError(f"Development {development_id} not found.")
        if term is None or term.vocabulary not in VALID_TERM_VOCABULARIES:
            raise RadarValidationError("The term is not from an allowed Radar vocabulary.")
        existing = self.db.execute(
            select(DevelopmentTerm).where(
                DevelopmentTerm.development_id == development_id,
                DevelopmentTerm.term_id == term_id,
            )
        ).scalar_one_or_none()
        if existing:
            raise ConflictError("This Development-term link already exists.")
        link = DevelopmentTerm(development_id=development_id, term_id=term_id, created_by=created_by)
        self.db.add(link)
        self.db.flush()
        return link

    def create_attention_sample(
        self,
        *,
        source_id: str,
        metric: str,
        value: Decimal,
        sampled_at: datetime,
        development_id: Optional[str] = None,
        model_id: Optional[str] = None,
        unit: Optional[str] = None,
        measurement_metadata: Optional[dict] = None,
        external_identity: Optional[str] = None,
        sample_hash: Optional[str] = None,
    ) -> AttentionSample:
        source = self.db.get(RadarSource, source_id)
        if source is None:
            raise NotFoundError(f"Radar source {source_id} not found.")
        if source.source_class != RadarSourceClass.S5 or source.state.value != "active":
            raise RadarValidationError("Attention samples require an active S5 source.")
        if self._exactly_one(development_id, model_id) is False:
            raise RadarValidationError("An attention sample requires exactly one subject.")
        if development_id and self.db.get(Development, development_id) is None:
            raise NotFoundError(f"Development {development_id} not found.")
        if model_id and self.db.get(Model, model_id) is None:
            raise NotFoundError(f"Model {model_id} not found.")
        if not metric.strip() or metric.strip().lower() not in ATTENTION_METRIC_POLICIES:
            raise RadarValidationError("No deterministic policy is configured for this attention metric.")
        duplicate = self.db.execute(
            select(AttentionSample).where(
                AttentionSample.source_id == source_id,
                AttentionSample.metric == metric.strip().lower(),
                AttentionSample.sampled_at == sampled_at,
                AttentionSample.external_identity == external_identity,
                AttentionSample.development_id == development_id,
                AttentionSample.model_id == model_id,
            )
        ).scalar_one_or_none()
        if duplicate:
            return duplicate
        sample = AttentionSample(
            development_id=development_id,
            model_id=model_id,
            source_id=source_id,
            metric=metric.strip().lower(),
            value=value,
            unit=unit,
            measurement_metadata=measurement_metadata,
            sampled_at=sampled_at,
            external_identity=external_identity,
            sample_hash=sample_hash,
        )
        self.db.add(sample)
        self.db.flush()
        return sample

    def attention_state(
        self,
        *,
        development_id: Optional[str] = None,
        model_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> AttentionState:
        if not self._exactly_one(development_id, model_id):
            raise RadarValidationError("Attention state requires exactly one subject.")
        subject_column = AttentionSample.development_id if development_id else AttentionSample.model_id
        subject_id = development_id or model_id
        samples = list(
            self.db.execute(
                select(AttentionSample)
                .where(subject_column == subject_id)
                .order_by(AttentionSample.sampled_at.desc())
            )
            .scalars()
            .all()
        )
        if not samples:
            return AttentionState.UNKNOWN
        policy = ATTENTION_METRIC_POLICIES.get(samples[0].metric)
        if policy is None:
            return AttentionState.UNKNOWN
        current_time = self._aware(now or datetime.now(timezone.utc))
        recent = [s for s in samples if current_time - self._aware(s.sampled_at) <= timedelta(days=7)]
        if not recent:
            return AttentionState.UNKNOWN
        latest = Decimal(str(recent[0].value))
        if self._sustained(samples, policy, current_time):
            return AttentionState.SUSTAINED
        if latest >= policy.high_value:
            return AttentionState.HIGH
        prior = next(
            (s for s in samples if self._aware(s.sampled_at) < self._aware(recent[0].sampled_at) - timedelta(days=1)),
            None,
        )
        if prior and Decimal(str(prior.value)) > 0 and latest >= Decimal(str(prior.value)) * (1 + policy.rising_ratio):
            return AttentionState.RISING
        return AttentionState.LOW

    def development_freshness(self, development_id: str, *, now: Optional[datetime] = None) -> Freshness:
        development = self.db.get(Development, development_id)
        if development is None:
            raise NotFoundError(f"Development {development_id} not found.")
        if development.status.value == "merged":
            return Freshness.SUPERSEDED
        rows = self.db.execute(
            select(RadarSource, Claim)
            .join(RadarItem, RadarItem.source_id == RadarSource.id)
            .join(ClaimOrigin, ClaimOrigin.source_item_id == RadarItem.id)
            .join(Claim, Claim.id == ClaimOrigin.claim_id)
            .where(Claim.development_id == development_id, Claim.status == ClaimStatus.ACTIVE)
        ).all()
        if not rows:
            return Freshness.UNKNOWN
        states = {derive_freshness(source, claim, now=now) for source, claim in rows}
        if Freshness.CURRENT in states:
            return Freshness.CURRENT
        if states and states.issubset({Freshness.STALE, Freshness.SUPERSEDED}):
            return Freshness.STALE
        return Freshness.UNKNOWN

    def _sustained(self, samples: List[AttentionSample], policy: AttentionMetricPolicy, now: datetime) -> bool:
        windows = set()
        for sample in samples:
            age = now - self._aware(sample.sampled_at)
            if age < timedelta(days=0) or age > timedelta(days=28):
                continue
            if Decimal(str(sample.value)) >= policy.high_value:
                windows.add(age.days // 7)
        return len(windows) >= policy.sustained_windows

    def create_triage(
        self,
        *,
        user_id: str,
        decision: TriageDecisionKind,
        reason_codes: List[str],
        rationale: Optional[str] = None,
        revisit_at: Optional[datetime] = None,
        revisit_condition: Optional[dict] = None,
        development_id: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> TriageDecision:
        if not self._exactly_one(development_id, model_id):
            raise RadarValidationError("A triage decision requires exactly one target.")
        if development_id and self.db.get(Development, development_id) is None:
            raise NotFoundError(f"Development {development_id} not found.")
        if model_id and self.db.get(Model, model_id) is None:
            raise NotFoundError(f"Model {model_id} not found.")
        valid_codes = {code.value for code in RadarReasonCode}
        if any(code not in valid_codes for code in reason_codes):
            raise RadarValidationError("Triage contains an unknown reason code.")
        if decision == TriageDecisionKind.WATCH and revisit_at is None and not revisit_condition:
            raise RadarValidationError("WATCH requires revisit_at or revisit_condition.")
        if revisit_condition is not None and not self._valid_revisit_condition(revisit_condition):
            raise RadarValidationError("Invalid revisit condition.")

        active = self._active_triage(user_id, development_id, model_id)
        if active:
            # Temporarily self-supersede to release the partial unique index;
            # the final pointer is replaced immediately in the same transaction.
            active.superseded_by_id = active.id
            self.db.flush()
        triage = TriageDecision(
            user_id=user_id,
            development_id=development_id,
            model_id=model_id,
            decision=decision,
            rationale=rationale,
            reason_codes=sorted(set(reason_codes)),
            revisit_at=revisit_at,
            revisit_condition=revisit_condition,
        )
        self.db.add(triage)
        self.db.flush()
        if active:
            active.superseded_by_id = triage.id
        return triage

    def current_triage(self, user_id: str, *, development_id: Optional[str] = None, model_id: Optional[str] = None):
        return self._active_triage(user_id, development_id, model_id)

    def triage_history(self, user_id: str, *, development_id: Optional[str] = None, model_id: Optional[str] = None):
        if not self._exactly_one(development_id, model_id):
            raise RadarValidationError("Triage history requires exactly one target.")
        target = TriageDecision.development_id == development_id if development_id else TriageDecision.model_id == model_id
        return list(
            self.db.execute(
                select(TriageDecision)
                .where(TriageDecision.user_id == user_id, target)
                .order_by(TriageDecision.decided_at.desc())
            ).scalars().all()
        )

    def relevance_reason_codes(self, user_id: str, development_id: str, *, now: Optional[datetime] = None) -> List[RadarReasonCode]:
        development = self.db.get(Development, development_id)
        if development is None:
            raise NotFoundError(f"Development {development_id} not found.")
        reasons: Set[RadarReasonCode] = set()
        type_key = development.development_type.lower()
        type_reasons = {
            "model_release": RadarReasonCode.NEW_MODEL,
            "pricing": RadarReasonCode.PRICE_CHANGE,
            "capability": RadarReasonCode.CAPABILITY_CHANGE,
            "context": RadarReasonCode.CONTEXT_CHANGE,
            "status": RadarReasonCode.STATUS_CHANGE,
            "research": RadarReasonCode.NEW_RESEARCH,
            "benchmark": RadarReasonCode.NEW_BENCHMARK,
        }
        if type_key in type_reasons:
            reasons.add(type_reasons[type_key])
        verification = derive_verification(self.db, development_id)
        if verification in {"Claimed", "Documented"}:
            reasons.add(RadarReasonCode.NEEDS_VERIFICATION)
        if self.attention_state(development_id=development_id, now=now) == AttentionState.RISING:
            reasons.add(RadarReasonCode.ATTENTION_RISING)
        if self._has_stale_source(development_id):
            reasons.add(RadarReasonCode.SOURCE_STALE)
        if self._has_conflicting_claims(development_id):
            reasons.add(RadarReasonCode.CONFLICTING_CLAIMS)
        if self.current_triage(user_id, development_id=development_id) is None:
            reasons.add(RadarReasonCode.UNREVIEWED)
        model_ids = set(self.db.execute(select(DevelopmentModel.model_id).where(DevelopmentModel.development_id == development_id)).scalars())
        used_models, used_providers = self._used_models_and_providers(user_id)
        if model_ids & used_models:
            reasons.add(RadarReasonCode.RELATED_TO_USED_MODEL)
        if model_ids & {m for m, _ in self._model_provider_pairs(model_ids, used_providers)}:
            reasons.add(RadarReasonCode.RELATED_TO_USED_PROVIDER)
        concept_ids = set(self.db.execute(select(DevelopmentConcept.concept_id).where(DevelopmentConcept.development_id == development_id, DevelopmentConcept.state == DevelopmentConceptState.CONFIRMED)).scalars())
        interests = self.db.execute(select(LearnerInterest).where(LearnerInterest.user_id == user_id)).scalars().all()
        interest_concepts = {i.concept_id for i in interests if i.concept_id}
        interest_terms = {i.term_id for i in interests if i.term_id}
        term_ids = set(self.db.execute(select(DevelopmentTerm.term_id).where(DevelopmentTerm.development_id == development_id)).scalars())
        if concept_ids & interest_concepts or term_ids & interest_terms:
            reasons.add(RadarReasonCode.RELATED_TO_INTEREST)
        plan_ids = set(self.db.execute(select(LearningPlanItem.concept_id).where(LearningPlanItem.user_id == user_id, LearningPlanItem.state.in_([PlanItemState.PROPOSED, PlanItemState.PLANNED]))).scalars())
        if concept_ids & plan_ids:
            reasons.add(RadarReasonCode.RELATED_TO_LEARNING_PLAN)
        if model_ids:
            for provider_id in self.db.execute(select(ProviderModel.provider_id).where(ProviderModel.model_id.in_(model_ids))).scalars():
                if provider_id in used_providers:
                    reasons.add(RadarReasonCode.RELATED_TO_USED_PROVIDER)
                    break
        return sorted(reasons, key=lambda item: item.value)

    def _has_stale_source(self, development_id: str) -> bool:
        rows = self.db.execute(
            select(RadarSource, Claim).join(RadarItem, RadarItem.source_id == RadarSource.id).join(ClaimOrigin, ClaimOrigin.source_item_id == RadarItem.id).join(Claim, Claim.id == ClaimOrigin.claim_id).where(Claim.development_id == development_id, Claim.status == ClaimStatus.ACTIVE)
        ).all()
        return any(derive_freshness(source, claim) .value == "stale" for source, claim in rows)

    def _has_conflicting_claims(self, development_id: str) -> bool:
        claims = self.db.execute(select(Claim).where(Claim.development_id == development_id, Claim.status.in_([ClaimStatus.ACTIVE, ClaimStatus.DISPUTED]))).scalars().all()
        if any(c.status == ClaimStatus.DISPUTED for c in claims):
            return True
        groups = {}
        for claim in claims:
            key = (claim.conditions or {}).get("conflict_key")
            if key:
                groups.setdefault(key, set()).add(claim.text.strip())
        return any(len(values) > 1 for values in groups.values())

    def _used_models_and_providers(self, user_id: str):
        rows = self.db.execute(
            select(ModelCall.model_id, ModelCall.provider_id)
            .join(AgentRun, AgentRun.id == ModelCall.agent_run_id)
            .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
            .join(Task, Task.id == TaskRun.task_id)
            .join(Project, Project.id == Task.project_id)
            .join(ProjectMembership, ProjectMembership.project_id == Project.id)
            .where(ProjectMembership.user_id == user_id, Project.ail_evidence_opt_in.is_(True))
        ).all()
        return {row[0] for row in rows}, {row[1] for row in rows}

    def _model_provider_pairs(self, model_ids, provider_ids):
        return self.db.execute(select(ProviderModel.model_id, ProviderModel.provider_id).where(ProviderModel.model_id.in_(model_ids), ProviderModel.provider_id.in_(provider_ids))).all()

    def _active_triage(self, user_id, development_id, model_id):
        target = TriageDecision.development_id == development_id if development_id else TriageDecision.model_id == model_id
        return self.db.execute(select(TriageDecision).where(TriageDecision.user_id == user_id, target, TriageDecision.superseded_by_id.is_(None))).scalar_one_or_none()

    @staticmethod
    def _exactly_one(first, second) -> bool:
        return (first is not None) != (second is not None)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    @staticmethod
    def _valid_revisit_condition(condition: dict) -> bool:
        return isinstance(condition, dict) and condition.get("kind") in {"verification_at_least", "attention_state", "date"}


def create_manual_radar_item(
    db: Session,
    *,
    source: RadarSource,
    title: str,
    canonical_url: str,
    normalized_content: str,
    external_identity: Optional[str] = None,
    published_at: Optional[datetime] = None,
    storage_ref: Optional[str] = None,
) -> RadarItem:
    """Create an immutable item only for the registered source's URL policy."""
    parsed = urlsplit(canonical_url)
    source_url = urlsplit(source.endpoint_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RadarValidationError("Manual Radar URLs must be credential-free HTTP(S) URLs.")
    if not source_url.hostname or parsed.hostname != source_url.hostname:
        raise RadarValidationError("The canonical URL is outside the registered Source policy.")
    existing_identity = None
    if external_identity:
        existing_identity = db.execute(select(RadarItem).where(RadarItem.source_id == source.id, RadarItem.external_identity == external_identity)).scalar_one_or_none()
        if existing_identity and existing_identity.content_hash != content_hash(normalized_content):
            raise ConflictError("An external identity cannot point to changed immutable content.")
    return RadarItemService(db).create_item(
        source=source,
        title=title,
        normalized_content=normalized_content,
        canonical_url=canonical_url,
        external_identity=external_identity,
        published_at=published_at,
        storage_ref=storage_ref,
    )
