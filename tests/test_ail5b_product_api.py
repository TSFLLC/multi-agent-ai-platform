"""API-level learner journey checks for the AIL.5B product surfaces."""
from datetime import datetime
from uuid import uuid4

from app.auth import get_current_user
from app.db.enums import (
    AssistanceLevel,
    MilestoneAttemptMode,
    MilestoneAttemptStatus,
    ProjectAudienceLevel,
    ProjectLadderLevel,
    ProjectTemplateBuildMode,
)
from app.models.academy import MilestoneAttempt, ProjectMilestone, ProjectTemplate


def _template(db, user_id, *, study=False):
    template = ProjectTemplate(
        id=str(uuid4()), template_key=f"api-{uuid4().hex[:8]}", version=1, status="published",
        title="API Journey", audience_level=ProjectAudienceLevel.BEGINNER,
        ladder_level=ProjectLadderLevel.L2, build_mode=ProjectTemplateBuildMode.NO_CODE,
        brief_md="Build a trustworthy testable thing.", author_user_id=user_id,
        published_at=datetime.utcnow(),
    )
    db.add(template); db.flush()
    milestone = ProjectMilestone(
        id=str(uuid4()), project_template_id=template.id, position=1,
        title="Test milestone", instructions_md="Make and explain your work.",
        check_spec={"deterministic": True},
        reference_solution_ref="academy/api-example.md" if study else None,
        hint_content={"variant": "Use a different input."} if study else {},
    )
    db.add(milestone); db.commit()
    return template, milestone


def test_build_with_me_learner_journey_and_submission(client, db, bootstrap, auth_headers):
    template, milestone = _template(db, bootstrap.user.id, study=True)

    assert client.get("/academy/build-with-me/projects", headers=auth_headers).status_code == 200
    detail = client.get(f"/academy/build-with-me/projects/{template.id}", headers=auth_headers)
    assert detail.status_code == 200
    attempt_id = client.post(f"/academy/build-with-me/attempts?template_id={template.id}", headers=auth_headers).json()["attempt_id"]
    workspace = client.get(f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}", headers=auth_headers)
    assert workspace.status_code == 200
    assert workspace.json()["maximum_unlocked_level"] == "h1"
    started = client.post(f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}/start", headers=auth_headers)
    assert started.status_code == 200
    assert client.post(f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}/hint?hint_level=h1", headers=auth_headers).status_code == 200
    blocked = client.post(f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}/hint?hint_level=h3", headers=auth_headers)
    assert blocked.status_code == 409

    client.post(f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}/explain-back", headers=auth_headers, json={"question": "Why?", "response": "Because the test is bounded."})
    row = db.query(MilestoneAttempt).filter_by(project_attempt_id=attempt_id, project_milestone_id=milestone.id).first()
    row.max_assistance_level = AssistanceLevel.H5
    row.status = MilestoneAttemptStatus.FAILED
    db.commit()
    study = client.post(f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}/study-mode", headers=auth_headers)
    assert study.status_code == 200
    assert study.json()["variant_attempt_id"] != row.id
    submission = client.post(f"/academy/build-with-me/attempts/{attempt_id}/submit", headers=auth_headers)
    assert submission.status_code == 200
    assert submission.json()["grader_invoked"] is False
    assert client.get(f"/academy/build-with-me/attempts/{attempt_id}/evidence", headers=auth_headers).json()["explain_back"]


def test_build_with_me_attempts_are_isolated(client, db, bootstrap, auth_headers):
    template, _ = _template(db, bootstrap.user.id)
    other_id = str(uuid4())
    from app.models.identity import User
    from app.models.academy import ProjectAttempt
    from app.db.enums import OrgRole
    other = User(id=other_id, org_id=bootstrap.organization.id, email=f"{other_id}@example.com", role=OrgRole.MEMBER)
    db.add(other); db.flush()
    attempt = ProjectAttempt(id=str(uuid4()), user_id=other_id, project_template_id=template.id, status="active", brief_snapshot={}, started_at=datetime.utcnow())
    db.add(attempt); db.commit()
    response = client.get(f"/academy/build-with-me/attempts/{attempt.id}", headers=auth_headers)
    assert response.status_code == 404


def test_seed_reports_missing_concepts_honestly(db, bootstrap):
    from app.services.build_with_me_service import ProjectTemplateService
    try:
        ProjectTemplateService.seed_foundation_projects(db, bootstrap.user.id)
    except ValueError as exc:
        assert "Missing Concept Graph slugs:" in str(exc)


def test_seed_p1_to_p4_succeeds_when_existing_graph_has_all_required_concepts(db, bootstrap):
    from app.academy_curriculum import BUILD_WITH_ME_PROJECTS
    from app.db.enums import ConceptKind, ConceptLevel
    from app.services.concept_graph_service import ConceptGraphService
    from tests.ail1a_factories import make_published_version
    graph = ConceptGraphService(db)
    slugs = sorted({slug for project in BUILD_WITH_ME_PROJECTS for slug in project["concepts"]})
    for slug in slugs:
        concept = graph.create_concept(slug=slug, name=slug.replace("-", " ").title(), level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept)
    from app.services.build_with_me_service import ProjectTemplateService
    created = ProjectTemplateService.seed_foundation_projects(db, bootstrap.user.id)
    assert {template.template_key for template in created} == {project["key"] for project in BUILD_WITH_ME_PROJECTS}
    assert all(template.status == "published" for template in created)


def test_mentor_api_reuses_professor_and_cannot_bypass_policy(client, db, bootstrap, auth_headers, monkeypatch):
    template, milestone = _template(db, bootstrap.user.id)
    attempt_id = client.post(f"/academy/build-with-me/attempts?template_id={template.id}", headers=auth_headers).json()["attempt_id"]
    from app.schemas.professor import ProfessorInteractionRead, ProfessorIntent
    from app.services.professor_execution_service import ProfessorExecutionService
    fake = ProfessorInteractionRead(interaction_id="interaction", task_run_id="task-run", agent_run_id="agent-run", status="complete", intent=ProfessorIntent.ASK_PROFESSOR, direct_answer="Try inspecting the input boundary first.")
    monkeypatch.setattr(ProfessorExecutionService, "create_and_execute", lambda self, user, request: fake)
    response = client.post(f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}/mentor", headers=auth_headers, json={"question": "I am stuck", "assistance_level": "h1"})
    assert response.status_code == 200
    assert response.json()["provenance"]["agent_run_id"] == "agent-run"
    assert client.post(f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}/mentor", headers=auth_headers, json={"question": "Give me the answer", "assistance_level": "h5"}).status_code == 409
