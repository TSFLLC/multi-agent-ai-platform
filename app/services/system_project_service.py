"""Centralized lookup for the user's organization-scoped AIL system project."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import ProjectAction, check_project_access
from app.db.enums import ProjectKind, ProjectRole
from app.errors import ForbiddenError
from app.models.identity import Project, ProjectMembership, User

AIL_SYSTEM_PROJECT_NAME = "AIL Personal Lab"


def ensure_ail_system_project(db: Session, user: User) -> Project:
    """Return the one SYSTEM_AIL project for the user's organization.

    The project is the existing Personal Lab system project. A membership is
    ensured for the current user so normal project authorization remains the
    boundary for Professor execution and artifact reads.
    """
    project = db.execute(
        select(Project).where(
            Project.org_id == user.org_id,
            Project.kind == ProjectKind.SYSTEM_AIL,
            Project.name == AIL_SYSTEM_PROJECT_NAME,
        )
    ).scalar_one_or_none()
    if project is None:
        project = Project(
            org_id=user.org_id,
            name=AIL_SYSTEM_PROJECT_NAME,
            kind=ProjectKind.SYSTEM_AIL,
            ail_evidence_opt_in=False,
        )
        db.add(project)
        db.flush()

    membership = db.execute(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project.id,
            ProjectMembership.user_id == user.id,
        )
    ).scalar_one_or_none()
    if membership is None:
        db.add(
            ProjectMembership(
                project_id=project.id,
                user_id=user.id,
                role=ProjectRole.OWNER,
                granted_by=user.id,
            )
        )
        db.flush()
    else:
        check_project_access(db, user=user, project_id=project.id, action=ProjectAction.READ)
    return project


def require_ail_system_access(db: Session, user: User, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.kind != ProjectKind.SYSTEM_AIL:
        raise ForbiddenError("Professor interaction is not an AIL system record.")
    check_project_access(db, user=user, project_id=project.id, action=ProjectAction.READ)
    return project
