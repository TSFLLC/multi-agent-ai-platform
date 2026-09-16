"""Local bootstrap identity — MA1B.

V1 is local-first + single-user (Owner decision) — this is not a signup
flow. On every app startup we ensure exactly one Organization, one Owner
User, one default Project, and one Owner ProjectMembership exist, using
the frozen MA0 identity contracts (app.models.identity) unchanged.

Idempotent by construction: the four identities use fixed, well-known
primary keys, so a restart resolves the existing rows (checked by primary
key / unique constraint) instead of minting new ones. A concurrent
double-bootstrap (which should not happen in practice — this only runs
from the single API process's startup lifespan) is still handled safely:
a duplicate-insert race is caught and treated as "someone else already
bootstrapped it."
"""

from typing import NamedTuple, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.enums import OrgRole, ProjectRole
from app.models.identity import Organization, Project, ProjectMembership, User

# Fixed, well-known IDs — not randomly generated — so "does the bootstrap
# identity already exist" is a primary-key lookup, not a fuzzy name match.
LOCAL_ORG_ID = "00000000-0000-0000-0000-000000000001"
LOCAL_OWNER_USER_ID = "00000000-0000-0000-0000-000000000002"
LOCAL_PROJECT_ID = "00000000-0000-0000-0000-000000000003"

LOCAL_ORG_NAME = "Local Organization"
LOCAL_OWNER_EMAIL = "owner@local"
LOCAL_PROJECT_NAME = "Default Project"


class BootstrapIdentities(NamedTuple):
    organization: Organization
    user: User
    project: Project
    membership: ProjectMembership


def ensure_local_bootstrap(db: Session) -> BootstrapIdentities:
    try:
        org = _ensure_organization(db)
        user = _ensure_owner_user(db, org)
        project = _ensure_project(db, org)
        membership = _ensure_owner_membership(db, project, user)
        db.commit()
    except IntegrityError:
        # Another process/call won a creation race — re-fetch rather than
        # fail; the rows are guaranteed to exist now either way.
        db.rollback()
        refetched_org = db.get(Organization, LOCAL_ORG_ID)
        refetched_user = db.get(User, LOCAL_OWNER_USER_ID)
        refetched_project = db.get(Project, LOCAL_PROJECT_ID)
        if refetched_org is None or refetched_user is None or refetched_project is None:
            raise RuntimeError(
                "Bootstrap race: a create failed with IntegrityError but the "
                "corresponding well-known row still does not exist."
            ) from None
        org, user, project = refetched_org, refetched_user, refetched_project
        # _ensure_owner_membership is itself check-then-create, so this is
        # correct whether the race left the membership row missing or not.
        membership = _ensure_owner_membership(db, project, user)
        db.commit()

    return BootstrapIdentities(organization=org, user=user, project=project, membership=membership)


def _ensure_organization(db: Session) -> Organization:
    org = db.get(Organization, LOCAL_ORG_ID)
    if org is None:
        org = Organization(id=LOCAL_ORG_ID, name=LOCAL_ORG_NAME)
        db.add(org)
        db.flush()
    return org


def _ensure_owner_user(db: Session, org: Organization) -> User:
    user = db.get(User, LOCAL_OWNER_USER_ID)
    if user is None:
        user = User(id=LOCAL_OWNER_USER_ID, org_id=org.id, email=LOCAL_OWNER_EMAIL, role=OrgRole.OWNER)
        db.add(user)
        db.flush()
    return user


def _ensure_project(db: Session, org: Organization) -> Project:
    project = db.get(Project, LOCAL_PROJECT_ID)
    if project is None:
        project = Project(id=LOCAL_PROJECT_ID, org_id=org.id, name=LOCAL_PROJECT_NAME)
        db.add(project)
        db.flush()
    return project


def _get_membership(db: Session, project_id: str, user_id: str) -> Optional[ProjectMembership]:
    return db.execute(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project_id, ProjectMembership.user_id == user_id
        )
    ).scalar_one_or_none()


def _ensure_owner_membership(db: Session, project: Project, user: User) -> ProjectMembership:
    membership = _get_membership(db, project.id, user.id)
    if membership is None:
        membership = ProjectMembership(
            project_id=project.id, user_id=user.id, role=ProjectRole.OWNER, granted_by=user.id
        )
        db.add(membership)
        db.flush()
    return membership
