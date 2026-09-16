"""Local bootstrap identity — MA1B.

One Organization, one Owner User, one default Project, one Owner
ProjectMembership — using the frozen MA0 identity contracts unchanged.
Idempotent: repeated calls (simulating app restarts) never duplicate.
"""

from app.bootstrap import (
    LOCAL_ORG_ID,
    LOCAL_OWNER_USER_ID,
    LOCAL_PROJECT_ID,
    ensure_local_bootstrap,
)
from app.db.enums import OrgRole, ProjectRole
from app.models.identity import Organization, Project, ProjectMembership, User


def test_creates_organization(db):
    identities = ensure_local_bootstrap(db)
    assert identities.organization.id == LOCAL_ORG_ID
    assert db.query(Organization).count() == 1


def test_creates_owner_user(db):
    identities = ensure_local_bootstrap(db)
    assert identities.user.id == LOCAL_OWNER_USER_ID
    assert identities.user.org_id == identities.organization.id
    assert identities.user.role == OrgRole.OWNER
    assert db.query(User).count() == 1


def test_creates_default_project(db):
    identities = ensure_local_bootstrap(db)
    assert identities.project.id == LOCAL_PROJECT_ID
    assert identities.project.org_id == identities.organization.id
    assert db.query(Project).count() == 1


def test_creates_owner_project_membership(db):
    identities = ensure_local_bootstrap(db)
    assert identities.membership.project_id == identities.project.id
    assert identities.membership.user_id == identities.user.id
    assert identities.membership.role == ProjectRole.OWNER
    assert db.query(ProjectMembership).count() == 1


def test_bootstrap_is_idempotent_across_repeated_calls(db):
    """Simulates restarting the application multiple times."""
    first = ensure_local_bootstrap(db)
    second = ensure_local_bootstrap(db)
    third = ensure_local_bootstrap(db)

    assert first.organization.id == second.organization.id == third.organization.id
    assert first.user.id == second.user.id == third.user.id
    assert first.project.id == second.project.id == third.project.id
    assert first.membership.id == second.membership.id == third.membership.id

    assert db.query(Organization).count() == 1
    assert db.query(User).count() == 1
    assert db.query(Project).count() == 1
    assert db.query(ProjectMembership).count() == 1


def test_bootstrap_idempotent_across_separate_sessions(session_factory):
    """The real-world restart case: a brand-new process/session calling
    bootstrap against a DB some earlier process already bootstrapped."""
    first_session = session_factory()
    try:
        first = ensure_local_bootstrap(first_session)
    finally:
        first_session.close()

    second_session = session_factory()
    try:
        second = ensure_local_bootstrap(second_session)
        assert second_session.query(Organization).count() == 1
        assert second_session.query(User).count() == 1
        assert second_session.query(Project).count() == 1
        assert second_session.query(ProjectMembership).count() == 1
    finally:
        second_session.close()

    assert first.organization.id == second.organization.id
