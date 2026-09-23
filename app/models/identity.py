"""Identity / tenancy — Section 24.2, 24.4 #6.

V1 is local/single-user by Owner decision, but organization/project/
membership boundaries are preserved in the data model so multi-user/hosted
expansion never requires a retrofit (Owner instruction, spec Section 35a
item 1 / Appendix D item 7).
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import OrgRole, ProjectKind, ProjectRole
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.types import sa_enum


class Organization(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    settings: Mapped[Optional[dict]] = mapped_column("settings_json", nullable=True)

    users: Mapped[List["User"]] = relationship(back_populates="organization")
    projects: Mapped[List["Project"]] = relationship(back_populates="organization")


class User(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_users_org_id_email"),)

    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[OrgRole] = mapped_column(sa_enum(OrgRole), nullable=False, default=OrgRole.MEMBER)

    organization: Mapped["Organization"] = relationship(back_populates="users")
    memberships: Mapped[List["ProjectMembership"]] = relationship(
        back_populates="user", foreign_keys="ProjectMembership.user_id"
    )


class Project(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "projects"

    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    policy_settings: Mapped[Optional[dict]] = mapped_column("policy_settings_json", nullable=True)
    kind: Mapped[ProjectKind] = mapped_column(sa_enum(ProjectKind), nullable=False, default=ProjectKind.STANDARD, deferred=True)
    ail_evidence_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0", deferred=True)

    organization: Mapped["Organization"] = relationship(back_populates="projects")
    memberships: Mapped[List["ProjectMembership"]] = relationship(back_populates="project")


class ProjectMembership(UUIDPrimaryKeyMixin, Base):
    """Section 24.4 #6 — required even for a single-user V1 deployment."""

    __tablename__ = "project_memberships"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_memberships_project_id_user_id"),
    )

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[ProjectRole] = mapped_column(sa_enum(ProjectRole), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    granted_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="memberships")
    user: Mapped["User"] = relationship(back_populates="memberships", foreign_keys=[user_id])
