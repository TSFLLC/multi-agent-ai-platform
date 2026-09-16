"""Identity/tenancy resource boundary — Section 25.2, 25.5, MA1B.

GET endpoints and project creation are real (MA1B); org creation and
member add/remove stay MA0-style 501 stubs — out of MA1B's bounded scope.
Every real endpoint here requires the local auth token
(app.auth.get_current_user); project-scoped ones additionally go through
app.authz.require_project_access, never an inline role check.
"""

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.auth import get_current_user
from app.authz import ProjectAction, get_project_membership, require_project_access
from app.db.enums import ProjectRole
from app.errors import ConflictError, NotFoundError
from app.models.identity import Organization, Project, ProjectMembership, User
from app.schemas.identity import (
    OrganizationCreate,
    OrganizationRead,
    ProjectCreate,
    ProjectMembershipCreate,
    ProjectMembershipRead,
    ProjectRead,
    UserRead,
)

router = APIRouter(tags=["projects"])


@router.get("/me", response_model=UserRead)
def get_me(user: User = Depends(get_current_user)):
    return user


@router.get("/organizations", response_model=List[OrganizationRead])
def list_organizations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """V1 is single-org: returns exactly the caller's own organization,
    never a global list — there is no cross-org visibility to have."""
    org = db.get(Organization, user.org_id)
    return [org] if org is not None else []


@router.post("/organizations", response_model=OrganizationRead, status_code=201)
def create_organization(body: OrganizationCreate, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/projects", response_model=List[ProjectRead])
def list_projects(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Only projects the caller has a project_memberships row for —
    membership is the sole source of visibility, same as write access."""
    stmt = (
        select(Project)
        .join(ProjectMembership, ProjectMembership.project_id == Project.id)
        .where(ProjectMembership.user_id == user.id)
    )
    return list(db.execute(stmt).scalars().all())


@router.post("/projects", response_model=ProjectRead, status_code=201)
def create_project(
    body: ProjectCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """The creator becomes the new project's Owner via a fresh
    project_memberships row — a project is never created ownerless."""
    if body.org_id != user.org_id:
        raise ConflictError("Cannot create a project outside your own organization.")

    project = Project(org_id=body.org_id, name=body.name)
    db.add(project)
    db.flush()
    membership = ProjectMembership(
        project_id=project.id, user_id=user.id, role=ProjectRole.OWNER, granted_by=user.id
    )
    db.add(membership)
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    _membership: ProjectMembership = Depends(require_project_access(ProjectAction.READ)),
):
    # db is the same request-scoped session require_project_access already
    # used (FastAPI caches Depends(get_db) per request) — an explicit
    # re-query here is more robust than relying on lazy-loading a
    # relationship off an object that dependency may release first.
    return db.get(Project, project_id)


@router.get("/projects/{project_id}/members", response_model=List[ProjectMembershipRead])
def list_project_members(
    project_id: str,
    db: Session = Depends(get_db),
    _membership: ProjectMembership = Depends(require_project_access(ProjectAction.READ)),
):
    stmt = select(ProjectMembership).where(ProjectMembership.project_id == project_id)
    return list(db.execute(stmt).scalars().all())


@router.post("/projects/{project_id}/members", response_model=ProjectMembershipRead, status_code=201)
def add_project_member(
    project_id: str,
    body: ProjectMembershipCreate,
    db: Session = Depends(get_db),
    actor: ProjectMembership = Depends(require_project_access(ProjectAction.ADMIN)),
):
    existing = get_project_membership(db, project_id=project_id, user_id=body.user_id)
    if existing is not None:
        raise ConflictError(f"User {body.user_id} is already a member of project {project_id}.")

    membership = ProjectMembership(
        project_id=project_id, user_id=body.user_id, role=body.role, granted_by=actor.user_id
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return membership


@router.delete("/projects/{project_id}/members/{membership_id}", status_code=204)
def remove_project_member(
    project_id: str,
    membership_id: str,
    db: Session = Depends(get_db),
    _actor: ProjectMembership = Depends(require_project_access(ProjectAction.ADMIN)),
):
    membership = db.get(ProjectMembership, membership_id)
    if membership is None or membership.project_id != project_id:
        raise NotFoundError(f"Membership {membership_id} not found on project {project_id}.")
    db.delete(membership)
    db.commit()
