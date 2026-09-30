"""AIL.5A Academy Foundation entities.

Programs schedule existing Concepts and Learning Items.  They deliberately
do not own lesson content, mastery state, evidence, experiments, or project
execution state.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.academy_professor import HelpLevel, HelpRequest, ProfessorMode
from app.db.base import Base
from app.db.enums import (
    AcademyEnrollmentStatus,
    AcademyPace,
    AcademyProgramItemKind,
    AcademyProgramVersionStatus,
    AcademyStepResponseKind,
    AcademyStepStatus,
    AssistanceLevel,
    ExecutionVerification,
    MilestoneAttemptMode,
    MilestoneAttemptStatus,
    ProjectAudienceLevel,
    ProjectAttemptStatus,
    ProjectLadderLevel,
    ProjectTemplateBuildMode,
)
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class AcademyProgram(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "programs"
    __table_args__ = (UniqueConstraint("slug", name="uq_programs_slug"),)

    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    author_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    target_track_term_id: Mapped[Optional[str]] = mapped_column(ForeignKey("taxonomy_terms.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")

    versions: Mapped[List["AcademyProgramVersion"]] = relationship(
        back_populates="program", passive_deletes=True, order_by="AcademyProgramVersion.version"
    )


class AcademyProgramVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "program_versions"
    __table_args__ = (UniqueConstraint("program_id", "version", name="uq_program_versions_program_id_version"),)

    program_id: Mapped[str] = mapped_column(ForeignKey("programs.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AcademyProgramVersionStatus] = mapped_column(sa_enum(AcademyProgramVersionStatus), nullable=False, default=AcademyProgramVersionStatus.DRAFT)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_rules: Mapped[dict] = mapped_column("completion_rules_json", nullable=False, default=dict)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    program: Mapped["AcademyProgram"] = relationship(back_populates="versions")
    items: Mapped[List["AcademyProgramItem"]] = relationship(
        back_populates="program_version", passive_deletes=True, order_by="(AcademyProgramItem.day, AcademyProgramItem.position)"
    )


class AcademyProgramItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "program_items"
    __table_args__ = (
        UniqueConstraint("program_version_id", "day", "position", name="uq_program_items_version_day_position"),
        CheckConstraint("concept_id IS NOT NULL OR learning_item_id IS NOT NULL", name="ck_program_items_has_target"),
    )

    program_version_id: Mapped[str] = mapped_column(ForeignKey("program_versions.id", ondelete="CASCADE"), nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    day: Mapped[int] = mapped_column(Integer, nullable=False)
    module_key: Mapped[str] = mapped_column(String(120), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    item_kind: Mapped[AcademyProgramItemKind] = mapped_column(sa_enum(AcademyProgramItemKind), nullable=False)
    concept_id: Mapped[Optional[str]] = mapped_column(ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=True)
    learning_item_id: Mapped[Optional[str]] = mapped_column(ForeignKey("learning_items.id", ondelete="RESTRICT"), nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    estimated_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    purpose_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completion_requirement: Mapped[Optional[dict]] = mapped_column("completion_requirement_json", nullable=True)

    program_version: Mapped["AcademyProgramVersion"] = relationship(back_populates="items")


class AcademyEnrollment(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("user_id", "program_version_id", name="uq_enrollments_user_id_program_version_id"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    program_version_id: Mapped[str] = mapped_column(ForeignKey("program_versions.id", ondelete="RESTRICT"), nullable=False)
    pace: Mapped[AcademyPace] = mapped_column(sa_enum(AcademyPace), nullable=False, default=AcademyPace.SCHEDULED)
    status: Mapped[AcademyEnrollmentStatus] = mapped_column(sa_enum(AcademyEnrollmentStatus), nullable=False, default=AcademyEnrollmentStatus.ACTIVE)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    progress_shared_with_author: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    program_version: Mapped["AcademyProgramVersion"] = relationship()
    user_projects: Mapped[List["ProjectAttempt"]] = relationship(back_populates="enrollment", passive_deletes=True)


class ProjectTemplate(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Reusable project definitions. Once published, immutable."""

    __tablename__ = "project_templates"
    __table_args__ = (UniqueConstraint("template_key", "version", name="uq_project_templates_key_version"),)

    template_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")  # draft/published/retired
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    audience_level: Mapped[ProjectAudienceLevel] = mapped_column(sa_enum(ProjectAudienceLevel), nullable=False)
    ladder_level: Mapped[ProjectLadderLevel] = mapped_column(sa_enum(ProjectLadderLevel), nullable=False)
    build_mode: Mapped[ProjectTemplateBuildMode] = mapped_column(sa_enum(ProjectTemplateBuildMode), nullable=False)
    brief_md: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty_profile: Mapped[dict] = mapped_column("difficulty_profile_json", nullable=False, default=dict)
    evidence_requirements: Mapped[dict] = mapped_column("evidence_requirements_json", nullable=False, default=dict)
    variant_spec: Mapped[Optional[dict]] = mapped_column("variant_spec_json", nullable=True)
    requires_platform_capability: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    est_minutes_min: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    est_minutes_max: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    capstone_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    author_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    concept_links: Mapped[List["ProjectTemplateConceptLink"]] = relationship(
        back_populates="template", passive_deletes=True, lazy="selectin"
    )
    milestones: Mapped[List["ProjectMilestone"]] = relationship(
        back_populates="template", passive_deletes=True, lazy="selectin", order_by="ProjectMilestone.position"
    )
    attempts: Mapped[List["ProjectAttempt"]] = relationship(back_populates="template", passive_deletes=True)


class ProjectTemplateConceptLink(UUIDPrimaryKeyMixin, Base):
    """Connects concepts to projects with role."""

    __tablename__ = "project_template_concepts"
    __table_args__ = (UniqueConstraint("project_template_id", "concept_id", name="uq_project_template_concepts"),)

    project_template_id: Mapped[str] = mapped_column(ForeignKey("project_templates.id", ondelete="CASCADE"), nullable=False)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # taught/applied/assessed

    template: Mapped["ProjectTemplate"] = relationship(back_populates="concept_links")
    concept: Mapped["Concept"] = relationship()


class ProjectMilestone(UUIDPrimaryKeyMixin, Base):
    """Ordered steps in a project."""

    __tablename__ = "project_milestones"
    __table_args__ = (UniqueConstraint("project_template_id", "position", name="uq_project_milestones_template_position"),)

    project_template_id: Mapped[str] = mapped_column(ForeignKey("project_templates.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    instructions_md: Mapped[str] = mapped_column(Text, nullable=False)
    check_spec: Mapped[dict] = mapped_column("check_spec_json", nullable=False, default=dict)
    learning_item_id: Mapped[Optional[str]] = mapped_column(ForeignKey("learning_items.id", ondelete="RESTRICT"), nullable=True)
    expected_artifact_kind: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    evidence_type: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    reference_solution_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    hint_content: Mapped[Optional[dict]] = mapped_column("hint_content_json", nullable=True)

    template: Mapped["ProjectTemplate"] = relationship(back_populates="milestones")
    attempts: Mapped[List["MilestoneAttempt"]] = relationship(back_populates="milestone", passive_deletes=True)


class ProjectAttempt(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Learner starting a project."""

    __tablename__ = "project_attempts"
    __table_args__ = (
        UniqueConstraint("user_id", "project_template_id", "enrollment_id", "is_capstone", name="uq_project_attempts"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_template_id: Mapped[str] = mapped_column(ForeignKey("project_templates.id", ondelete="RESTRICT"), nullable=False)
    enrollment_id: Mapped[Optional[str]] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=True)
    is_capstone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[ProjectAttemptStatus] = mapped_column(sa_enum(ProjectAttemptStatus), nullable=False, default=ProjectAttemptStatus.ACTIVE)
    brief_snapshot: Mapped[dict] = mapped_column("brief_snapshot_json", nullable=False, default=dict)
    learner_agent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    app_eval_set_id: Mapped[Optional[str]] = mapped_column(ForeignKey("eval_sets.id", ondelete="SET NULL"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    template: Mapped["ProjectTemplate"] = relationship(back_populates="attempts")
    enrollment: Mapped[Optional["AcademyEnrollment"]] = relationship(back_populates="user_projects")
    milestones: Mapped[List["MilestoneAttempt"]] = relationship(back_populates="project_attempt", passive_deletes=True, order_by="MilestoneAttempt.created_at")


class MilestoneAttempt(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Work on a single milestone."""

    __tablename__ = "milestone_attempts"
    # Retries and Study Mode variants are intentionally append-only.  A
    # project can therefore have several attempts for one milestone.
    __table_args__ = (Index("ix_milestone_attempts_project_milestone", "project_attempt_id", "project_milestone_id"),)

    project_attempt_id: Mapped[str] = mapped_column(ForeignKey("project_attempts.id", ondelete="CASCADE"), nullable=False)
    project_milestone_id: Mapped[str] = mapped_column(ForeignKey("project_milestones.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[MilestoneAttemptStatus] = mapped_column(sa_enum(MilestoneAttemptStatus), nullable=False, default=MilestoneAttemptStatus.NOT_STARTED)
    attempts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_assistance_level: Mapped[Optional[AssistanceLevel]] = mapped_column(sa_enum(AssistanceLevel), nullable=True)
    mode: Mapped[MilestoneAttemptMode] = mapped_column(sa_enum(MilestoneAttemptMode), nullable=False, default=MilestoneAttemptMode.NORMAL)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    project_attempt: Mapped["ProjectAttempt"] = relationship(back_populates="milestones")
    milestone: Mapped["ProjectMilestone"] = relationship(back_populates="attempts")


class ProjectExperimentLink(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A bounded link to an existing Personal Lab Experiment."""
    __tablename__ = "project_experiment_links"
    __table_args__ = (UniqueConstraint("milestone_attempt_id", "experiment_id", name="uq_project_experiment_link"),)

    project_attempt_id: Mapped[str] = mapped_column(ForeignKey("project_attempts.id", ondelete="CASCADE"), nullable=False)
    milestone_attempt_id: Mapped[Optional[str]] = mapped_column(ForeignKey("milestone_attempts.id", ondelete="CASCADE"), nullable=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False)
    project_question: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    learner_decision: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ExplainBackResponse(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "explain_back_responses"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_attempt_id: Mapped[str] = mapped_column(ForeignKey("project_attempts.id", ondelete="CASCADE"), nullable=False)
    milestone_attempt_id: Mapped[str] = mapped_column(ForeignKey("milestone_attempts.id", ondelete="CASCADE"), nullable=False)
    concept_id: Mapped[Optional[str]] = mapped_column(ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    assistance_level: Mapped[Optional[AssistanceLevel]] = mapped_column(sa_enum(AssistanceLevel), nullable=True)


class CandidateEvidence(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only project evidence before the canonical learning service."""
    __tablename__ = "candidate_evidence"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_attempt_id: Mapped[str] = mapped_column(ForeignKey("project_attempts.id", ondelete="CASCADE"), nullable=False)
    milestone_attempt_id: Mapped[Optional[str]] = mapped_column(ForeignKey("milestone_attempts.id", ondelete="CASCADE"), nullable=True)
    concept_id: Mapped[Optional[str]] = mapped_column(ForeignKey("concepts.id", ondelete="RESTRICT"), nullable=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assistance_level: Mapped[Optional[AssistanceLevel]] = mapped_column(sa_enum(AssistanceLevel), nullable=True)
    execution_verification: Mapped[Optional[ExecutionVerification]] = mapped_column(sa_enum(ExecutionVerification), nullable=True)
    qualified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    learning_evidence_id: Mapped[Optional[str]] = mapped_column(ForeignKey("learning_evidence.id", ondelete="SET NULL"), nullable=True)


class AssessmentReadySubmission(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "assessment_ready_submissions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_attempt_id: Mapped[str] = mapped_column(ForeignKey("project_attempts.id", ondelete="CASCADE"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ready")
    snapshot: Mapped[dict] = mapped_column("snapshot_json", nullable=False, default=dict)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AcademyStepProgress(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """AIL.5D.1 — one learner's progress on one authored step of one immutable Learning Item version.

    Authored content is never stored or edited here: the step lives in ``learning_items.spec_json.steps`` and
    is identified by ``step_key``. This row is mutable learner state only. It writes no evidence and derives no
    mastery: "learning complete" is a presentation state; demonstration stays with AIL.5C / LearningEvidence.

    * ``learning_item_id`` is the exact version the learner acted on; ``lineage_id`` (set by the service from
      that item, never by a client) lets progress be looked up across versions.
    * ``step_fingerprint`` is the digest of the step's learner-visible definition at write time. A later version
      of the Day only inherits this row when the step's fingerprint is unchanged.
    * ``opened`` never means completed. Completion carries a ``completion_basis`` recording how it was earned.
    """

    __tablename__ = "academy_step_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "learning_item_id", "step_key", name="uq_academy_step_progress_user_item_step"),
        CheckConstraint("open_count >= 1", name="open_count"),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL)",
            name="completed_at",
        ),
        CheckConstraint(
            "(status = 'skipped' AND skipped_at IS NOT NULL) OR (status <> 'skipped' AND skipped_at IS NULL)",
            name="skipped_at",
        ),
        Index("ix_academy_step_progress_user_lineage", "user_id", "lineage_id"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    learning_item_id: Mapped[str] = mapped_column(ForeignKey("learning_items.id", ondelete="RESTRICT"), nullable=False)
    lineage_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    step_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[AcademyStepStatus] = mapped_column(sa_enum(AcademyStepStatus), nullable=False, default=AcademyStepStatus.OPENED)
    open_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    skipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_basis: Mapped[Optional[dict]] = mapped_column("completion_basis_json", nullable=True)


class AcademyStepResponse(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """AIL.5D.3 - what one learner wrote or chose on one authored step of one immutable Learning Item version.

    Append-only learner data. The authored step lives in ``learning_items.spec_json.steps``; this row only records the
    learner's own response to it. Refining a response appends a new ``revision`` (the latest wins); nothing is updated
    or deleted, so ordering is provable from ``created_at`` (for example, that an answer was committed before a reveal).

    * Strictly owned: ``user_id`` (set by the service from the authenticated learner, never by a client).
    * Bound to the exact version: ``learning_item_id`` plus a server-set ``lineage_id``, and ``step_fingerprint`` (the
      step definition at write time) so a newer version can tell an unchanged step's response from a revised one.
    * ``response_key`` names a part of the response (a statement id, an outline point) and is ``""`` for a single value.
    * ``content`` is bounded JSON (text, a structured answer, ...). It is private to the learner and is never grading:
      it writes no evidence and is never shown to the Professor or the Grader unless a later, explicit slice says so.
    * ``ref_type`` / ``ref_id`` optionally point at the canonical record the response belongs to (a knowledge-check
      evidence row now; an experiment or run for Lab / Practice later). ``assistance`` is reserved for help metadata.
    """

    __tablename__ = "academy_step_responses"
    __table_args__ = (
        UniqueConstraint("user_id", "learning_item_id", "step_key", "kind", "response_key", "revision", name="uq_academy_step_responses_revision"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index("ix_academy_step_responses_user_lineage_step", "user_id", "lineage_id", "step_key"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    learning_item_id: Mapped[str] = mapped_column(ForeignKey("learning_items.id", ondelete="RESTRICT"), nullable=False)
    lineage_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    step_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[AcademyStepResponseKind] = mapped_column(sa_enum(AcademyStepResponseKind), nullable=False)
    response_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[dict] = mapped_column("content_json", nullable=False)
    ref_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    ref_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    assistance: Mapped[Optional[dict]] = mapped_column("assistance_json", nullable=True)


class AcademyProfessorHelp(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """AIL.5D.4 - one delivered step-scoped AI Professor help, recorded for later independence decisions.

    A row exists only for help the learner actually received (a completed, validated interaction). It is an audit of
    assistance, never evidence: writing it never changes what a learner has demonstrated. ``assistance_level`` is on the
    platform's existing H0-H5 scale (None when a teaching-mode question is not assistance on an exercise);
    ``revealed_solution`` is true when authored solution material was in the context (recorded as H5).
    ``context_sha256`` fingerprints exactly what the Professor was given. A future practice instance reads the highest
    level recorded for its step. Owned by ``user_id`` (server-set) and bound to the exact item version and step.
    """

    __tablename__ = "academy_professor_help"
    __table_args__ = (
        UniqueConstraint("interaction_id", name="uq_academy_professor_help_interaction"),
        Index("ix_academy_professor_help_user_lineage_step", "user_id", "lineage_id", "step_key"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    learning_item_id: Mapped[str] = mapped_column(ForeignKey("learning_items.id", ondelete="RESTRICT"), nullable=False)
    lineage_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    step_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[ProfessorMode] = mapped_column(sa_enum(ProfessorMode), nullable=False)
    request_kind: Mapped[HelpRequest] = mapped_column(sa_enum(HelpRequest), nullable=False)
    help_level: Mapped[HelpLevel] = mapped_column(sa_enum(HelpLevel), nullable=False)
    assistance_level: Mapped[Optional[AssistanceLevel]] = mapped_column(sa_enum(AssistanceLevel), nullable=True)
    revealed_solution: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interaction_id: Mapped[str] = mapped_column(String(36), nullable=False)
    context_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
