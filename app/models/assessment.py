"""AIL.5C Assessment + Demonstration entities.

Five tables, nothing else. There is deliberately NO mastery table, score,
learner-state cache or second evidence store: an assessment can only append
``learning_evidence`` through ``AssessmentEvidenceWriter`` and
``LearnerStateService`` remains the only place a ladder rung is derived.

Ownership / versioning / immutability / retention (per table):

``assessment_definitions``  Academy-owned, learner-facing rubric + challenge
    unit. Row-per-version and immutable once published (enforced by an ORM
    guard AND a frozen ``content_hash``); retired rows stay for history.
``assessment_definition_concepts``  Relationship rows pinning the exact
    ``concept_versions`` row each definition assesses. Immutable with the
    definition.
``assessment_attempts``  Learner-owned (``users.id``), never project-scoped.
    ``draft_json`` is mutable only while DRAFT; everything else is write-once
    at submit. Pins every version an attempt ran against.
``assessment_results``  Append-only stage / final / human outputs. Never
    updated (ORM guard). A correction appends a ``human`` row that points at
    the row it supersedes; history is never rewritten.
``assessment_reviews``  Consent-bound human review. Reuses the Approval CAS +
    action-fingerprint *pattern* but not the Approval engine, whose
    authorization is project RBAC (every learner owns the shared AIL project).

All rows cascade on learner deletion (spec Sec 30); unsubmitted drafts are
purged after 30 days by the retention sweep (the attempt row is kept as
ABANDONED).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import (
    AssessmentAttemptStatus,
    AssessmentDefinitionStatus,
    AssessmentKind,
    AssessmentOrigin,
    AssessmentOutcome,
    AssessmentResultKind,
    AssessmentReviewDecision,
    AssessmentReviewStatus,
    AssessmentReviewTrigger,
    DemonstrationEffect,
    EvidenceType,
)
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum

ACTIVE_ATTEMPT_STATUSES = ("draft", "submitted", "checking", "awaiting_grading", "grading")


class AssessmentDefinition(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One immutable version of a learner-facing assessment."""

    __tablename__ = "assessment_definitions"
    __table_args__ = (
        UniqueConstraint("definition_key", "version", name="uq_assessment_definitions_key_version"),
    )

    definition_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AssessmentDefinitionStatus] = mapped_column(
        sa_enum(AssessmentDefinitionStatus), nullable=False, default=AssessmentDefinitionStatus.DRAFT
    )
    assessment_kind: Mapped[AssessmentKind] = mapped_column(sa_enum(AssessmentKind), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    instructions_md: Mapped[str] = mapped_column(Text, nullable=False)
    produces_evidence_type: Mapped[EvidenceType] = mapped_column(sa_enum(EvidenceType), nullable=False)
    project_template_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("project_templates.id", ondelete="RESTRICT"), nullable=True
    )
    # Ordered list of criteria: {key, label, method: deterministic|grader,
    # required, description, check_spec, anchors, reference_points, facts,
    # on_not_met}. Versioned with the definition, never edited in place.
    criteria: Mapped[list] = mapped_column("criteria_json", nullable=False, default=list)
    # Authored variant pool + input pools; the source of deterministic fresh
    # challenge selection (never model-generated).
    challenge_spec: Mapped[Optional[dict]] = mapped_column("challenge_spec_json", nullable=True)
    independence_policy: Mapped[dict] = mapped_column(
        "independence_policy_json", nullable=False, default=dict
    )
    grading_policy: Mapped[dict] = mapped_column("grading_policy_json", nullable=False, default=dict)
    allowed_resources: Mapped[list] = mapped_column("allowed_resources_json", nullable=False, default=list)
    requires_platform_capability: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    author_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # sha256 of the canonical content, frozen at publish: proves a published
    # definition was not silently edited.
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class AssessmentDefinitionConcept(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "assessment_definition_concepts"
    __table_args__ = (
        UniqueConstraint(
            "definition_id", "concept_id", name="uq_assessment_definition_concepts_definition_concept"
        ),
    )

    definition_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_definitions.id", ondelete="CASCADE"), nullable=False
    )
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=False)
    # The exact version this definition assesses (never "latest").
    concept_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="assessed")
    # Which required criteria decide THIS concept's evidence row.
    criterion_keys: Mapped[list] = mapped_column("criterion_keys_json", nullable=False, default=list)


class AssessmentAttempt(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "assessment_attempts"
    __table_args__ = (
        Index(
            "uq_assessment_attempts_one_active",
            "user_id",
            "definition_id",
            unique=True,
            sqlite_where=text("status IN ('draft', 'submitted', 'checking', 'awaiting_grading', 'grading')"),
            postgresql_where=text(
                "status IN ('draft', 'submitted', 'checking', 'awaiting_grading', 'grading')"
            ),
        ),
        Index(
            "uq_assessment_attempts_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    definition_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    origin: Mapped[AssessmentOrigin] = mapped_column(
        sa_enum(AssessmentOrigin), nullable=False, default=AssessmentOrigin.LEARNER_STARTED
    )
    project_attempt_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("project_attempts.id", ondelete="SET NULL"), nullable=True
    )
    source_submission_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("assessment_ready_submissions.id", ondelete="SET NULL"), nullable=True
    )
    enrollment_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("enrollments.id", ondelete="SET NULL"), nullable=True
    )
    previous_attempt_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("assessment_attempts.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[AssessmentAttemptStatus] = mapped_column(
        sa_enum(AssessmentAttemptStatus), nullable=False, default=AssessmentAttemptStatus.DRAFT
    )
    # Every version this attempt ran against (definition, concepts, template,
    # program, grading contract, rubric). Frozen at start.
    pinned_versions: Mapped[dict] = mapped_column("pinned_versions_json", nullable=False, default=dict)
    fresh_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fresh_reason: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    challenge_seed: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    challenge_instance: Mapped[Optional[dict]] = mapped_column("challenge_instance_json", nullable=True)
    draft: Mapped[Optional[dict]] = mapped_column("draft_json", nullable=True)
    submission: Mapped[Optional[dict]] = mapped_column("submission_json", nullable=True)
    submission_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    input_manifest: Mapped[Optional[dict]] = mapped_column("input_manifest_json", nullable=True)
    input_manifest_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attestation: Mapped[Optional[dict]] = mapped_column("attestation_json", nullable=True)
    grading_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    grader_agent_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_versions.id"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)


class AssessmentResult(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only. Never updated."""

    __tablename__ = "assessment_results"
    __table_args__ = (
        UniqueConstraint("attempt_id", "seq", name="uq_assessment_results_attempt_id_seq"),
        CheckConstraint("result_kind != 'final' OR outcome IS NOT NULL", name="final_has_outcome"),
        CheckConstraint(
            "result_kind != 'human' OR (outcome IS NOT NULL AND supersedes_result_id IS NOT NULL)",
            name="human_supersedes_and_decides",
        ),
        # One deterministic and one grader stage row per (attempt, round).
        Index(
            "uq_assessment_results_stage",
            "attempt_id",
            "result_kind",
            "round",
            unique=True,
            sqlite_where=text("result_kind IN ('deterministic', 'grader')"),
            postgresql_where=text("result_kind IN ('deterministic', 'grader')"),
        ),
        # One original final row per attempt.
        Index(
            "uq_assessment_results_final",
            "attempt_id",
            unique=True,
            sqlite_where=text("result_kind = 'final' AND supersedes_result_id IS NULL"),
            postgresql_where=text("result_kind = 'final' AND supersedes_result_id IS NULL"),
        ),
        # A result is superseded at most once — the chain never forks.
        Index(
            "uq_assessment_results_supersedes",
            "supersedes_result_id",
            unique=True,
            sqlite_where=text("supersedes_result_id IS NOT NULL"),
            postgresql_where=text("supersedes_result_id IS NOT NULL"),
        ),
    )

    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised ownership: every read filters on this directly.
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_kind: Mapped[AssessmentResultKind] = mapped_column(sa_enum(AssessmentResultKind), nullable=False)
    outcome: Mapped[Optional[AssessmentOutcome]] = mapped_column(sa_enum(AssessmentOutcome), nullable=True)
    demonstration_effect: Mapped[Optional[DemonstrationEffect]] = mapped_column(
        sa_enum(DemonstrationEffect), nullable=True
    )
    # Per-criterion findings, each labelled with its method and provenance.
    criteria: Mapped[list] = mapped_column("criteria_json", nullable=False, default=list)
    facts: Mapped[dict] = mapped_column("facts_json", nullable=False, default=dict)
    gaps: Mapped[list] = mapped_column("gaps_json", nullable=False, default=list)
    remediation: Mapped[list] = mapped_column("remediation_json", nullable=False, default=list)
    report: Mapped[Optional[dict]] = mapped_column("report_json", nullable=True)
    grader_agent_run_ids: Mapped[Optional[list]] = mapped_column("grader_agent_run_ids_json", nullable=True)
    grader_agent_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_versions.id"), nullable=True
    )
    grading_contract_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # For ``human`` rows: the review that produced it. Validated in the
    # service layer (a hard FK would make results <-> reviews circular).
    review_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    supersedes_result_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("assessment_results.id", ondelete="RESTRICT"), nullable=True
    )
    record_snapshot: Mapped[Optional[dict]] = mapped_column("record_snapshot_json", nullable=True)
    record_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class AssessmentReview(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "assessment_reviews"
    __table_args__ = (
        Index(
            "uq_assessment_reviews_one_open",
            "attempt_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
    )

    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"), nullable=False
    )
    result_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_results.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger: Mapped[AssessmentReviewTrigger] = mapped_column(sa_enum(AssessmentReviewTrigger), nullable=False)
    status: Mapped[AssessmentReviewStatus] = mapped_column(
        sa_enum(AssessmentReviewStatus), nullable=False, default=AssessmentReviewStatus.OPEN
    )
    reason_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The learner's explicit consent to let a reviewer see THIS attempt. Without
    # it a reviewer sees only non-content metadata.
    consent_shared_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision: Mapped[Optional[AssessmentReviewDecision]] = mapped_column(
        sa_enum(AssessmentReviewDecision), nullable=True
    )
    decision_rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # sha256 over (result id, manifest hash, submission hash): binds the review
    # to the exact record the reviewer saw — the Approval action-fingerprint pattern.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resulting_result_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("assessment_results.id", ondelete="SET NULL"), nullable=True
    )


# -- ORM immutability guards -------------------------------------------------------------

_DEFINITION_FROZEN_FIELDS = (
    "definition_key",
    "version",
    "assessment_kind",
    "title",
    "instructions_md",
    "produces_evidence_type",
    "project_template_id",
    "criteria",
    "challenge_spec",
    "independence_policy",
    "grading_policy",
    "allowed_resources",
    "requires_platform_capability",
    "author_user_id",
    "content_hash",
    "published_at",
)


class ImmutableRecordError(RuntimeError):
    """An attempt to modify a published/append-only assessment record."""


@event.listens_for(AssessmentDefinition, "before_update")
def _guard_definition(mapper, connection, target):
    """DRAFT rows are editable. Once PUBLISHED the content is frozen and the
    only legal change is PUBLISHED -> RETIRED; RETIRED is terminal."""
    state = inspect(target)
    status_history = state.attrs.status.history
    previous = (status_history.deleted or status_history.unchanged or [None])[0]
    if previous not in (AssessmentDefinitionStatus.PUBLISHED, AssessmentDefinitionStatus.RETIRED):
        return
    changed = [f for f in _DEFINITION_FROZEN_FIELDS if state.attrs[f].history.has_changes()]
    if changed:
        raise ImmutableRecordError(
            "A published assessment definition is immutable; publish a new version instead "
            f"(attempted change: {', '.join(changed)})"
        )
    if status_history.has_changes() and not (
        previous == AssessmentDefinitionStatus.PUBLISHED
        and target.status == AssessmentDefinitionStatus.RETIRED
    ):
        raise ImmutableRecordError("A published assessment definition can only be retired")


@event.listens_for(AssessmentDefinitionConcept, "before_update")
def _guard_definition_concept(mapper, connection, target):
    raise ImmutableRecordError("Assessment definition concept links are immutable")


# Everything an attempt pinned, drew, froze or attested is write-once: set at
# start / by the submit compare-and-swap, never edited afterwards through the ORM.
_ATTEMPT_WRITE_ONCE = (
    "user_id",
    "definition_id",
    "origin",
    "project_attempt_id",
    "source_submission_id",
    "enrollment_id",
    "previous_attempt_id",
    "pinned_versions",
    "fresh_required",
    "fresh_reason",
    "challenge_seed",
    "challenge_instance",
    "input_manifest",
    "input_manifest_hash",
    "started_at",
    "idempotency_key",
    "submission",
    "submission_hash",
    "attestation",
)


@event.listens_for(AssessmentAttempt, "before_update")
def _guard_attempt(mapper, connection, target):
    state = inspect(target)
    changed = [f for f in _ATTEMPT_WRITE_ONCE if state.attrs[f].history.has_changes()]
    if changed:
        raise ImmutableRecordError(
            f"An assessment attempt's pinned and frozen fields are write-once ({', '.join(changed)})"
        )
    grader = state.attrs.grader_agent_version_id.history
    if grader.has_changes() and any(v is not None for v in (grader.deleted or [])):
        raise ImmutableRecordError("The Grader Agent Version is pinned at first grade and never changes")
    status = state.attrs.status.history
    was = (status.deleted or status.unchanged or [None])[0]
    if state.attrs.draft.history.has_changes() and was != AssessmentAttemptStatus.DRAFT:
        raise ImmutableRecordError("The draft can only change while the attempt is a draft")


@event.listens_for(AssessmentResult, "before_update")
def _guard_result(mapper, connection, target):
    raise ImmutableRecordError("Assessment results are append-only; append a superseding result instead")
