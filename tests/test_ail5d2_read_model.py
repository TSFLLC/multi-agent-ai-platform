"""AIL.5D.2 — the step-aware learner read model and the structured-Day ``/open`` correction.

Opening is opening. A structured Day can only become learning-complete by its required steps actually completing, and
only real evidence (never a view) can make it demonstrated. Legacy (non-structured) Days behave exactly as before.
"""

import json

import pytest

from app.auth import get_current_user
from app.db.enums import EvidenceType, GradingMode
from app.main import app as fastapi_app
from app.models.identity import User
from app.models.learner import LearningEvidence
from app.services.academy_level1_service import AcademyLevel1Service
from app.services.academy_step_service import AcademyStepService, VerifiedCompletion
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_state_service import LearnerStateService
from app.services.learning_evidence_service import LearningEvidenceService
from tests.ail1a_factories import make_user
from tests.ail5d1_factories import CLAIM_WHY, REVEAL, make_legacy_item, make_structured_item, new_version, sample_spec

LEVEL1 = "/academy/level-1"
REQUIRED = ["what-is-ai", "rules-vs-patterns", "bp-reminder", "three-claims", "ai-or-not", "lab-run"]
ALL_KEYS = ["baseline", "what-is-ai", "rules-vs-patterns", "bp-reminder", "three-claims", "ai-or-not", "explain-ai", "compare-answers", "lab-run"]
KC_REQUIREMENT = {"requires_all": [{"evidence_type": "knowledge_check", "grader_in": ["deterministic"]}]}


@pytest.fixture()
def item(db, bootstrap):
    return make_structured_item(db)


def _evidence(db, user_id):
    return db.query(LearningEvidence).filter(LearningEvidence.user_id == user_id).all()


def _done(db, user, item_id, key):
    AcademyStepService(db).complete_step(user.id, item_id, key, verified=VerifiedCompletion(kind="test"))


def _view(client, headers, day=1, **params):
    response = client.get(f"{LEVEL1}/days/{day}/learning", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _as(db, user):
    fastapi_app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)


# -- the ordered public projection -------------------------------------------------------------------------------------------


def test_the_read_model_projects_ordered_public_steps_with_stable_keys(client, auth_headers, item):
    body = _view(client, auth_headers)
    assert body["structured"] is True and body["version"] == 1 and body["day"] == 1
    assert [s["key"] for s in body["steps"]] == ALL_KEYS
    assert [s["position"] for s in body["steps"]] == list(range(1, 10))
    by_key = {s["key"]: s for s in body["steps"]}
    assert by_key["what-is-ai"]["type"] == "teach" and by_key["ai-or-not"]["type"] == "check" and by_key["lab-run"]["type"] == "lab"
    assert by_key["baseline"]["required"] is False and by_key["what-is-ai"]["required"] is True
    assert by_key["what-is-ai"]["title"] == "What exactly is AI?" and by_key["what-is-ai"]["content"]["blocks"]
    assert by_key["ai-or-not"]["binding"] == {"kind": "item_knowledge_check"}


def test_private_content_and_internals_never_reach_the_read_model(client, auth_headers, item):
    body = _view(client, auth_headers)
    blob = json.dumps(body)
    for secret in (REVEAL, CLAIM_WHY, "Correct answer: X.", "reveal_md", "\"claims\"", "authored_check_md", "fingerprint"):
        assert secret not in blob, secret
    assert all("private" not in s for s in body["steps"])


def test_the_read_model_does_not_accept_client_supplied_progress(client, auth_headers, item):
    body = _view(client, auth_headers, learning_complete="true", demonstrated="true", status="completed", current_step_key="lab-run")
    assert body["learning_complete"] is False and body["demonstrated"] is False
    assert {s["status"] for s in body["steps"]} == {"not_started"}
    assert body["current_step_key"] == "baseline"


# -- current / next derivation, required vs optional --------------------------------------------------------------------------


def test_a_fresh_learner_starts_at_the_first_step_with_nothing_complete(db, item, bootstrap):
    view = AcademyStepService(db).learning_view(bootstrap.user.id, item.id)
    assert view["current_step_key"] == "baseline" and view["next_step_key"] == "what-is-ai"
    assert [s["is_current"] for s in view["steps"]].count(True) == 1 and [s["is_next"] for s in view["steps"]].count(True) == 1
    assert (view["required_completed"], view["required_total"], view["optional_total"]) == (0, 6, 3)
    assert view["learning_complete"] is False and view["demonstrated"] is False


def test_current_and_next_follow_authored_order_and_skip_past_completed_or_skipped_steps(db, item, bootstrap):
    svc, user = AcademyStepService(db), bootstrap.user
    svc.skip_step(user.id, item.id, "baseline")                      # optional: skipped, not completed
    assert svc.learning_view(user.id, item.id)["current_step_key"] == "what-is-ai"
    svc.complete_step(user.id, item.id, "what-is-ai")                # self-completable: Continue
    view = svc.learning_view(user.id, item.id)
    assert (view["current_step_key"], view["next_step_key"]) == ("rules-vs-patterns", "bp-reminder")
    svc.open_step(user.id, item.id, "rules-vs-patterns")             # opened stays current; it is not progress
    assert svc.learning_view(user.id, item.id)["current_step_key"] == "rules-vs-patterns"
    statuses = {s["key"]: s["status"] for s in svc.learning_view(user.id, item.id)["steps"]}
    assert statuses["baseline"] == "skipped" and statuses["what-is-ai"] == "completed" and statuses["rules-vs-patterns"] == "opened"


def test_there_is_no_current_step_once_everything_is_resolved(db, item, bootstrap):
    user = bootstrap.user
    for key in ALL_KEYS:
        if key in ("baseline", "explain-ai", "compare-answers"):
            AcademyStepService(db).skip_step(user.id, item.id, key)
        else:
            _done(db, user, item.id, key)
    view = AcademyStepService(db).learning_view(user.id, item.id)
    assert view["current_step_key"] is None and view["next_step_key"] is None and view["learning_complete"] is True


def test_learning_complete_needs_required_steps_only_and_leaves_optional_steps_navigable(db, item, bootstrap):
    user = bootstrap.user
    for key in REQUIRED:
        _done(db, user, item.id, key)
    view = AcademyStepService(db).learning_view(user.id, item.id)
    assert view["learning_complete"] is True and (view["required_completed"], view["optional_completed"]) == (6, 0)
    assert view["current_step_key"] == "baseline"  # an unfinished optional step is still where the learner is
    assert view["demonstrated"] is False and "not demonstrated knowledge" in view["note"]


# -- exact LearningItem version semantics -------------------------------------------------------------------------------------


def test_the_read_model_is_of_the_current_version_and_a_stale_item_is_refused(db, client, auth_headers, item, bootstrap):
    _done(db, bootstrap.user, item.id, "what-is-ai")
    spec2 = sample_spec()
    next(s for s in spec2["steps"] if s["key"] == "rules-vs-patterns")["content"]["lead_md"] = "Revised."
    v2 = new_version(db, item, spec=spec2)
    body = _view(client, auth_headers)
    assert body["version"] == 2 and body["item_id"] == v2.id and body["lineage_id"] == item.lineage_id
    by_key = {s["key"]: s for s in body["steps"]}
    assert by_key["what-is-ai"]["status"] == "completed" and by_key["what-is-ai"]["carried_from_version"] == 1
    assert by_key["rules-vs-patterns"]["status"] == "not_started"
    stale = client.get(f"{LEVEL1}/items/{item.id}/steps", headers=auth_headers)
    assert stale.status_code == 409 and stale.json()["error"]["detail"]["current_item_id"] == v2.id
    with pytest.raises(Exception) as err:
        AcademyStepService(db).learning_view(bootstrap.user.id, item.id)
    assert "updated" in str(err.value)


def test_progress_is_isolated_between_learners_in_the_read_model(db, client, auth_headers, item, bootstrap):
    other = make_user(db, org=None, email="second@example.com")
    db.commit()
    _done(db, bootstrap.user, item.id, "what-is-ai")
    assert AcademyStepService(db).learning_view(other.id, item.id)["required_completed"] == 0
    _as(db, other)
    assert _view(client, auth_headers)["required_completed"] == 0
    fastapi_app.dependency_overrides.pop(get_current_user, None)
    assert _view(client, auth_headers)["required_completed"] == 1


# -- /open: before vs after ---------------------------------------------------------------------------------------------------


def test_opening_a_structured_day_writes_no_evidence_and_changes_no_state(db, client, auth_headers, item, bootstrap):
    before = len(_evidence(db, bootstrap.user.id))
    for _ in range(3):
        response = client.post(f"{LEVEL1}/items/{item.id}/open", headers=auth_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["opened"] is True and body["evidence_recorded"] is False and body["evidence_id"] is None
        assert body["learner_state"] == "not_started" and body["passed"] is None
    assert len(_evidence(db, bootstrap.user.id)) == before == 0
    view = _view(client, auth_headers)
    assert view["learning_complete"] is False and view["demonstrated"] is False
    day = next(d for d in client.get(f"{LEVEL1}/days", headers=auth_headers).json() if d["day"] == 1)
    assert day["evidence_earned"] is False and day["learning_complete"] is False and day["state"] == "not_started"


def test_viewing_everything_cannot_complete_or_demonstrate_a_structured_day(db, item, bootstrap):
    svc, user = AcademyStepService(db), bootstrap.user
    for key in ALL_KEYS:
        svc.open_step(user.id, item.id, key)
    AcademyLevel1Service(db).expose(user.id, item.id)
    view = svc.learning_view(user.id, item.id)
    assert view["required_completed"] == 0 and view["learning_complete"] is False and view["demonstrated"] is False
    assert {s["status"] for s in view["steps"]} == {"opened"}
    assert _evidence(db, user.id) == []


def test_legacy_open_behaviour_is_unchanged(db, client, auth_headers, bootstrap):
    legacy = make_legacy_item(db)
    response = client.post(f"{LEVEL1}/items/{legacy.id}/open", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["evidence_id"] and body["passed"] is True and body["learner_state"] == "exposed"
    assert body.get("evidence_recorded") is None  # the legacy response shape is untouched
    rows = _evidence(db, bootstrap.user.id)
    assert [(r.evidence_type, r.grader, r.passed) for r in rows] == [(EvidenceType.LESSON_COMPLETED, GradingMode.SELF, True)]
    again = client.post(f"{LEVEL1}/items/{legacy.id}/open", headers=auth_headers).json()
    assert again["evidence_id"] == body["evidence_id"] and len(_evidence(db, bootstrap.user.id)) == 1
    day = next(d for d in client.get(f"{LEVEL1}/days", headers=auth_headers).json() if d["day"] == 2)
    assert day["evidence_earned"] is True and day["structured"] is False and day["learning_complete"] is None


def test_a_legacy_day_reads_as_not_structured_and_the_old_endpoints_still_work(db, client, auth_headers, bootstrap):
    legacy = make_legacy_item(db)
    body = _view(client, auth_headers, day=2)
    assert body["structured"] is False and body["steps"] == [] and body["learning_complete"] is None and body["item_id"] == legacy.id
    assert client.get(f"{LEVEL1}/days/2", headers=auth_headers).status_code == 200
    assert client.get(f"{LEVEL1}/items/{legacy.id}/steps", headers=auth_headers).status_code == 404
    assert client.get(f"{LEVEL1}/days/9/learning", headers=auth_headers).status_code == 404


# -- lesson_completed is earned, once -----------------------------------------------------------------------------------------


def test_lesson_completed_is_written_exactly_once_and_only_when_the_last_required_step_completes(db, item, bootstrap):
    user = bootstrap.user
    for key in REQUIRED[:-1]:
        _done(db, user, item.id, key)
    assert _evidence(db, user.id) == []  # five of six required steps: nothing yet
    AcademyStepService(db).skip_step(user.id, item.id, "baseline")  # skipping never counts
    assert _evidence(db, user.id) == []
    _done(db, user, item.id, REQUIRED[-1])
    rows = _evidence(db, user.id)
    assert [(r.evidence_type, r.learning_item_id, r.passed) for r in rows] == [(EvidenceType.LESSON_COMPLETED, item.id, True)]
    _done(db, user, item.id, "explain-ai")  # further completion writes nothing more
    _done(db, user, item.id, REQUIRED[-1])
    assert len(_evidence(db, user.id)) == 1
    view = AcademyStepService(db).learning_view(user.id, item.id)
    assert view["learning_complete"] is True and view["demonstrated"] is False and view["concept_state"] == "exposed"


def test_lesson_completed_is_not_repeated_on_a_later_version_of_the_same_lineage(db, item, bootstrap):
    user = bootstrap.user
    for key in REQUIRED:
        _done(db, user, item.id, key)
    spec2 = sample_spec()
    next(s for s in spec2["steps"] if s["key"] == "bp-reminder")["content"]["question_md"] = "A reworded question?"
    v2 = new_version(db, item, spec=spec2)
    _done(db, user, v2.id, "bp-reminder")
    assert AcademyStepService(db).learning_view(user.id, v2.id)["learning_complete"] is True
    assert len([e for e in _evidence(db, user.id) if e.evidence_type == EvidenceType.LESSON_COMPLETED]) == 1


def test_lesson_completed_is_per_learner(db, item, bootstrap):
    other = make_user(db, org=None, email="third@example.com")
    db.commit()
    for key in REQUIRED:
        _done(db, bootstrap.user, item.id, key)
    assert len(_evidence(db, bootstrap.user.id)) == 1 and _evidence(db, other.id) == []


# -- demonstrated comes only from canonical evidence ----------------------------------------------------------------------------


def test_a_structured_day_is_demonstrated_only_by_real_evidence_and_evidence_earned_ignores_lesson_completed(db, client, auth_headers, bootstrap):
    item = make_structured_item(db, slug="demo-concept", evidence_requirements=KC_REQUIREMENT)
    user = bootstrap.user
    for key in REQUIRED:
        _done(db, user, item.id, key)
    day = next(d for d in client.get(f"{LEVEL1}/days", headers=auth_headers).json() if d["day"] == 1)
    assert day["learning_complete"] is True and day["demonstrated"] is False and day["evidence_earned"] is False
    version = ConceptGraphService(db).get_current_version(item.concept_id)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id, concept_id=item.concept_id, concept_version_id=version.id, learning_item_id=item.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK, grader=GradingMode.DETERMINISTIC, passed=True,
    )
    assert LearnerStateService(db).state(user.id, item.concept_id).ladder == "demonstrated"
    day = next(d for d in client.get(f"{LEVEL1}/days", headers=auth_headers).json() if d["day"] == 1)
    assert day["demonstrated"] is True and day["evidence_earned"] is True
    assert _view(client, auth_headers)["demonstrated"] is True


# -- the Professor is unaffected ----------------------------------------------------------------------------------------------------


def test_the_professor_context_is_unaffected_by_step_progress_and_learning_complete(db, item, bootstrap):
    from app.schemas.professor import ProfessorContextRequest, ProfessorIntent, ProfessorTarget, ProfessorTargetType
    from app.services.professor_context_service import ProfessorContextAssembler

    for key in REQUIRED:
        _done(db, bootstrap.user, item.id, key)
    context = ProfessorContextAssembler(db).assemble(
        bootstrap.user.id,
        ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=item.concept_id)),
    )
    blob = json.dumps([r.model_dump(mode="json") for r in context.records], default=str) + json.dumps(context.deterministic_facts, default=str)
    for leaked in (REVEAL, CLAIM_WHY, "reveal_md", "steps", "step_key", "current_step_key", "academy_step_progress"):
        assert leaked not in blob, leaked
    assert context.deterministic_facts["learner_state"]["ladder"] == "exposed"  # canonical state, unchanged semantics
