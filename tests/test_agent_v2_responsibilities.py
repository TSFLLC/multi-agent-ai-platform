"""Starter Agent v2 substantive responsibilities — MA7.7D.

Verifies the six original starter Agents' v2 content (published through
the existing Agent Registry service/API, never the app-startup seed) and
that publishing v2 never mutates v1 — Section 12.3's frozen invariant.
"""

from app.agent_responsibility_upgrades import AGENT_V2_UPGRADES
from app.db.enums import VersionStatus
from app.models.agents import AgentVersion, PromptVersion
from app.starter_agents import STARTER_AGENTS, ensure_starter_agents


def test_v2_upgrade_specs_cover_exactly_the_six_original_agents():
    upgrade_roles = {spec.role for spec in AGENT_V2_UPGRADES}
    original_roles = {spec.role for spec in STARTER_AGENTS if spec.role != "final_reviewer"}
    assert upgrade_roles == original_roles
    assert len(AGENT_V2_UPGRADES) == 6


def test_each_v2_spec_has_a_must_not_section():
    for spec in AGENT_V2_UPGRADES:
        assert "must NOT" in spec.prompt


def test_specs_that_could_wrongly_self_approve_defer_to_human_approval():
    """Every v2 spec whose role could plausibly judge/approve the overall
    result must explicitly defer to Human Approval; the Software
    Engineer's must-not list is about scope/self-grading, not approval
    language, per the frozen spec, so it is deliberately excluded here."""
    for role in ("planner", "test_engineer", "code_reviewer", "security_reviewer", "research_agent"):
        spec = next(s for s in AGENT_V2_UPGRADES if s.role == role)
        assert "Human Approval" in spec.prompt


def test_planner_v2_covers_required_responsibilities():
    spec = next(s for s in AGENT_V2_UPGRADES if s.role == "planner")
    for phrase in ("acceptance criteria", "out-of-scope", "dependencies", "Research"):
        assert phrase in spec.prompt
    assert "Implement the solution yourself" in spec.prompt


def test_software_engineer_v2_covers_required_responsibilities():
    spec = next(s for s in AGENT_V2_UPGRADES if s.role == "software_engineer")
    for phrase in ("minimally", "scope", "assumptions", "blockers"):
        assert phrase in spec.prompt


def test_test_engineer_v2_covers_required_responsibilities():
    spec = next(s for s in AGENT_V2_UPGRADES if s.role == "test_engineer")
    for phrase in ("boundary", "regression", "Fabricate", "pass/fail"):
        assert phrase in spec.prompt


def test_code_reviewer_v2_covers_required_responsibilities():
    spec = next(s for s in AGENT_V2_UPGRADES if s.role == "code_reviewer")
    for phrase in ("severity", "edge cases", "maintainability"):
        assert phrase in spec.prompt


def test_security_reviewer_v2_covers_required_responsibilities():
    spec = next(s for s in AGENT_V2_UPGRADES if s.role == "security_reviewer")
    for phrase in ("authentication", "authorization", "injection", "credential"):
        assert phrase in spec.prompt


def test_research_agent_v2_covers_required_responsibilities():
    spec = next(s for s in AGENT_V2_UPGRADES if s.role == "research_agent")
    for phrase in ("reusable across software, architecture, product", "traceability", "Fabricate"):
        assert phrase in spec.prompt


def test_publishing_v2_never_mutates_v1(db):
    """Exercises the same AgentRegistryService calls
    scripts/provision_agent_responsibilities.py drives over HTTP —
    publishing v2 must insert a new row, never touch v1's content."""
    from app.services.agent_registry_service import AgentRegistryService
    from tests.conftest import make_project

    project = make_project(db)
    ensure_starter_agents(db, project_id=project.id)

    planner = next(a for a in db.query(AgentVersion).filter_by(version=1).all() if a.role == "planner")
    v1_prompt = db.get(PromptVersion, planner.prompt_version_id)
    original_v1_content = v1_prompt.content
    original_v1_status = planner.status

    spec = next(s for s in AGENT_V2_UPGRADES if s.role == "planner")
    svc = AgentRegistryService(db)
    prompt_v2 = svc.create_prompt_version(agent_id=planner.agent_id, content=spec.prompt)
    version_v2 = svc.create_agent_version(
        agent_id=planner.agent_id,
        name=spec.name,
        role=spec.role,
        description=spec.description,
        prompt_version_id=prompt_v2.id,
    )
    published = svc.publish_agent_version(version_v2.id)

    db.refresh(planner)
    db.refresh(v1_prompt)
    assert v1_prompt.content == original_v1_content
    assert planner.status == original_v1_status == VersionStatus.ACTIVE
    assert published.version == 2
    assert published.status == VersionStatus.ACTIVE
    assert published.id != planner.id
    assert db.get(PromptVersion, prompt_v2.id).content == spec.prompt

    all_versions = db.query(AgentVersion).filter_by(agent_id=planner.agent_id).all()
    assert len(all_versions) == 2
