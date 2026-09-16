"""Agent Registry — Section 12, 24.2, 24.4 #3/#5.

Agent ≠ Model is enforced structurally: no column on ``agents`` or
``agent_versions`` binds to a specific ``models``/``providers`` row. The
binding only ever happens per Agent Run, resolved fresh by the Router
(ADR-1). ``model_policy`` here expresses *constraints/preference* only.

Published ``agent_versions`` rows are immutable: nothing in this module (or
any service built on top of it) may update a published version's content
columns in place — "editing" means inserting a new version row with
``version + 1``. This is a code-level discipline the immutability test
(``tests/test_agent_version_immutability.py``) checks.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import (
    DefaultModelStrategy,
    PolicyRuleScope,
    PolicyRuleType,
    ToolGrantType,
    ToolStatus,
    VersionStatus,
)
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class Agent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Stable identity — Section 12.1."""

    __tablename__ = "agents"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Denormalized convenience mirror of the latest published version's
    # status (Section 24.2). Never authoritative — agent_versions.status is
    # — and must always agree with it; enforced as an application
    # invariant (kept consistent with the comparison_runs.winner_agent_run_id
    # pattern used elsewhere), not a DB constraint SQLite can express across
    # tables.
    current_status: Mapped[Optional[VersionStatus]] = mapped_column(sa_enum(VersionStatus), nullable=True)

    # passive_deletes=True defers to the FK's ON DELETE CASCADE at the DB
    # level; without it, the ORM's default behavior tries to UPDATE
    # dependent rows' agent_id to NULL before deleting the parent, which
    # fails against the NOT NULL agent_id column.
    versions: Mapped[List["AgentVersion"]] = relationship(back_populates="agent", passive_deletes=True)
    prompt_versions: Mapped[List["PromptVersion"]] = relationship(
        back_populates="agent", passive_deletes=True
    )


class PromptVersion(UUIDPrimaryKeyMixin, Base):
    """Section 12.4, 24.4 #3 — versioned independently of Agent Version."""

    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_prompt_versions_agent_id_version"),)

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)

    agent: Mapped["Agent"] = relationship(back_populates="prompt_versions")


class AgentVersion(UUIDPrimaryKeyMixin, Base):
    """Immutable once published — Section 12.2, 12.3."""

    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_id_version"),)

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    prompt_version_id: Mapped[Optional[str]] = mapped_column(ForeignKey("prompt_versions.id"), nullable=True)

    capabilities: Mapped[Optional[list]] = mapped_column("capabilities_json", nullable=True)
    model_policy: Mapped[Optional[dict]] = mapped_column("model_policy_json", nullable=True)
    default_model_strategy: Mapped[DefaultModelStrategy] = mapped_column(
        sa_enum(DefaultModelStrategy),
        nullable=False,
        default=DefaultModelStrategy.MANUAL_REQUIRED,
    )
    context_policy: Mapped[Optional[dict]] = mapped_column("context_policy_json", nullable=True)
    # V1: no cross-task Agent memory (Owner decision) — field kept nullable
    # for forward compatibility, never populated/consumed by any MA0-or-later
    # V1 code path.
    memory_policy: Mapped[Optional[dict]] = mapped_column("memory_policy_json", nullable=True)
    budget_policy: Mapped[Optional[dict]] = mapped_column("budget_policy_json", nullable=True)
    timeout_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retry_policy: Mapped[Optional[dict]] = mapped_column("retry_policy_json", nullable=True)
    approval_requirements: Mapped[Optional[dict]] = mapped_column("approval_requirements_json", nullable=True)
    status: Mapped[VersionStatus] = mapped_column(
        sa_enum(VersionStatus),
        nullable=False,
        default=VersionStatus.DRAFT,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    agent: Mapped["Agent"] = relationship(back_populates="versions")
    tool_grants: Mapped[List["AgentVersionToolGrant"]] = relationship(back_populates="agent_version")


class Tool(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Stable tool identity — Section 24.4 #5. ``id`` is a stable slug."""

    __tablename__ = "tools"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_schema_ref: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    tool_version: Mapped[str] = mapped_column(String(40), nullable=False, default="1")
    status: Mapped[ToolStatus] = mapped_column(sa_enum(ToolStatus), nullable=False, default=ToolStatus.ACTIVE)

    grants: Mapped[List["AgentVersionToolGrant"]] = relationship(back_populates="tool")


class AgentVersionToolGrant(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Replaces allowed_tools/prohibited_tools arrays — Section 24.4 #5."""

    __tablename__ = "agent_version_tool_grants"
    __table_args__ = (
        UniqueConstraint(
            "agent_version_id", "tool_id", "grant_type", name="uq_agent_version_tool_grants_version_tool_type"
        ),
    )

    agent_version_id: Mapped[str] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[str] = mapped_column(ForeignKey("tools.id", ondelete="CASCADE"), nullable=False)
    grant_type: Mapped[ToolGrantType] = mapped_column(sa_enum(ToolGrantType), nullable=False)

    agent_version: Mapped["AgentVersion"] = relationship(back_populates="tool_grants")
    tool: Mapped["Tool"] = relationship(back_populates="grants")


class PolicyRule(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Generic policy rule contract (Section 6 tool system requirement).

    Seeds Section 23.2's default approval-required operation list and
    org/project-wide tool allow/deny policy without hard-coding either into
    application logic. Not evaluated by any engine in MA0.
    """

    __tablename__ = "policy_rules"

    scope: Mapped[PolicyRuleScope] = mapped_column(sa_enum(PolicyRuleScope), nullable=False)
    rule_type: Mapped[PolicyRuleType] = mapped_column(sa_enum(PolicyRuleType), nullable=False)
    target_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    config: Mapped[Optional[dict]] = mapped_column("config_json", nullable=True)
