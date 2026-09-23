"""Deterministic services for the AIL.2A evidence ledger."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import ClassVar, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.artifacts_eval import Evaluation
from app.models.providers import Model
from app.models.radar import (
    Claim,
    ClaimCitation,
    ClaimCreationMethod,
    ClaimOrigin,
    ClaimOriginKind,
    ClaimStatus,
    ClaimType,
    Development,
    DevelopmentModel,
    DevelopmentStatus,
    RadarItem,
    RadarItemState,
    RadarSource,
    RadarSourceClass,
    RadarSourceState,
)
from app.models.tasks import AgentRun


class RadarValidationError(ValueError):
    pass


class Freshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"
    SUPERSEDED = "superseded"


VERIFICATION_LEVELS = (
    "Claimed",
    "Documented",
    "Available",
    "Independently Measured",
    "Tested by Us",
)


def normalize_content(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\r\n", "\n")).strip()


def content_hash(value: str) -> str:
    return hashlib.sha256(normalize_content(value).encode("utf-8")).hexdigest()


def candidate_key(*, development_type: str, subject_key: str, effective_at: Optional[datetime], change_key: str) -> str:
    payload = {
        "development_type": development_type.strip().lower(),
        "subject_key": subject_key.strip().lower(),
        "effective_at": effective_at.isoformat() if effective_at else None,
        "change_key": change_key.strip().lower(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RadarSourceService:
    def __init__(self, db: Session):
        self.db = db

    def activate(self, source: RadarSource) -> RadarSource:
        if not source.owner_reviewed_at or not source.tos_reviewed_at:
            raise RadarValidationError("A source requires owner and ToS review before activation.")
        source.state = RadarSourceState.ACTIVE
        return source

    def assert_fetchable(self, source: RadarSource) -> None:
        if source.state != RadarSourceState.ACTIVE:
            raise RadarValidationError("Only active curated sources may be fetched.")


class RadarItemService:
    def __init__(self, db: Session):
        self.db = db

    def create_item(
        self,
        *,
        source: RadarSource,
        title: str,
        normalized_content: str,
        canonical_url: Optional[str] = None,
        external_identity: Optional[str] = None,
        published_at: Optional[datetime] = None,
        storage_ref: Optional[str] = None,
    ) -> RadarItem:
        RadarSourceService(self.db).assert_fetchable(source)
        normalized = normalize_content(normalized_content)
        digest = content_hash(normalized)
        existing = self.db.execute(
            select(RadarItem).where(
                RadarItem.source_id == source.id,
                RadarItem.content_hash == digest,
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        item = RadarItem(
            source_id=source.id,
            title=title.strip(),
            normalized_content=normalized,
            content_hash=digest,
            canonical_url=canonical_url,
            external_identity=external_identity,
            published_at=published_at,
            storage_ref=storage_ref,
            processing_state=RadarItemState.RECEIVED,
        )
        self.db.add(item)
        self.db.flush()
        return item

    def validate_quote(self, item: RadarItem, quote_span: Optional[str]) -> None:
        if quote_span is not None and quote_span not in item.normalized_content:
            raise RadarValidationError("quote_span is not an exact substring of immutable source content.")


class RadarClaimService:
    _CEILING: ClassVar[dict] = {
        RadarSourceClass.S1: {ClaimType.FACT},
        RadarSourceClass.S2: {ClaimType.PROVIDER_CLAIM, ClaimType.FACT},
        RadarSourceClass.S3: {ClaimType.RESEARCH_RESULT},
        RadarSourceClass.S4: {ClaimType.BENCHMARK_RESULT},
        RadarSourceClass.S5: {ClaimType.COMMUNITY_SIGNAL},
    }

    def __init__(self, db: Session):
        self.db = db

    def _validate_source_claim(self, claim_type: ClaimType, source_item: RadarItem) -> None:
        source = self.db.get(RadarSource, source_item.source_id)
        if source is None or source.source_class not in self._CEILING:
            raise RadarValidationError("This claim type cannot originate from an internal source class.")
        if claim_type not in self._CEILING[source.source_class]:
            raise RadarValidationError("Claim type exceeds the source-class ceiling.")
        if claim_type == ClaimType.BENCHMARK_RESULT and source.source_class == RadarSourceClass.S2:
            raise RadarValidationError("Provider-authored benchmarks must be PROVIDER_CLAIM.")

    def create_claim(
        self,
        *,
        claim_type: ClaimType,
        text: str,
        as_of: datetime,
        created_by: ClaimCreationMethod,
        development_id: Optional[str] = None,
        model_id: Optional[str] = None,
        quote_span: Optional[str] = None,
        conditions: Optional[dict] = None,
        source_item_id: Optional[str] = None,
        evaluation_id: Optional[str] = None,
        agent_run_id: Optional[str] = None,
        cited_claim_ids: Optional[list] = None,
    ) -> Claim:
        if not development_id and not model_id:
            raise RadarValidationError("A claim requires a Development or canonical Model subject.")
        if model_id and self.db.get(Model, model_id) is None:
            raise RadarValidationError("Unknown canonical model identity.")
        origins = [source_item_id, evaluation_id, agent_run_id]
        if sum(value is not None for value in origins) != 1:
            raise RadarValidationError("A claim must have exactly one origin.")
        if source_item_id:
            item = self.db.get(RadarItem, source_item_id)
            if item is None:
                raise RadarValidationError("Unknown source item.")
            self._validate_source_claim(claim_type, item)
            RadarItemService(self.db).validate_quote(item, quote_span)
        elif evaluation_id:
            if claim_type != ClaimType.PLATFORM_OBSERVATION:
                raise RadarValidationError("Platform evaluations may only originate PLATFORM_OBSERVATION claims.")
            evaluation = self.db.get(Evaluation, evaluation_id)
            subject_run = self.db.get(AgentRun, evaluation.agent_run_id) if evaluation else None
            if evaluation is None or subject_run is None or subject_run.status.value != "completed":
                raise RadarValidationError("PLATFORM_OBSERVATION requires a completed Evaluation Run.")
            if not conditions or not conditions.get("sample_size") or not conditions.get("slice"):
                raise RadarValidationError("PLATFORM_OBSERVATION requires sample_size and slice conditions.")
        elif agent_run_id:
            if claim_type != ClaimType.AI_EXPLANATION:
                raise RadarValidationError("AIL Agent Run origins may only create AI_EXPLANATION claims.")
            run = self.db.get(AgentRun, agent_run_id)
            if run is None or run.status.value != "completed":
                raise RadarValidationError("AI_EXPLANATION requires a completed Agent Run.")
        claim = Claim(
            claim_type=claim_type,
            text=text.strip(),
            development_id=development_id,
            model_id=model_id,
            quote_span=quote_span,
            conditions=conditions,
            as_of=as_of,
            created_by=created_by,
        )
        self.db.add(claim)
        self.db.flush()
        self.db.add(
            ClaimOrigin(
                claim_id=claim.id,
                origin_kind=(
                    ClaimOriginKind.SOURCE_ITEM
                    if source_item_id
                    else ClaimOriginKind.PLATFORM_EVALUATION
                    if evaluation_id
                    else ClaimOriginKind.AIL_AGENT_RUN
                ),
                source_item_id=source_item_id,
                evaluation_id=evaluation_id,
                agent_run_id=agent_run_id,
            )
        )
        if claim_type == ClaimType.AI_EXPLANATION:
            if not cited_claim_ids:
                raise RadarValidationError("AI_EXPLANATION requires citations.")
            for cited_id in cited_claim_ids:
                cited = self.db.get(Claim, cited_id)
                if cited is None or cited.id == claim.id or cited.claim_type == ClaimType.AI_EXPLANATION:
                    raise RadarValidationError("AI_EXPLANATION citations must reference existing non-AI claims.")
                self.db.add(ClaimCitation(explanation_claim_id=claim.id, cited_claim_id=cited.id))
        return claim

    def merge_developments(self, source: Development, target: Development) -> Development:
        if source.id == target.id:
            raise RadarValidationError("A Development cannot be merged into itself.")
        if source.status == DevelopmentStatus.MERGED:
            raise RadarValidationError("A merged Development cannot be merged again.")
        source.merged_into_id = target.id
        source.status = DevelopmentStatus.MERGED
        return source


class RadarDevelopmentService:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create_candidate(
        self,
        *,
        title: str,
        development_type: str,
        subject_key: str,
        effective_at: Optional[datetime],
        change_key: str,
        announced_at: Optional[datetime] = None,
    ) -> Development:
        key = candidate_key(
            development_type=development_type,
            subject_key=subject_key,
            effective_at=effective_at,
            change_key=change_key,
        )
        existing = self.db.execute(
            select(Development).where(Development.candidate_key == key)
        ).scalar_one_or_none()
        if existing:
            return existing
        development = Development(
            title=title.strip(),
            development_type=development_type.strip(),
            announced_at=announced_at,
            effective_at=effective_at,
            candidate_key=key,
        )
        self.db.add(development)
        self.db.flush()
        return development


def derive_verification(db: Session, development_id: str) -> str:
    development = db.get(Development, development_id)
    if development is None:
        raise RadarValidationError("Unknown Development.")
    claims = list(
        db.execute(select(Claim).where(Claim.development_id == development_id, Claim.status == ClaimStatus.ACTIVE))
        .scalars()
        .all()
    )
    types = {claim.claim_type for claim in claims}
    if any(claim.claim_type == ClaimType.PLATFORM_OBSERVATION for claim in claims):
        return "Tested by Us"
    if any(claim.claim_type == ClaimType.BENCHMARK_RESULT for claim in claims):
        return "Independently Measured"
    if development_id and db.execute(select(DevelopmentModel).where(DevelopmentModel.development_id == development_id)).first():
        model_ids = db.execute(select(DevelopmentModel.model_id).where(DevelopmentModel.development_id == development_id)).scalars().all()
        if any((db.get(Model, model_id) is not None and db.get(Model, model_id).status.value == "active") for model_id in model_ids):
            return "Available"
    if ClaimType.FACT in types:
        return "Documented"
    return "Claimed"


def derive_freshness(source: Optional[RadarSource], claim: Optional[Claim] = None, now: Optional[datetime] = None) -> Freshness:
    if claim and claim.status == ClaimStatus.SUPERSEDED:
        return Freshness.SUPERSEDED
    if source is None or source.last_success_at is None or not source.cadence_minutes:
        return Freshness.UNKNOWN
    current = now or datetime.now(timezone.utc)
    return (
        Freshness.STALE
        if current - source.last_success_at > timedelta(minutes=source.cadence_minutes * 3)
        else Freshness.CURRENT
    )
