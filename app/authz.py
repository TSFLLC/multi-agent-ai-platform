"""Project-scoped authorization — MA1B.

Authorization is based on ``project_memberships`` (the frozen MA0
contract, Section 24.4 #6) — not on ``users.role`` (org-level) and not on
"the caller happens to be the only local user." A user with no
``project_memberships`` row for a given project has no access to it,
full stop, even if they are the org's Owner — this is what makes
cross-project access denial a real, structural guarantee rather than an
accident of V1 only having one user today.

The role names below are exactly ``app.db.enums.ProjectRole``
(OWNER/ADMIN/MEMBER/VIEWER) — nothing renamed or invented.
"""

import enum
from typing import Callable, Dict, FrozenSet, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.db.enums import OrgRole, ProjectRole
from app.errors import ForbiddenError, NotFoundError
from app.models.identity import Project, ProjectMembership, User


class ProjectAction(str, enum.Enum):
    READ = "read"
    MODIFY = "modify"
    ADMIN = "admin"  # consequential/administrative actions


# Explicit, small policy — deliberately not "clever" or data-driven.
_PERMISSIONS: Dict[ProjectRole, FrozenSet[ProjectAction]] = {
    ProjectRole.VIEWER: frozenset({ProjectAction.READ}),
    ProjectRole.MEMBER: frozenset({ProjectAction.READ, ProjectAction.MODIFY}),
    ProjectRole.ADMIN: frozenset({ProjectAction.READ, ProjectAction.MODIFY, ProjectAction.ADMIN}),
    ProjectRole.OWNER: frozenset({ProjectAction.READ, ProjectAction.MODIFY, ProjectAction.ADMIN}),
}


def role_permits(role: ProjectRole, action: ProjectAction) -> bool:
    return action in _PERMISSIONS[role]


def get_project_membership(db: Session, *, project_id: str, user_id: str) -> Optional[ProjectMembership]:
    return db.execute(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project_id, ProjectMembership.user_id == user_id
        )
    ).scalar_one_or_none()


def require_project_access(action: ProjectAction) -> Callable[..., ProjectMembership]:
    """FastAPI dependency factory. The returned dependency reads
    ``project_id`` from the route's path parameters, so every route using
    it must declare a ``project_id`` path segment."""

    def _dependency(
        project_id: str,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> ProjectMembership:
        project = db.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found.")

        membership = get_project_membership(db, project_id=project_id, user_id=user.id)
        if membership is None:
            raise ForbiddenError(f"No project membership for project {project_id}.")
        if not role_permits(membership.role, action):
            raise ForbiddenError(
                f"Role {membership.role.value!r} does not permit {action.value!r} on project {project_id}."
            )
        return membership

    return _dependency


def require_org_admin(org_id: str, user: User = Depends(get_current_user)) -> User:
    """Org-scoped authorization (e.g. audit log access) uses
    ``users.role`` (OrgRole), the org-level counterpart of
    ``project_memberships`` — a resource scoped to the whole org, not a
    single project, is not naturally a project_memberships question.
    An org_id that doesn't match the caller's own org is rejected the
    same way an unauthorized project_id is: no existence-vs-access
    distinction leaked to the caller."""
    if user.org_id != org_id:
        raise ForbiddenError(f"Not authorized for organization {org_id}.")
    if user.role not in (OrgRole.OWNER, OrgRole.ADMIN):
        raise ForbiddenError(f"Role {user.role.value!r} does not permit this administrative action.")
    return user
