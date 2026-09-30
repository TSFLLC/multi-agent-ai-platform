"""AIL.5D.4 HTTP surface: step-scoped Professor status, ask, context preview, and the knowledge-check feedback loop."""

import json

import pytest

from app.auth import get_current_user
from app.main import app as fastapi_app
from app.models.academy import AcademyProfessorHelp
from app.models.concepts import Concept
from app.models.identity import User
from app.services.academy_level1_service import AcademyLevel1Service
from app.services.academy_structured_authoring import author_day_structure
from app.services.assessment_service import AssessmentService
from tests.ail1a_factories import make_user
from tests.ail5c_factories import kc_definition
from tests.test_ail5d3_day1 import _legacy_day1

L1 = "/academy/level-1"
THINK1 = "think-traditional-or-ai"
CHECK = "ai-or-not-ai"


@pytest.fixture()
def day1(db, bootstrap):
    _legacy_day1(db)
    author_day_structure(db, 1)
    return next(r for r in AcademyLevel1Service(db)._current_academy_items() if r.spec["day"] == 1)


def _status(client, headers, item, key):
    return client.get(f"{L1}/items/{item.id}/steps/{key}/professor", headers=headers)


def test_the_status_endpoint_tells_the_learner_what_the_professor_is_and_can_see(client, auth_headers, day1):
    body = _status(client, auth_headers, day1, THINK1).json()
    assert body["mode"] == "guided" and body["next_hint_level"] == "hint" and body["prior_hints"] == 0 and body["available"] is True
    assert "current step" in body["knows"] and "answers you have not earned yet" in body["cannot_see"]
    assert "instructions" not in json.dumps(body)
    teaching = _status(client, auth_headers, day1, "what-is-ai").json()
    assert teaching["mode"] == "teaching" and teaching["solution_eligible"] is True


def test_unknown_steps_and_unknown_items_are_404_and_stale_versions_are_409(client, auth_headers, day1, db):
    assert _status(client, auth_headers, day1, "no-such-step").status_code in (404, 409)
    assert client.get(f"{L1}/items/no-such-item/steps/x/professor", headers=auth_headers).status_code == 404
    from tests.ail5d1_factories import new_version

    v3 = new_version(db, day1, spec=json.loads(json.dumps(day1.spec)))
    stale = _status(client, auth_headers, day1, "what-is-ai")
    assert stale.status_code == 409 and stale.json()["error"]["detail"]["current_item_id"] == v3.id
    assert client.post(f"{L1}/items/{day1.id}/steps/what-is-ai/professor", json={}, headers=auth_headers).status_code == 409
    assert _status(client, auth_headers, v3, "what-is-ai").status_code == 200


@pytest.mark.parametrize("body", [
    {"help": "solution"}, {"help": "explain"}, {"help": "hint", "level": "explain"}, {"mode": "teaching"}, {"user_id": "someone-else"},
    {"question": "x" * 2001}, {"help": "hint", "reveal": True},
])
def test_a_client_cannot_choose_the_level_the_mode_or_another_learner(client, auth_headers, day1, body):
    response = client.post(f"{L1}/items/{day1.id}/steps/{THINK1}/professor", json=body, headers=auth_headers)
    assert response.status_code == 422


def test_requests_without_the_local_token_are_rejected(client, day1):
    assert client.get(f"{L1}/items/{day1.id}/steps/{THINK1}/professor").status_code == 401
    assert client.post(f"{L1}/items/{day1.id}/steps/{THINK1}/professor", json={}).status_code == 401


def test_an_open_assessment_pauses_the_step_professor_over_http(client, auth_headers, day1, db, bootstrap):
    definition = kc_definition(db, bootstrap.user, db.get(Concept, day1.concept_id), key="api-lock")
    AssessmentService(db).start(bootstrap.user, definition.definition_key)
    paused = _status(client, auth_headers, day1, THINK1).json()
    assert paused["available"] is False and paused["paused_reason"]
    response = client.post(f"{L1}/items/{day1.id}/steps/{THINK1}/professor", json={"help": "hint"}, headers=auth_headers)
    assert response.status_code == 409 and "AI Professor" in response.json()["error"]["message"]
    generic = client.post("/professor/interactions", headers=auth_headers, json={
        "intent": "EXPLAIN_THIS", "help": "hint", "target": {"type": "academy_step", "id": day1.id, "step_key": THINK1}})
    assert generic.status_code == 409           # the generic Professor endpoint is held by the same lock
    assert db.query(AcademyProfessorHelp).count() == 0


def test_the_context_preview_shows_the_step_context_and_enforces_the_target_shape(client, auth_headers, day1, bootstrap):
    ok = client.get("/professor/context-preview", headers=auth_headers, params={
        "intent": "EXPLAIN_THIS", "target_type": "academy_step", "target_id": day1.id, "step_key": THINK1, "help": "hint"})
    assert ok.status_code == 200
    context = ok.json()["context"]
    assert context["user_id"] == bootstrap.user.id and context["deterministic_facts"]["assistance"]["level"] == "hint"
    assert "You want auditability" not in json.dumps(context)
    missing_step = client.get("/professor/context-preview", headers=auth_headers, params={
        "intent": "EXPLAIN_THIS", "target_type": "academy_step", "target_id": day1.id})
    assert missing_step.status_code == 409 and "step_key" in missing_step.json()["error"]["message"]
    wrong_intent = client.get("/professor/context-preview", headers=auth_headers, params={
        "intent": "WHY_DOES_THIS_MATTER", "target_type": "academy_step", "target_id": day1.id, "step_key": THINK1})
    assert wrong_intent.status_code in (404, 409)


def test_the_preview_never_writes_help_or_changes_the_ladder(client, auth_headers, day1, db):
    for _ in range(3):
        client.get("/professor/context-preview", headers=auth_headers, params={
            "intent": "EXPLAIN_THIS", "target_type": "academy_step", "target_id": day1.id, "step_key": THINK1, "help": "hint"})
    assert db.query(AcademyProfessorHelp).count() == 0
    assert _status(client, auth_headers, day1, THINK1).json()["next_hint_level"] == "hint"


def test_another_learner_sees_their_own_ladder_over_http(client, auth_headers, day1, db):
    from app.academy_professor import HelpLevel, HelpRequest, ProfessorMode

    other = make_user(db, org=None, email="second@example.com")
    db.commit()
    from app.academy_steps import public_steps

    fingerprint = next(s["fingerprint"] for s in public_steps(day1.spec) if s["key"] == THINK1)
    for i in range(2):
        db.add(AcademyProfessorHelp(user_id=other.id, learning_item_id=day1.id, lineage_id=day1.lineage_id, step_key=THINK1, step_fingerprint=fingerprint,
                                    mode=ProfessorMode.GUIDED, request_kind=HelpRequest.HINT, help_level=HelpLevel.HINT, interaction_id=f"{i}" * 36, context_sha256="0" * 64))
    db.commit()
    assert _status(client, auth_headers, day1, THINK1).json()["prior_hints"] == 0             # the owner has asked for none
    fastapi_app.dependency_overrides[get_current_user] = lambda: db.get(User, other.id)
    assert _status(client, auth_headers, day1, THINK1).json()["prior_hints"] == 2
    assert _status(client, auth_headers, day1, THINK1).json()["next_hint_level"] == "stronger_hint"   # rung 3 after two hints (capped: not eligible)
    fastapi_app.dependency_overrides.pop(get_current_user, None)


# -- knowledge-check retry loop over HTTP ---------------------------------------------------------------------------------------------------------------------


def test_the_day_1_knowledge_check_loop_feedback_retry_success_complete(client, auth_headers, day1, db):
    answers = {q["id"]: q["answer"] for q in day1.spec["knowledge_check"]}
    wrong = {k: {"system": "ai", "claim": "realistic"} for k in answers}
    step_url = f"{L1}/items/{day1.id}/steps/{CHECK}/complete"
    first = client.post(f"{L1}/items/{day1.id}/knowledge-check", json={"answers": wrong}, headers=auth_headers).json()
    assert first["passed"] is False and first["feedback"]["retry_allowed"] is True and first["feedback"]["total"] == 8
    assert 0 < first["feedback"]["incorrect"] <= 8 and all("explanation" not in r for r in first["results"])
    blob = json.dumps(first)
    assert "answer" not in first and "timer" not in blob and "arithmetic" not in blob        # no answers, no rationale, yet
    assert client.post(step_url, headers=auth_headers).status_code == 409
    mixed = dict(wrong)
    mixed[next(iter(answers))] = next(iter(answers.values()))                                # one right, rest still wrong
    second = client.post(f"{L1}/items/{day1.id}/knowledge-check", json={"answers": mixed}, headers=auth_headers).json()
    assert second["feedback"]["attempt"] == 2 and second["feedback"]["incorrect"] < first["feedback"]["incorrect"] + 1
    assert client.post(step_url, headers=auth_headers).status_code == 409
    done = client.post(f"{L1}/items/{day1.id}/knowledge-check", json={"answers": answers}, headers=auth_headers).json()
    assert done["passed"] is True and done["feedback"] is None and "timer" in done["results"][0]["explanation"]
    assert client.post(step_url, headers=auth_headers).status_code == 200
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    check = next(s for s in view["steps"] if s["key"] == CHECK)
    assert check["status"] == "completed" and check["check_result"]["attempts"] == 3 and check["check_result"]["ever_passed"] is True
    assert view["demonstrated"] is False and view["learning_complete"] is False       # other required steps remain; never demonstrated by a check alone
