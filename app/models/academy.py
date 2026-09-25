"""AIL.5A Academy Foundation entities.

Programs schedule existing Concepts and Learning Items.  They deliberately
do not own lesson content, mastery state, evidence, experiments, or project
execution state.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import (
    AcademyEnrollmentStatus,
    AcademyPace,
    AcademyProgramItemKind,
    AcademyProgramVersionStatus,
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
    __table_args__ = (UniqueConstraint("project_attempt_id", "project_milestone_id", name="uq_milestone_attempts"),)

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
