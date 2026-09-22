"""Starter Agent catalog seeding — Section 12.5, MA2; extended MA7.7D.

Seven starter Agents, seeded idempotently, as seed data — not hard-coded
platform behavior (users can create further Agents normally).
"""

from app.db.enums import VersionStatus
from app.models.agents import Agent, AgentVersion, PromptVersion
from app.starter_agents import STARTER_AGENTS, ensure_starter_agents
from tests.conftest import make_project


def test_seeds_exactly_seven_agents(db):
    project = make_project(db)
    agents = ensure_starter_agents(db, project_id=project.id)
    assert len(agents) == 7
    assert len(STARTER_AGENTS) == 7


def test_expected_roles_present(db):
    project = make_project(db)
    agents = ensure_starter_agents(db, project_id=project.id)
    names = {a.name for a in agents}
    assert names == {
        "Planner",
        "Software Engineer",
        "Test Engineer",
        "Code Reviewer",
        "Security Reviewer",
        "Research Agent",
        "Final Reviewer",
    }


def test_final_reviewer_seeded_with_active_v1(db):
    project = make_project(db)
    agents = ensure_starter_agents(db, project_id=project.id)
    final_reviewer = next(a for a in agents if a.role == "final_reviewer")
    assert final_reviewer.name == "Final Reviewer"
    assert final_reviewer.current_status == VersionStatus.ACTIVE

    versions = db.query(AgentVersion).filter_by(agent_id=final_reviewer.id).all()
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].status == VersionStatus.ACTIVE


def test_adding_final_reviewer_does_not_disturb_existing_six(db):
    """Simulates an existing installation that already has the original
    six (pre-MA7.7D) restarting against the updated STARTER_AGENTS list
    that now includes a seventh entry -- only the new row is created."""
    project = make_project(db)
    original_six = [spec for spec in STARTER_AGENTS if spec.role != "final_reviewer"]
    from app.starter_agents import _ensure_one

    for spec in original_six:
        _ensure_one(db, project_id=project.id, spec=spec)
    db.commit()
    assert db.query(Agent).count() == 6

    agents = ensure_starter_agents(db, project_id=project.id)
    assert len(agents) == 7
    assert db.query(Agent).count() == 7
    # The pre-existing six's rows are untouched (same ids, still exactly
    # one version each).
    for spec in original_six:
        agent = db.get(Agent, spec.id)
        assert agent is not None
        assert db.query(AgentVersion).filter_by(agent_id=agent.id).count() == 1


def test_each_starter_agent_has_a_published_version_and_prompt(db):
    project = make_project(db)
    ensure_starter_agents(db, project_id=project.id)

    for agent in db.query(Agent).all():
        versions = db.query(AgentVersion).filter_by(agent_id=agent.id).all()
        assert len(versions) == 1
        assert versions[0].status == VersionStatus.ACTIVE
        assert versions[0].prompt_version_id is not None

        prompts = db.query(PromptVersion).filter_by(agent_id=agent.id).all()
        assert len(prompts) == 1


def test_seeding_is_idempotent_across_repeated_calls(db):
    project = make_project(db)
    ensure_starter_agents(db, project_id=project.id)
    ensure_starter_agents(db, project_id=project.id)
    ensure_starter_agents(db, project_id=project.id)

    assert db.query(Agent).count() == 7
    assert db.query(AgentVersion).count() == 7
    assert db.query(PromptVersion).count() == 7


def test_seeding_idempotent_across_separate_sessions(session_factory):
    project_id = None
    s1 = session_factory()
    try:
        project = make_project(s1)
        project_id = project.id
        ensure_starter_agents(s1, project_id=project_id)
    finally:
        s1.close()

    s2 = session_factory()
    try:
        ensure_starter_agents(s2, project_id=project_id)
        assert s2.query(Agent).count() == 7
    finally:
        s2.close()


def test_starter_agents_are_not_hard_coded_elsewhere():
    """Only app.starter_agents itself should know these six names — a
    static search guard against the six leaking into application logic
    as hard-coded assumptions."""
    import inspect

    from app.services import agent_registry_service

    source = inspect.getsource(agent_registry_service)
    for spec in STARTER_AGENTS:
        assert spec.name not in source


def test_users_can_still_create_additional_agents_freely(db):
    """Seeding does not prevent normal Agent creation."""
    from app.services.agent_registry_service import AgentRegistryService

    project = make_project(db)
    ensure_starter_agents(db, project_id=project.id)

    svc = AgentRegistryService(db)
    custom = svc.create_agent(project_id=project.id, name="Custom Agent", role="custom")
    assert db.query(Agent).count() == 8
    assert custom.name == "Custom Agent"
