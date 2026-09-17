"""Agent Registry Service — CRUD, versioning, lifecycle transitions,
Prompt Version linkage — Section 12, 26.7, MA2."""

import pytest

from app.db.enums import VersionStatus
from app.errors import InvalidStateTransitionError, NotFoundError
from app.models.agents import Agent, AgentVersion, PromptVersion
from app.services.agent_registry_service import AgentRegistryService
from tests.conftest import make_project


def test_create_and_get_agent(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="Backend Engineer", role="engineer")

    assert svc.get_agent(agent.id).id == agent.id
    assert db.query(Agent).count() == 1


def test_list_agents_scoped_to_project(db):
    project_a = make_project(db, name="A")
    project_b = make_project(db, name="B")
    svc = AgentRegistryService(db)
    svc.create_agent(project_id=project_a.id, name="Agent A", role="x")
    svc.create_agent(project_id=project_b.id, name="Agent B", role="x")

    assert [a.name for a in svc.list_agents(project_id=project_a.id)] == ["Agent A"]


def test_create_agent_version_starts_at_one_and_increments(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")

    v1 = svc.create_agent_version(agent_id=agent.id, name="X", role="x")
    v2 = svc.create_agent_version(agent_id=agent.id, name="X", role="x")

    assert v1.version == 1
    assert v2.version == 2
    assert v1.status == VersionStatus.DRAFT


def test_publish_transitions_draft_to_active(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")
    version = svc.create_agent_version(agent_id=agent.id, name="X", role="x")

    published = svc.publish_agent_version(version.id)

    assert published.status == VersionStatus.ACTIVE
    assert published.published_at is not None
    db.refresh(agent)
    assert agent.current_status == VersionStatus.ACTIVE


def test_publish_immutable_content_not_mutated(db):
    """Publishing must never change name/role/description/prompt_version_id
    — only status/published_at (Section 12.3)."""
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")
    version = svc.create_agent_version(agent_id=agent.id, name="Original Name", role="original_role")

    published = svc.publish_agent_version(version.id)

    assert published.name == "Original Name"
    assert published.role == "original_role"


def test_publish_already_published_version_is_invalid(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")
    version = svc.create_agent_version(agent_id=agent.id, name="X", role="x")
    svc.publish_agent_version(version.id)

    with pytest.raises(InvalidStateTransitionError):
        svc.publish_agent_version(version.id)


def test_deprecate_requires_active(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")
    version = svc.create_agent_version(agent_id=agent.id, name="X", role="x")

    with pytest.raises(InvalidStateTransitionError):
        svc.deprecate_agent_version(version.id)  # still DRAFT

    svc.publish_agent_version(version.id)
    deprecated = svc.deprecate_agent_version(version.id)
    assert deprecated.status == VersionStatus.DEPRECATED


def test_retire_allowed_from_active_or_deprecated(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")

    v1 = svc.create_agent_version(agent_id=agent.id, name="X", role="x")
    svc.publish_agent_version(v1.id)
    retired_from_active = svc.retire_agent_version(v1.id)
    assert retired_from_active.status == VersionStatus.RETIRED

    v2 = svc.create_agent_version(agent_id=agent.id, name="X", role="x")
    svc.publish_agent_version(v2.id)
    svc.deprecate_agent_version(v2.id)
    retired_from_deprecated = svc.retire_agent_version(v2.id)
    assert retired_from_deprecated.status == VersionStatus.RETIRED


def test_retire_from_draft_is_invalid(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")
    version = svc.create_agent_version(agent_id=agent.id, name="X", role="x")

    with pytest.raises(InvalidStateTransitionError):
        svc.retire_agent_version(version.id)


def test_retired_cannot_be_republished_or_redeprecated(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")
    version = svc.create_agent_version(agent_id=agent.id, name="X", role="x")
    svc.publish_agent_version(version.id)
    svc.retire_agent_version(version.id)

    with pytest.raises(InvalidStateTransitionError):
        svc.publish_agent_version(version.id)
    with pytest.raises(InvalidStateTransitionError):
        svc.deprecate_agent_version(version.id)


def test_transition_on_nonexistent_version_raises_not_found(db):
    svc = AgentRegistryService(db)
    with pytest.raises(NotFoundError):
        svc.publish_agent_version("00000000-0000-0000-0000-000000000999")


def test_prompt_version_linkage(db):
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")

    pv1 = svc.create_prompt_version(agent_id=agent.id, content="You are helpful.")
    pv2 = svc.create_prompt_version(agent_id=agent.id, content="You are extremely helpful.")
    assert pv1.version == 1
    assert pv2.version == 2

    version = svc.create_agent_version(agent_id=agent.id, name="X", role="x", prompt_version_id=pv1.id)
    assert version.prompt_version_id == pv1.id
    assert db.query(PromptVersion).count() == 2


def test_editing_creates_new_version_never_mutates_prior(db):
    """ "Editing" a published agent is a new version row — the prior one's
    columns are untouched (the reproducibility invariant)."""
    project = make_project(db)
    svc = AgentRegistryService(db)
    agent = svc.create_agent(project_id=project.id, name="X", role="x")
    v1 = svc.create_agent_version(agent_id=agent.id, name="V1 Name", role="x")
    svc.publish_agent_version(v1.id)

    v2 = svc.create_agent_version(agent_id=agent.id, name="V2 Name", role="x")

    db.refresh(v1)
    assert v1.name == "V1 Name"
    assert v2.name == "V2 Name"
    assert v2.id != v1.id
    assert db.query(AgentVersion).count() == 2
