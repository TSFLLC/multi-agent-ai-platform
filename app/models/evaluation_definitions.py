"""Evaluation Definition Registry — MA6 Slice 1.

Structurally mirrors the Agent/AgentVersion pattern (app.models.agents) —
same immutability discipline, same ``VersionStatus`` lifecycle, no shared
base class forced between them (Slice 1 is deliberately bounded; see the
service module's docstring for why no lifecycle helper was extracted).

``EvaluationDefinition`` is a stable, project-scoped identity (an Owner
decision frozen at the MA6 architecture checkpoint — evaluation rubrics
belong to a project, not the whole platform, in V1). Editing a rubric's
criteria means publishing a new ``EvaluationDefinitionVersion``, never
mutating a published one in place — the exact same reproducibility
guarantee ``agent_versions`` already provides, applied to evaluation
criteria instead of prompts/model policy.

This module intentionally stops at "what does this rubric measure and how
is it versioned." It does NOT model an evaluation *run* or *result* — the
existing (unused) MA0 ``evaluations`` table is deferred to a later slice
per the approved architecture checkpoint, and no scoring/ranking/winner
concept exists anywhere in this file (Section 2 invariant: MA6 must never
automatically select a comparison winner — nothing here even has the
columns to attempt it).
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import VersionStatus
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class EvaluationDefinition(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Stable identity for one evaluation rubric family (e.g. "Software
    Engineer correctness rubric") — project-scoped, per the MA6 checkpoint
    decision. Structurally identical in spirit to ``app.models.agents.Agent``:
    a persistent name/identity that never itself carries criteria — those
    live on versioned child rows only.
    """

    __tablename__ = "evaluation_definitions"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Denormalized convenience mirror of the latest-transitioned version's
    # status — same pattern and same caveat as Agent.current_status: never
    # authoritative, kept in sync only where a version's status changes
    # (EvaluationDefinitionService), never enforced as a DB constraint.
    current_status: Mapped[Optional[VersionStatus]] = mapped_column(sa_enum(VersionStatus), nullable=True)

    versions: Mapped[List["EvaluationDefinitionVersion"]] = relationship(
        back_populates="evaluation_definition", passive_deletes=True
    )


class EvaluationDefinitionVersion(UUIDPrimaryKeyMixin, Base):
    """Immutable once published — same discipline as ``AgentVersion``
    (Section 12.3's guarantee, applied here): publishing/deprecating/
    retiring only ever flips ``status``/``published_at``; "editing" a
    rubric's criteria means creating a new version row with ``version+1``.
    """

    __tablename__ = "evaluation_definition_versions"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_definition_id", "version", name="uq_evaluation_definition_versions_definition_id_version"
        ),
    )

    evaluation_definition_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_definitions.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Optional per-version release note (e.g. "v2: added maintainability
    # criterion") -- distinct from the parent's own name/description,
    # which identifies the rubric family, not any one version of it.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[VersionStatus] = mapped_column(sa_enum(VersionStatus), nullable=False, default=VersionStatus.DRAFT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    evaluation_definition: Mapped["EvaluationDefinition"] = relationship(back_populates="versions")
    criteria: Mapped[List["EvaluationCriterion"]] = relationship(
        back_populates="evaluation_definition_version",
        passive_deletes=True,
        order_by="EvaluationCriterion.order_index",
    )


class EvaluationCriterion(UUIDPrimaryKeyMixin, Base):
    """One ordered criterion within a versioned rubric — Slice 1 only
    defines *what* is measured, never a result/finding (that is
    ``EvaluationCriterionResult``, explicitly deferred to a later slice).

    ``method_hint`` is a free-text, unenforced hint only (e.g.
    "deterministic" / "agent_judge" / "human") — Slice 1 never executes
    anything, so nothing here validates or dispatches on this value; it
    exists purely so a criterion authored today doesn't need a schema
    change once execution (a later slice) reads it.
    """

    __tablename__ = "evaluation_criteria"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_definition_version_id", "key", name="uq_evaluation_criteria_version_id_key"
        ),
    )

    evaluation_definition_version_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_definition_versions.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    method_hint: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    evaluation_definition_version: Mapped["EvaluationDefinitionVersion"] = relationship(back_populates="criteria")
