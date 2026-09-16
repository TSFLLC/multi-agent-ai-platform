"""Project-scoped authorization — MA1B.

Based on project_memberships (never users.role) for project-level
actions; org-scoped resources (like audit) use users.role instead.
Frozen ProjectRole/OrgRole enums only — nothing renamed or invented.
"""

import pytest

from app.authz import ProjectAction, require_org_admin, require_project_access, role_permits
from app.db.enums import OrgRole, ProjectRole
from app.errors import ForbiddenError, NotFoundError
from app.models.identity import ProjectMembership, User
from tests.conftest import make_org, make_project

# --- policy table -----------------------------------------------------


@pytest.mark.parametrize(
    "role,action,expected",
    [
        (ProjectRole.VIEWER, ProjectAction.READ, True),
        (ProjectRole.VIEWER, ProjectAction.MODIFY, False),
        (ProjectRole.VIEWER, ProjectAction.ADMIN, False),
        (ProjectRole.MEMBER, ProjectAction.READ, True),
        (ProjectRole.MEMBER, ProjectAction.MODIFY, True),
        (ProjectRole.MEMBER, ProjectAction.ADMIN, False),
        (ProjectRole.ADMIN, ProjectAction.READ, True),
        (ProjectRole.ADMIN, ProjectAction.MODIFY, True),
        (ProjectRole.ADMIN, ProjectAction.ADMIN, True),
        (ProjectRole.OWNER, ProjectAction.READ, True),
        (ProjectRole.OWNER, ProjectAction.MODIFY, True),
        (ProjectRole.OWNER, ProjectAction.ADMIN, True),
    ],
)
def test_role_permits_matrix(role, action, expected):
    assert role_permits(role, action) is expected


def test_policy_covers_every_frozen_project_role():
    """No role invented/dropped — exactly the four ProjectRole values."""
    from app.authz import _PERMISSIONS

    assert set(_PERMISSIONS.keys()) == set(ProjectRole)


# --- require_project_access --------------------------------------------


def _member(db, project, user, role):
    m = ProjectMembership(project_id=project.id, user_id=user.id, role=role)
    db.add(m)
    db.flush()
    return m


def test_require_project_access_allows_sufficient_role(db):
    org = make_org(db)
    project = make_project(db, org=org)
    user = User(org_id=org.id, email="viewer@x", role=OrgRole.MEMBER)
    db.add(user)
    db.flush()
    _member(db, project, user, ProjectRole.MEMBER)

    dep = require_project_access(ProjectAction.MODIFY)
    membership = dep(project_id=project.id, user=user, db=db)
    assert membership.role == ProjectRole.MEMBER


def test_require_project_access_denies_insufficient_role(db):
    org = make_org(db)
    project = make_project(db, org=org)
    user = User(org_id=org.id, email="viewer2@x", role=OrgRole.MEMBER)
    db.add(user)
    db.flush()
    _member(db, project, user, ProjectRole.VIEWER)

    dep = require_project_access(ProjectAction.MODIFY)
    with pytest.raises(ForbiddenError):
        dep(project_id=project.id, user=user, db=db)


def test_require_project_access_denies_non_member_even_if_org_owner(db):
    """The core guarantee: org-level Owner status never substitutes for a
    project_memberships row. Authorization is project-membership-based,
    full stop."""
    org = make_org(db)
    project = make_project(db, org=org)
    org_owner = User(org_id=org.id, email="orgowner@x", role=OrgRole.OWNER)
    db.add(org_owner)
    db.flush()
    # Deliberately no ProjectMembership row for org_owner on this project.

    dep = require_project_access(ProjectAction.READ)
    with pytest.raises(ForbiddenError):
        dep(project_id=project.id, user=org_owner, db=db)


def test_require_project_access_denies_membership_in_a_different_project(db):
    """Cross-project access denial: a member of project A has no implicit
    access to project B."""
    org = make_org(db)
    project_a = make_project(db, org=org, name="A")
    project_b = make_project(db, org=org, name="B")
    user = User(org_id=org.id, email="crossproj@x", role=OrgRole.MEMBER)
    db.add(user)
    db.flush()
    _member(db, project_a, user, ProjectRole.OWNER)

    dep = require_project_access(ProjectAction.READ)
    dep(project_id=project_a.id, user=user, db=db)  # allowed
    with pytest.raises(ForbiddenError):
        dep(project_id=project_b.id, user=user, db=db)  # denied


def test_require_project_access_nonexistent_project_is_not_found(db):
    org = make_org(db)
    user = User(org_id=org.id, email="x@x", role=OrgRole.MEMBER)
    db.add(user)
    db.flush()

    dep = require_project_access(ProjectAction.READ)
    with pytest.raises(NotFoundError):
        dep(project_id="00000000-0000-0000-0000-000000000999", user=user, db=db)


# --- require_org_admin --------------------------------------------------


def test_require_org_admin_allows_owner(db):
    org = make_org(db)
    owner = User(org_id=org.id, email="owner@x", role=OrgRole.OWNER)
    db.add(owner)
    db.flush()

    result = require_org_admin(org_id=org.id, user=owner)
    assert result.id == owner.id


def test_require_org_admin_allows_admin(db):
    org = make_org(db)
    admin = User(org_id=org.id, email="admin@x", role=OrgRole.ADMIN)
    db.add(admin)
    db.flush()

    require_org_admin(org_id=org.id, user=admin)  # does not raise


def test_require_org_admin_denies_member(db):
    org = make_org(db)
    member = User(org_id=org.id, email="member@x", role=OrgRole.MEMBER)
    db.add(member)
    db.flush()

    with pytest.raises(ForbiddenError):
        require_org_admin(org_id=org.id, user=member)


def test_require_org_admin_denies_mismatched_org_id_even_for_owner(db):
    """Supplying a different org_id can never be used to bypass
    authorization, even by an Owner of some other org."""
    org_a = make_org(db, name="A")
    org_b = make_org(db, name="B")
    owner_of_a = User(org_id=org_a.id, email="ownerA@x", role=OrgRole.OWNER)
    db.add(owner_of_a)
    db.flush()

    with pytest.raises(ForbiddenError):
        require_org_admin(org_id=org_b.id, user=owner_of_a)
