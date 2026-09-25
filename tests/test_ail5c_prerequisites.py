"""AIL.5C CP0 — prerequisite corrections.

Covers: the single independence policy, the Learner State platform floor and
v2 requirement constraints, supersession, multi-hop CHANGED, publish-time
requirement validation, 5B server-side evidence derivation, 5B submission
finalization, and owner-scoping of shared-AIL-project records.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db.enums import (
    AssistanceLevel,
    ChangeSeverity,
    ConceptKind,
    EvidenceType,
    ExecutionVerification,
    GradingMode,
    QuestionOrigin,
)
from app.errors import ConflictError
from app.services import independence_policy as policy
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_state_service import (
    CHANGED,
    DEMONSTRATED,
    PRACTICED,
    UNDERSTOOD,
    LearnerStateService,
)
from app.services.learning_evidence_service import LearningEvidenceService
from tests.ail1a_factories import make_concept, make_published_version, make_user

H = AssistanceLevel
V = ExecutionVerification

_LAB_LEG = {"requires_all": [{"evidence_type": "lab", "min_passed": 1}]}


def _row(**kw):
    base = dict(
        superseded_by_id=None,
        on_demo_data=False,
        grader=GradingMode.DETERMINISTIC,
        execution_verification=None,
        assistance_level=None,
        evidence_type=EvidenceType.LAB,
        milestone_attempt_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _lab(db, user, version, *, assistance=None, verification=None, grader=GradingMode.DETERMINISTIC,
         demo=False, kind=EvidenceType.LAB, milestone=None):
    return LearningEvidenceService(db).record_evidence(
        user_id=user.id, concept_id=version.concept_id, concept_version_id=version.id,
        evidence_type=kind, grader=grader, passed=True, assistance_level=assistance,
        execution_verification=verification, on_demo_data=demo, milestone_attempt_id=milestone,
    )


def _milestone(db, user):
    from app.models.academy import MilestoneAttempt, ProjectAttempt
    from tests.test_ail5b_product_api import _template

    template, milestone = _template(db, user.id)
    attempt = ProjectAttempt(user_id=user.id, project_template_id=template.id, brief_snapshot={})
    db.add(attempt)
    db.flush()
    row = MilestoneAttempt(project_attempt_id=attempt.id, project_milestone_id=milestone.id)
    db.add(row)
    db.commit()
    return row.id


def _state(db, user, concept_id):
    return LearnerStateService(db).state(user.id, concept_id)


# -- the single policy ----------------------------------------------------------


def test_policy_classifies_assistance_in_one_place():
    assert [policy.classify_assistance(x) for x in (H.H0, H.H1, H.H2)] == [policy.FULL] * 3
    assert policy.classify_assistance(H.H3) == policy.PARTIAL
    assert policy.classify_assistance(H.H4) == policy.FORMATIVE
    assert policy.classify_assistance(H.H5) == policy.FORMATIVE
    assert policy.classify_assistance(None) == policy.FULL


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"superseded_by_id": "x"}, "superseded"),
        ({"on_demo_data": True}, "demo_data"),
        ({"grader": GradingMode.SELF}, "self_reported"),
        ({"execution_verification": V.SELF_REPORTED}, "self_reported"),
        ({"assistance_level": H.H3}, "h3_partial_structure"),
        ({"assistance_level": H.H4}, "h4_guided_walkthrough"),
        ({"assistance_level": H.H5}, "h5_solution_shown"),
        ({"evidence_type": EvidenceType.MODIFICATION, "execution_verification": V.NOT_APPLICABLE}, "unverified_execution"),
        ({"milestone_attempt_id": "m1", "execution_verification": V.NOT_APPLICABLE}, "unverified_execution"),
    ],
)
def test_platform_floor_reasons(kwargs, reason):
    ok, why = policy.demonstration_floor(_row(**kwargs))
    assert (ok, why) == (False, reason)


def test_platform_floor_passes_independent_verified_work():
    assert policy.demonstration_floor(_row(assistance_level=H.H2, execution_verification=V.PLATFORM_VERIFIED,
                                           milestone_attempt_id="m1")) == (True, "ok")
    assert policy.demonstration_floor(_row()) == (True, "ok")  # e.g. a 3C experiment lab row


def test_leg_constraints_can_only_tighten_the_floor():
    verified = _row(assistance_level=H.H2, execution_verification=V.PLATFORM_VERIFIED)
    assert policy.leg_constraints_ok(verified, {"max_assistance": "h2"})
    assert not policy.leg_constraints_ok(verified, {"max_assistance": "h1"})
    # h4 cannot loosen the H2 floor
    assert not policy.counts_toward_demonstrated(_row(assistance_level=H.H3), {"max_assistance": "h4"})
    assert not policy.leg_constraints_ok(_row(), {"min_verification": "platform_verified"})
    assert policy.leg_constraints_ok(verified, {"min_verification": "platform_verified"})
    assert not policy.leg_constraints_ok(verified, {"grader_in": ["ai_rubric"]})
    assert not policy.leg_constraints_ok(verified, {"max_assistance": "nonsense"})  # fail closed


def test_fresh_challenge_policy_is_deterministic():
    assert policy.fresh_challenge_required(policy.IF_ASSISTED, source_levels=[H.H1]) == (False, "source_work_independent")
    assert policy.fresh_challenge_required(policy.IF_ASSISTED, source_levels=[H.H3])[0]
    assert policy.fresh_challenge_required(policy.IF_ASSISTED, source_levels=[H.H5])[0]
    assert policy.fresh_challenge_required(policy.IF_ASSISTED, study_mode_used=True) == (True, "study_mode_used")
    assert policy.fresh_challenge_required(policy.NEVER, source_levels=[H.H5]) == (False, "policy_never")
    assert policy.fresh_challenge_required(policy.NEVER, kind_is_capstone=True)[0]
    assert policy.fresh_challenge_required(policy.ALWAYS)[0]


def test_declared_ai_assistant_is_formative():
    assert policy.attestation_is_formative({"declaration": "used_ai_assistant"})
    assert not policy.attestation_is_formative({"declaration": "used_docs"})
    assert not policy.attestation_is_formative(None)


# -- Learner State floor ------------------------------------------------------------


def _user_concept(db, requirements=None, kind=ConceptKind.MECHANISM):
    user = make_user(db, email=f"u{uuid4().hex[:6]}@example.com")
    concept = make_concept(db, slug=f"c-{uuid4().hex[:8]}", name="C", kind=kind)
    version = make_published_version(db, concept, evidence_requirements=requirements or _LAB_LEG)
    return user, concept, version


def test_verified_h2_lab_demonstrates(db):
    user, concept, version = _user_concept(db)
    _lab(db, user, version, assistance=H.H2, verification=V.PLATFORM_VERIFIED, milestone=_milestone(db, user))
    assert _state(db, user, concept.id).ladder == DEMONSTRATED


@pytest.mark.parametrize("assistance", [H.H3, H.H4])
def test_assisted_lab_reaches_practiced_never_demonstrated(db, assistance):
    """Regression for the pre-5C defect: an H3/H4 LAB row used to satisfy DEMONSTRATED."""
    user, concept, version = _user_concept(db)
    _lab(db, user, version, assistance=assistance, verification=V.PLATFORM_VERIFIED, milestone=_milestone(db, user))
    assert _state(db, user, concept.id).ladder == PRACTICED


def test_h5_only_work_never_demonstrates_or_practices(db):
    user, concept, version = _user_concept(db)
    _lab(db, user, version, assistance=H.H5, verification=V.PLATFORM_VERIFIED, milestone=_milestone(db, user))
    state = _state(db, user, concept.id)
    assert state.ladder not in (PRACTICED, DEMONSTRATED)


def test_self_reported_and_demo_evidence_never_demonstrate(db):
    user, concept, version = _user_concept(db)
    _lab(db, user, version, assistance=H.H0, verification=V.SELF_REPORTED, grader=GradingMode.SELF, milestone=_milestone(db, user))
    _lab(db, user, version, demo=True)
    assert _state(db, user, concept.id).ladder == PRACTICED


def test_unverified_project_lab_does_not_demonstrate(db):
    user, concept, version = _user_concept(db)
    _lab(db, user, version, assistance=H.H0, verification=V.NOT_APPLICABLE, milestone=_milestone(db, user))
    assert _state(db, user, concept.id).ladder == PRACTICED


def test_ai_judgment_alone_never_demonstrates(db):
    user, concept, version = _user_concept(db, {"requires_all": [{"evidence_type": "lab", "min_passed": 1}]})
    _lab(db, user, version, grader=GradingMode.AI_RUBRIC)
    assert _state(db, user, concept.id).ladder == PRACTICED


def test_superseded_evidence_counts_toward_nothing_but_stays_visible(db):
    user, concept, version = _user_concept(db)
    row = _lab(db, user, version)
    assert _state(db, user, concept.id).ladder == DEMONSTRATED
    replacement = _lab(db, user, version, grader=GradingMode.HUMAN, kind=EvidenceType.INTERPRETATION)
    row.superseded_by_id = replacement.id
    db.commit()
    state = _state(db, user, concept.id)
    assert state.ladder != DEMONSTRATED and row in state.evidence


def test_v2_constraints_tighten_a_requirement(db):
    req = {"requires_all": [{"evidence_type": "lab", "min_passed": 1, "max_assistance": "h1",
                             "min_verification": "platform_verified", "grader_in": ["deterministic"]}]}
    user, concept, version = _user_concept(db, req)
    _lab(db, user, version, assistance=H.H2, verification=V.PLATFORM_VERIFIED, milestone=_milestone(db, user))
    assert _state(db, user, concept.id).ladder == PRACTICED  # h2 is inside the floor but outside this leg
    _lab(db, user, version, assistance=H.H1, verification=V.PLATFORM_VERIFIED, milestone=_milestone(db, user))
    assert _state(db, user, concept.id).ladder == DEMONSTRATED


def test_state_is_recomputed_never_stored(db):
    user, concept, version = _user_concept(db)
    assert _state(db, user, concept.id).ladder != DEMONSTRATED
    _lab(db, user, version)
    assert _state(db, user, concept.id).ladder == DEMONSTRATED


# -- multi-hop CHANGED ---------------------------------------------------------------


def _publish(db, concept, severity):
    service = ConceptGraphService(db)
    draft = service.create_draft_version(concept_id=concept.id, plain_definition="Updated.",
                                         change_severity=severity, evidence_requirements=_LAB_LEG)
    return service.publish_version(draft.id)


def test_a_material_hop_followed_by_a_minor_hop_is_still_changed(db):
    user, concept, version = _user_concept(db)
    _lab(db, user, version)
    _publish(db, concept, ChangeSeverity.MATERIAL)
    _publish(db, concept, ChangeSeverity.MINOR)
    state = _state(db, user, concept.id)
    assert state.ladder == DEMONSTRATED and CHANGED in state.overlays  # DEMONSTRATED + CHANGED coexist


def test_evidence_on_the_newest_version_clears_changed(db):
    user, concept, version = _user_concept(db)
    _lab(db, user, version)
    newest = _publish(db, concept, ChangeSeverity.MATERIAL)
    _lab(db, user, newest)
    assert CHANGED not in _state(db, user, concept.id).overlays


def test_minor_only_history_is_not_changed(db):
    user, concept, version = _user_concept(db)
    _lab(db, user, version)
    _publish(db, concept, ChangeSeverity.MINOR)
    assert CHANGED not in _state(db, user, concept.id).overlays


# -- publish-time validation ------------------------------------------------------------


def test_publish_rejects_an_ai_judged_only_requirement_set(db):
    concept = make_concept(db, slug="only-ai", name="Only AI")
    service = ConceptGraphService(db)
    draft = service.create_draft_version(
        concept_id=concept.id, plain_definition="x",
        evidence_requirements={"requires_all": [{"evidence_type": "explain_back", "min_passed": 1}]},
    )
    with pytest.raises(ConflictError):
        service.publish_version(draft.id)


def test_publish_accepts_ai_leg_paired_with_a_deterministic_leg(db):
    concept = make_concept(db, slug="paired", name="Paired")
    service = ConceptGraphService(db)
    draft = service.create_draft_version(
        concept_id=concept.id, plain_definition="x",
        evidence_requirements={"requires_all": [
            {"evidence_type": "knowledge_check", "min_passed": 1},
            {"evidence_type": "explain_back", "min_passed": 1}]},
    )
    assert service.publish_version(draft.id).status.value == "active"


@pytest.mark.parametrize("bad", [
    {"requires_all": [{"evidence_type": "nonsense"}]},
    {"requires_all": [{"evidence_type": "lab", "max_assistance": "h9"}]},
    {"requires_all": [{"evidence_type": "lab", "min_verification": "self_reported"}]},
    {"requires_all": [{"evidence_type": "lab", "grader_in": ["oracle"]}]},
])
def test_publish_rejects_malformed_requirements(db, bad):
    concept = make_concept(db, slug=f"bad-{uuid4().hex[:6]}", name="Bad")
    service = ConceptGraphService(db)
    draft = service.create_draft_version(concept_id=concept.id, plain_definition="x", evidence_requirements=bad)
    with pytest.raises(ConflictError):
        service.publish_version(draft.id)


def test_reviewed_knowledge_check_still_understood(db):
    user, concept, version = _user_concept(db)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id, concept_id=concept.id, concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK, grader=GradingMode.DETERMINISTIC, passed=True,
        question_origin=QuestionOrigin.REVIEWED)
    assert _state(db, user, concept.id).ladder == UNDERSTOOD


# -- 5B server-side derivation ----------------------------------------------------------------


def _project_attempt(client, db, bootstrap, auth_headers):
    from tests.test_ail5b_product_api import _template

    template, milestone = _template(db, bootstrap.user.id)
    attempt_id = client.post(
        f"/academy/build-with-me/attempts?template_id={template.id}", headers=auth_headers
    ).json()["attempt_id"]
    return attempt_id, milestone


def _concept_for(db):
    concept = make_concept(db, slug=f"e-{uuid4().hex[:8]}", name="E", kind=ConceptKind.MECHANISM)
    make_published_version(db, concept, evidence_requirements=_LAB_LEG)
    db.commit()
    return concept


def test_client_cannot_mint_platform_verified_evidence(client, db, bootstrap, auth_headers):
    attempt_id, milestone = _project_attempt(client, db, bootstrap, auth_headers)
    concept = _concept_for(db)
    resp = client.post(
        f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}/evidence",
        headers=auth_headers,
        json={"concept_id": concept.id, "passed": True, "source_type": "manual",
              "execution_verification": "platform_verified", "assistance_level": "h0"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["execution_verification"] == "self_reported"
    assert body["verification_basis"] == "self_reported_claim"
    from app.models.learner import LearningEvidence

    row = db.query(LearningEvidence).filter_by(id=body["learning_evidence_id"]).one()
    assert row.grader == GradingMode.SELF and row.execution_verification == V.SELF_REPORTED
    assert _state(db, bootstrap.user, concept.id).ladder == PRACTICED  # never DEMONSTRATED


def test_cited_foreign_or_missing_record_is_not_found(client, db, bootstrap, auth_headers):
    attempt_id, milestone = _project_attempt(client, db, bootstrap, auth_headers)
    resp = client.post(
        f"/academy/build-with-me/attempts/{attempt_id}/milestones/{milestone.id}/evidence",
        headers=auth_headers,
        json={"passed": True, "source_type": "evaluation_run", "source_id": str(uuid4())},
    )
    assert resp.status_code == 404


def test_derive_facts_uses_platform_records_only(db):
    from app.db.enums import AgentRunStatus
    from app.models.tasks import Task
    from app.services.project_evidence_verification import derive_candidate_facts
    from tests.conftest import make_agent_run, make_task_run

    user = make_user(db, email="derive@example.com")
    other = make_user(db, org=None, email="other@example.com")
    task_run = make_task_run(db)
    task = db.get(Task, task_run.task_id)
    task.created_by = user.id
    agent_run = make_agent_run(db, task_run=task_run, status=AgentRunStatus.COMPLETED)
    db.commit()

    mine = derive_candidate_facts(db, user.id, "agent_run", agent_run.id, claimed_passed=False)
    assert mine.passed is True and mine.execution_verification == V.PLATFORM_VERIFIED
    with pytest.raises(LookupError):
        derive_candidate_facts(db, other.id, "agent_run", agent_run.id, claimed_passed=True)
    claim = derive_candidate_facts(db, user.id, "manual", None, claimed_passed=True)
    assert claim.execution_verification == V.SELF_REPORTED


def test_finalized_submission_cannot_be_overwritten(client, db, bootstrap, auth_headers):
    from app.models.academy import AssessmentReadySubmission

    attempt_id, _milestone = _project_attempt(client, db, bootstrap, auth_headers)
    url = f"/academy/build-with-me/attempts/{attempt_id}/submit"
    first = client.post(url, headers=auth_headers)
    assert first.status_code == 200
    again = client.post(url, headers=auth_headers)
    assert again.status_code == 200  # resubmitting before finalization is unchanged 5B behaviour
    row = db.query(AssessmentReadySubmission).filter_by(project_attempt_id=attempt_id).one()
    row.finalized_at = datetime.now(timezone.utc)
    db.commit()
    assert client.post(url, headers=auth_headers).status_code == 409


# -- P0-3: shared AIL project privacy --------------------------------------------------------


def test_ail_system_records_are_private_to_their_creator(client, db, bootstrap, auth_headers):
    from app.api.deps import get_db  # noqa: F401
    from app.auth import get_current_user
    from app.db.enums import ArtifactType
    from app.main import app as fastapi_app
    from app.models.artifacts_eval import Artifact
    from app.models.identity import User
    from app.services.system_project_service import ensure_ail_system_project
    from tests.conftest import make_agent_run, make_agent_version, make_task, make_task_run

    owner = bootstrap.user
    project = ensure_ail_system_project(db, owner)
    task = make_task(db, project)
    task.created_by = owner.id
    task_run = make_task_run(db, task)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=make_agent_version(db))
    artifact = Artifact(agent_run_id=agent_run.id, type=ArtifactType.REPORT, storage_ref="nowhere.md")
    db.add(artifact)
    db.commit()

    # the creator can list and read
    assert any(t["id"] == task.id for t in client.get(f"/tasks?project_id={project.id}", headers=auth_headers).json())
    assert client.get(f"/agent-runs/{agent_run.id}", headers=auth_headers).status_code == 200

    # a second learner in the same org (also OWNER of the shared AIL project) cannot
    intruder = make_user(db, org=bootstrap.organization, email="intruder@example.com")
    ensure_ail_system_project(db, intruder)
    db.commit()
    fastapi_app.dependency_overrides[get_current_user] = lambda: db.get(User, intruder.id)
    try:
        assert client.get(f"/tasks/{task.id}", headers=auth_headers).status_code == 404
        assert client.get(f"/tasks/{task.id}/runs", headers=auth_headers).status_code == 404
        assert client.get(f"/agent-runs/{agent_run.id}", headers=auth_headers).status_code == 404
        assert client.get(f"/agent-runs/{agent_run.id}/artifacts", headers=auth_headers).status_code == 404
        assert client.get(f"/artifacts/{artifact.id}/content", headers=auth_headers).status_code == 404
        listed = client.get(f"/tasks?project_id={project.id}", headers=auth_headers).json()
        assert all(t["id"] != task.id for t in listed)
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


# -- dry-run diff report -------------------------------------------------------------------


def test_learner_state_diff_reports_the_regression_it_exists_to_show(db):
    from scripts.ail5c_learner_state_diff import diff

    user, concept, version = _user_concept(db)
    _lab(db, user, version, assistance=H.H3, verification=V.PLATFORM_VERIFIED, milestone=_milestone(db, user))
    rows = list(diff(db))
    assert rows == [(user.id, concept.id, ("demonstrated", []), ("practiced", []))]
