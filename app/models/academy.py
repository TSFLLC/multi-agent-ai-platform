"""AIL.5A Academy Foundation entities.

Programs schedule existing Concepts and Learning Items.  They deliberately
do not own lesson content, mastery state, evidence, experiments, or project
execution state.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import AcademyEnrollmentStatus, AcademyPace, AcademyProgramItemKind, AcademyProgramVersionStatus
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
