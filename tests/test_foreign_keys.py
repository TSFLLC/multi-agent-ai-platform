"""FK enforcement — Section 10.5.2 (PRAGMA foreign_keys=ON is non-negotiable)."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import VersionStatus
from app.models.agents import AgentVersion
from tests.conftest import make_agent


def test_orphan_agent_version_rejected(db):
    """An agent_versions row referencing a nonexistent agent_id must fail —
    proves PRAGMA foreign_keys=ON is actually active, not just configured."""
    bad = AgentVersion(
        agent_id="00000000-0000-0000-0000-000000000000",
        version=1,
        name="x",
        role="x",
        status=VersionStatus.DRAFT,
    )
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_valid_agent_version_succeeds(db):
    agent = make_agent(db)
    av = AgentVersion(agent_id=agent.id, version=1, name="x", role="x", status=VersionStatus.DRAFT)
    db.add(av)
    db.commit()
    assert av.id is not None


def test_cascade_delete_removes_dependent_versions(db):
    agent = make_agent(db)
    av = AgentVersion(agent_id=agent.id, version=1, name="x", role="x", status=VersionStatus.DRAFT)
    db.add(av)
    db.commit()

    db.delete(agent)
    db.commit()

    remaining = db.query(AgentVersion).filter_by(id=av.id).first()
    assert remaining is None
