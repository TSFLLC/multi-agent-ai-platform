"""AIL.5C HTTP surface, cross-user isolation and Professor coaching after assessment."""

import pytest

from app.auth import get_current_user
from app.main import app as fastapi_app
from app.models.identity import User
from tests.ail5c_factories import kc_definition, make_ail_concept, second_user

BASE = "/academy/assessments"


def _as(db, user):
    fastapi_app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)


def _as_local():
    fastapi_app.dependency_overrides.pop(get_current_user, None)


def _answers(attempt_json, choice=1):
    return {"responses": {i["entry_key"]: {"selected": [choice]} for i in attempt_json["challenge"]["items"]}}


def _journey(client, headers, key, *, choice=1, declaration="no_external_help"):
    started = client.post(f"{BASE}/attempts", json={"definition_key": key}, headers=headers)
    assert started.status_code == 201, started.text
    attempt = started.json()
    saved = client.put(f"{BASE}/attempts/{attempt['id']}/draft", json={"draft": _answers(attempt, choice)}, headers=headers)
    assert saved.status_code == 200
    submitted = client.post(f"{BASE}/attempts/{attempt['id']}/submit", json={"attestation": {"declaration": declaration}}, headers=headers)
    assert submitted.status_code == 200, submitted.text
    return attempt, submitted.json()


@pytest.fixture()
def kc(db, bootstrap):
    concept, version = make_ail_concept(db, core=True)
    return concept, kc_definition(db, bootstrap.user, concept)


# -- the learner journey -----------------------------------------------------------------------------------


def test_center_is_empty_then_offers_a_ready_assessment(client, db, bootstrap, auth_headers):
    empty = client.get(f"{BASE}/center", headers=auth_headers).json()
    assert empty["ready_for_assessment"] == empty["in_progress"] == empty["history"] == empty["demonstrated"] == []
    concept, _v = make_ail_concept(db)
    kc_definition(db, bootstrap.user, concept)
    center = client.get(f"{BASE}/center", headers=auth_headers).json()
    assert [r["definition_key"] for r in center["ready_for_assessment"]] == ["kc-structured-output"]
    assert center["ready_for_assessment"][0]["why_offered"]


def test_definition_detail_says_what_is_assessed_without_leaking_the_answers(client, kc, auth_headers):
    _c, defn = kc
    detail = client.get(f"{BASE}/definitions/{defn.definition_key}", headers=auth_headers).json()
    assert detail["mentor_locked_during_attempt"] is True and detail["allowed_resources"] == ["Your lessons"]
    assert detail["criteria"][0]["decided_by"] == "the platform" and detail["time_limit_hours"] == 24
    assert detail["evidence_collected"]
    assert set(detail["criteria"][0]) == {"key", "label", "description", "required", "decided_by", "anchors"}
    assert not {"challenge_spec", "grading_policy", "independence_policy"} & set(detail)
    assert "answer_key" not in str(detail) and "reference_points" not in str(detail)
    assert client.get(f"{BASE}/definitions/none", headers=auth_headers).status_code == 404


def test_full_journey_pass_result_record_and_center(client, db, bootstrap, kc, auth_headers):
    concept, defn = kc
    attempt, result = _journey(client, auth_headers, defn.definition_key)
    assert "answer_key" not in str(attempt) and attempt["mentor_locked"] is True and attempt["status"] == "draft"
    assert result["finalized"] and result["result"]["outcome"] == "passed"
    assert result["result"]["demonstration_effect"] == "counts_toward_demonstrated" and result["result"]["has_record"]
    report = result["result"]["report"]
    assert report["platform_fact"]["deterministic_checks"][0]["finding"] == "met"
    assert report["answers"]["learner_state"][concept.id]["after"] == "demonstrated"

    fetched = client.get(f"{BASE}/attempts/{attempt['id']}/result", headers=auth_headers).json()
    assert fetched["result"]["id"] == result["result"]["id"] and fetched["attempt"]["mentor_locked"] is False

    records = client.get(f"{BASE}/records", headers=auth_headers).json()
    assert len(records) == 1 and records[0]["status"] == "valid" and records[0]["concepts"] == ["Structured Output"]
    detail = client.get(f"{BASE}/records/{records[0]['result_id']}", headers=auth_headers).json()
    assert detail["record"]["title"] == "Demonstration Record" and detail["record_hash"] == records[0]["record_hash"]
    md = client.get(f"{BASE}/records/{records[0]['result_id']}?format=md", headers=auth_headers)
    assert md.status_code == 200 and md.text.startswith("# Demonstration Record") and "Not a certificate or credential." in md.text

    center = client.get(f"{BASE}/center", headers=auth_headers).json()
    assert len(center["demonstrated"]) == 1 and len(center["history"]) == 1 and center["in_progress"] == []


def test_needs_work_shows_the_gap_a_remediation_step_and_the_failed_attempt(client, kc, auth_headers):
    _c, defn = kc
    attempt, result = _journey(client, auth_headers, defn.definition_key, choice=0)
    assert result["result"]["outcome"] == "needs_work"
    assert result["result"]["gaps"][0]["label"] == "You answered the questions correctly"
    assert {s["kind"] for s in result["result"]["remediation"]} >= {"learning_item", "retry"}
    center = client.get(f"{BASE}/center", headers=auth_headers).json()
    assert center["needs_work"][0]["attempt_id"] == attempt["id"] and center["needs_work"][0]["remediation"]
    assert center["history"][0]["outcome"] == "needs_work"
    again = client.post(f"{BASE}/attempts", json={"definition_key": defn.definition_key, "previous_attempt_id": attempt["id"]}, headers=auth_headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "assessment_not_ready"
    assert any(c["key"] == "cooldown" for c in again.json()["error"]["detail"]["checks"])


def test_start_is_idempotent_with_the_idempotency_key(client, kc, auth_headers):
    _c, defn = kc
    headers = {**auth_headers, "Idempotency-Key": "abc-123"}
    first = client.post(f"{BASE}/attempts", json={"definition_key": defn.definition_key}, headers=headers).json()
    second = client.post(f"{BASE}/attempts", json={"definition_key": defn.definition_key}, headers=headers).json()
    assert first["id"] == second["id"]


def test_leaving_an_assessment_releases_the_lock(client, kc, auth_headers):
    _c, defn = kc
    attempt = client.post(f"{BASE}/attempts", json={"definition_key": defn.definition_key}, headers=auth_headers).json()
    left = client.post(f"{BASE}/attempts/{attempt['id']}/abandon", headers=auth_headers).json()
    assert left["status"] == "abandoned" and left["mentor_locked"] is False


def test_the_client_can_never_set_an_outcome_or_state(client, kc, auth_headers):
    _c, defn = kc
    attempt = client.post(f"{BASE}/attempts", json={"definition_key": defn.definition_key}, headers=auth_headers).json()
    for body in ({"attestation": {"declaration": "no_external_help"}, "outcome": "passed"},
                 {"attestation": {"declaration": "no_external_help"}, "learner_state": "demonstrated"}):
        assert client.post(f"{BASE}/attempts/{attempt['id']}/submit", json=body, headers=auth_headers).status_code == 422
    assert client.post(f"{BASE}/attempts", json={"definition_key": defn.definition_key, "status": "finalized"}, headers=auth_headers).status_code == 422
    routes = {(r.path, tuple(sorted(r.methods))) for r in fastapi_app.routes if r.path.startswith(BASE)}
    assert not any("evidence" in path or "state" in path or "outcome" in path for path, _ in routes)


def test_a_foreign_budget_cannot_be_billed_for_grading(client, kc, auth_headers):
    _c, defn = kc
    attempt = client.post(f"{BASE}/attempts", json={"definition_key": defn.definition_key}, headers=auth_headers).json()
    resp = client.post(f"{BASE}/attempts/{attempt['id']}/grade", json={"budget_id": "no-such-budget"}, headers=auth_headers)
    assert resp.status_code == 403


# -- privacy over HTTP ---------------------------------------------------------------------------------------------


def test_another_learner_cannot_retrieve_an_attempt_result_record_or_review(client, db, bootstrap, kc, auth_headers):
    concept, defn = kc
    attempt, result = _journey(client, auth_headers, defn.definition_key)
    result_id = result["result"]["id"]
    review = client.post(f"{BASE}/attempts/{attempt['id']}/review", json={"reason": "I disagree with this result.", "consent": True}, headers=auth_headers)
    assert review.status_code == 201
    other = second_user(db, bootstrap.organization)
    _as(db, other)
    try:
        for method, path in (
            ("get", f"/attempts/{attempt['id']}"), ("get", f"/attempts/{attempt['id']}/result"),
            ("put", f"/attempts/{attempt['id']}/draft"), ("post", f"/attempts/{attempt['id']}/abandon"),
            ("get", f"/records/{result_id}"), ("post", f"/reviews/{review.json()['id']}/withdraw"),
            ("post", f"/reviews/{review.json()['id']}/consent"),
        ):
            kwargs = {"json": {"draft": {}}} if method == "put" else {}
            response = getattr(client, method)(BASE + path, headers=auth_headers, **kwargs)
            assert response.status_code == 404, (method, path, response.status_code)
        assert client.get(f"{BASE}/records", headers=auth_headers).json() == []
        center = client.get(f"{BASE}/center", headers=auth_headers).json()
        assert center["history"] == [] and center["demonstrated"] == [] and center["needs_work"] == []
        assert client.get(f"{BASE}/reviews", headers=auth_headers).status_code == 403  # a member is not a reviewer
    finally:
        _as_local()


def test_authoring_requires_an_owner_or_admin(client, db, bootstrap, auth_headers):
    concept, _v = make_ail_concept(db)
    body = {
        "definition_key": "api-authored", "kind": "knowledge_check", "title": "API authored", "instructions_md": "Answer.",
        "criteria": [{"key": "ok", "label": "Correct", "method": "deterministic", "required": True, "check": {"type": "choice_match"}}],
        "concept_links": [{"concept_id": concept.id, "criterion_keys": ["ok"]}],
        "challenge_spec": {"entry_kind": "choice", "draw_size": 1, "pool": [
            {"entry_key": f"q{i}", "prompt": "Q?", "options": ["a", "b"], "answer_key": [0]} for i in range(3)]},
    }
    member = second_user(db, bootstrap.organization)
    _as(db, member)
    try:
        assert client.post(f"{BASE}/definitions", json=body, headers=auth_headers).status_code == 403
    finally:
        _as_local()
    created = client.post(f"{BASE}/definitions", json=body, headers=auth_headers)
    assert created.status_code == 201
    published = client.post(f"{BASE}/definitions/{created.json()['id']}/publish", headers=auth_headers)
    assert published.status_code == 200 and published.json()["status"] == "published" and published.json()["content_hash"]
    again = client.post(f"{BASE}/definitions/{created.json()['id']}/publish", headers=auth_headers)
    assert again.status_code == 409  # a published definition is immutable


# -- reviewer endpoints -------------------------------------------------------------------------------------------------


def test_review_over_http_consent_gated_and_audited(client, db, bootstrap, kc, auth_headers):
    concept, defn = kc
    learner = second_user(db, bootstrap.organization)
    _as(db, learner)
    try:
        attempt, _result = _journey(client, auth_headers, defn.definition_key, choice=0)
        review = client.post(f"{BASE}/attempts/{attempt['id']}/review", json={"reason": "The second question was ambiguous.", "consent": True}, headers=auth_headers).json()
    finally:
        _as_local()
    queue = client.get(f"{BASE}/reviews", headers=auth_headers).json()
    assert [r["id"] for r in queue] == [review["id"]] and "reason_text" not in queue[0]
    detail = client.get(f"{BASE}/reviews/{review['id']}", headers=auth_headers).json()
    assert detail["consented"] and detail["submission"] and "new_assessment" in detail["allowed_decisions"]
    bad = client.post(f"{BASE}/reviews/{review['id']}/decision", json={"decision": "override_pass", "rationale": "Let them pass, please."}, headers=auth_headers)
    assert bad.status_code == 409  # a platform fact cannot be overridden
    ok = client.post(f"{BASE}/reviews/{review['id']}/decision", json={"decision": "confirm", "rationale": "The question was fine."}, headers=auth_headers)
    assert ok.status_code == 200 and ok.json()["status"] == "resolved"
    _as(db, learner)
    try:
        view = client.get(f"{BASE}/attempts/{attempt['id']}/result", headers=auth_headers).json()
    finally:
        _as_local()
    assert view["reviews"][0]["decision"] == "confirm" and len(view["history"]) >= 3
    assert view["result"]["kind"] == "human" and view["result"]["outcome"] == "needs_work"


# -- Professor coaching AFTER assessment -----------------------------------------------------------------------------------


def _professor_context(db, user, result_id):
    from app.schemas.professor import ProfessorContextRequest, ProfessorIntent, ProfessorTarget, ProfessorTargetType
    from app.services.professor_context_service import ProfessorContextAssembler

    request = ProfessorContextRequest(
        intent=ProfessorIntent.HELP_ME_AFTER_ASSESSMENT,
        target=ProfessorTarget(type=ProfessorTargetType.ASSESSMENT_RESULT, id=result_id),
    )
    return ProfessorContextAssembler(db).assemble(user.id, request)


def test_professor_sees_the_results_facts_and_next_steps_but_never_the_submission(client, db, bootstrap, kc, auth_headers):
    _c, defn = kc
    _attempt, result = _journey(client, auth_headers, defn.definition_key, choice=0)
    context = _professor_context(db, bootstrap.user, result["result"]["id"])
    roles = {r.role for r in context.records}
    assert {"assessment_result", "assessment_platform_fact", "assessment_remediation"} <= roles
    assert context.deterministic_facts["assessment_outcome"] == "needs_work"
    assert "selected" not in str([r.data for r in context.records])  # no raw submission


def test_professor_cannot_read_another_learners_result(client, db, bootstrap, kc, auth_headers):
    from app.services.professor_context_service import ProfessorContextError

    _c, defn = kc
    _attempt, result = _journey(client, auth_headers, defn.definition_key)
    other = second_user(db, bootstrap.organization)
    with pytest.raises(ProfessorContextError):
        _professor_context(db, other, result["result"]["id"])


def test_professor_coaching_cannot_regrade_or_contradict_the_outcome(client, db, bootstrap, kc, auth_headers):
    from app.schemas.professor import ProfessorAssertionKind, ProfessorResponse
    from app.services.professor_contract import ProfessorResponseValidationError, validate_professor_response

    _c, defn = kc
    _attempt, result = _journey(client, auth_headers, defn.definition_key, choice=0)
    context = _professor_context(db, bootstrap.user, result["result"]["id"])

    def response(text):
        return ProfessorResponse.model_validate({
            "intent": "HELP_ME_AFTER_ASSESSMENT", "direct_answer": text, "explanation": "Advisory explanation.",
            "evidence": [], "uncertainties": [], "suggested_next_actions": [], "attachment_references": [],
            "grounded_assertions": [{"assertion_kind": ProfessorAssertionKind.AI_EXPLANATION.value, "text": text, "references": []}],
        })

    assert validate_professor_response(response("Let us revisit the lesson on structured output together."), context)
    for bad in ("I will regrade this for you.", "You have passed the assessment.", "This now counts as evidence for you.",
                "I have graded your answers again.", "I will change your result to a pass."):
        with pytest.raises(ProfessorResponseValidationError):
            validate_professor_response(response(bad), context)


def test_professor_target_options_and_intent_requirements(client, db, bootstrap, kc, auth_headers):
    from app.schemas.professor import ProfessorContextRequest, ProfessorIntent
    from app.services.professor_context_service import ProfessorContextAssembler, ProfessorContextError
    from app.services.professor_execution_service import ProfessorExecutionService

    _c, defn = kc
    _journey(client, auth_headers, defn.definition_key, choice=0)
    options = ProfessorExecutionService(db).target_options(bootstrap.user, ProfessorIntent.HELP_ME_AFTER_ASSESSMENT)
    assert len(options.options) >= 1 and options.options[0].type.value == "assessment_result"
    with pytest.raises(ProfessorContextError):
        ProfessorContextAssembler(db).assemble(bootstrap.user.id, ProfessorContextRequest(intent=ProfessorIntent.HELP_ME_AFTER_ASSESSMENT))
