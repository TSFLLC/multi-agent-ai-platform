"""Prompt Version relationships — Section 12.4, 24.4 #3."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import VersionStatus
from app.models.agents import AgentVersion, PromptVersion
from tests.conftest import make_agent


def test_prompt_version_versioned_independently_of_agent_version(db):
    """Same Agent Version wrapper, different prompt_version_id — enables
    comparison axis 3 (same task/model/role, different prompt wording)
    without minting a new Agent Version."""
    agent = make_agent(db)
    pv1 = PromptVersion(agent_id=agent.id, version=1, content="You are helpful.")
    pv2 = PromptVersion(agent_id=agent.id, version=2, content="You are extremely helpful.")
    db.add_all([pv1, pv2])
    db.commit()

    av = AgentVersion(
        agent_id=agent.id,
        version=1,
        name=agent.name,
        role=agent.role,
        prompt_version_id=pv1.id,
        status=VersionStatus.ACTIVE,
    )
    db.add(av)
    db.commit()

    assert av.prompt_version_id == pv1.id
    assert pv1.id != pv2.id


def test_duplicate_prompt_version_number_rejected(db):
    agent = make_agent(db)
    db.add(PromptVersion(agent_id=agent.id, version=1, content="a"))
    db.commit()

    db.add(PromptVersion(agent_id=agent.id, version=1, content="b"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_prompt_versions_scoped_per_agent_not_per_agent_version(db):
    """Two different Agents may each independently have their own
    prompt_versions numbered starting at 1 — the uniqueness is
    (agent_id, version), not global."""
    agent_a = make_agent(db, name="Agent A")
    agent_b = make_agent(db, name="Agent B")
    db.add(PromptVersion(agent_id=agent_a.id, version=1, content="a"))
    db.add(PromptVersion(agent_id=agent_b.id, version=1, content="b"))
    db.commit()
