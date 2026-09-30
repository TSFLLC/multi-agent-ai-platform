"""AIL.5D.3 — durable learner responses and server-verified interactive completion.

The browser can never say "completed": an interactive step completes only when the server finds its own interaction in
persisted records (a valid saved response, a committed answer, or the canonical knowledge-check evidence).
"""

import json

import pytest
from sqlalchemy.exc import IntegrityError

from app.auth import get_current_user
from app.db.enums import AcademyStepResponseKind, EvidenceType
from app.main import app as fastapi_app
from app.models.academy import AcademyStepResponse
from app.models.identity import User
from app.models.learner import LearningEvidence
from app.services.academy_step_service import AcademyStepService, VerifiedCompletion
from tests.ail1a_factories import make_user
from tests.ail5d1_factories import CLAIM_WHY, REVEAL, make_structured_item, new_version, sample_spec, sample_steps

L1 = "/academy/level-1"


def _lecture_spec(**over):
    """The sample Day without its lab step, so every required step can complete through the server workflow."""
    return sample_spec(kind="lecture", steps=[s for s in sample_steps() if s["type"] != "lab"], **over)


@pytest.fixture()
def item(db, bootstrap):
    return make_structured_item(db, spec=_lecture_spec())


def _url(item, key, tail):
    return f"{L1}/items/{item.id}/steps/{key}/{tail}"


def _put(client, headers, item, key, body):
    return client.put(_url(item, key, "response"), json=body, headers=headers)


def _complete(client, headers, item, key):
    return client.post(_url(item, key, "complete"), headers=headers)


def _as(db, user):
    fastapi_app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)


def _rows(db, **filters):
    return db.query(AcademyStepResponse).filter_by(**filters).order_by(AcademyStepResponse.created_at, AcademyStepResponse.revision).all()


# -- reflect / reflection: a valid saved response, then Continue ------------------------------------------------------------------


def test_a_reflect_step_needs_a_valid_saved_response_before_it_can_complete(client, auth_headers, item, db):
    early = _complete(client, auth_headers, item, "baseline")
    assert early.status_code == 409 and early.json()["error"]["detail"]["code"] == "interaction_required"
    assert _put(client, auth_headers, item, "baseline", {"text": "too short"[:5]}).status_code == 422   # min_chars 8
    assert _put(client, auth_headers, item, "baseline", {"text": "x" * 8001}).status_code == 422
    saved = _put(client, auth_headers, item, "baseline", {"text": "AI is when computers think like humans."})
    assert saved.status_code == 200 and saved.json()["response"] == {"text": "AI is when computers think like humans."}
    # saving is not completing
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    baseline = next(s for s in view["steps"] if s["key"] == "baseline")
    assert baseline["status"] != "completed" and baseline["response"] == {"text": "AI is when computers think like humans."}
    done = _complete(client, auth_headers, item, "baseline")
    assert done.status_code == 200 and done.json()["step"]["status"] == "completed"
    row = db.query(__import__("app.models.academy", fromlist=["AcademyStepProgress"]).AcademyStepProgress).filter_by(step_key="baseline").one()
    assert row.completion_basis["kind"] == "response_saved" and row.completion_basis["reference"]


def test_refining_a_response_appends_a_revision_and_never_rewrites_history(client, auth_headers, item, db):
    for text in ("first thought about AI", "a better second thought", "third and final thought"):
        assert _put(client, auth_headers, item, "baseline", {"text": text}).status_code == 200
    rows = _rows(db, step_key="baseline")
    assert [r.revision for r in rows] == [1, 2, 3] and [r.content["text"] for r in rows][0] == "first thought about AI"
    latest = client.get(_url(item, "baseline", "response"), headers=auth_headers).json()
    assert latest["response"] == {"text": "third and final thought"}


def test_a_reflection_step_reads_back_the_earlier_baseline(client, auth_headers, item):
    _put(client, auth_headers, item, "baseline", {"text": "AI is when computers think like humans."})
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    reflection = next(s for s in view["steps"] if s["key"] == "compare-answers")
    assert reflection["compare_to"]["key"] == "baseline" and reflection["compare_to"]["response"] == {"text": "AI is when computers think like humans."}


def test_responses_are_validated_against_the_step_type_and_unknown_fields_are_refused(client, auth_headers, item):
    assert _put(client, auth_headers, item, "baseline", {"points": {"a": "b"}}).status_code == 422       # wrong shape for reflect
    assert _put(client, auth_headers, item, "baseline", {"text": "a proper reflection", "completed": True}).status_code == 422
    assert _put(client, auth_headers, item, "baseline", {"text": "a proper reflection", "status": "completed"}).status_code == 422
    assert _put(client, auth_headers, item, "baseline", {}).status_code == 422
    assert _put(client, auth_headers, item, "what-is-ai", {"text": "reading takes no response"}).status_code == 409   # teach
    assert _put(client, auth_headers, item, "ai-or-not", {"text": "the check uses the knowledge-check endpoint"}).status_code == 409


# -- think: commit once, reveal only afterwards ---------------------------------------------------------------------------------


def test_the_reveal_is_unavailable_until_the_answer_is_committed_and_the_commit_is_final(client, auth_headers, item, db):
    before = client.post(_url(item, "bp-reminder", "reveal"), headers=auth_headers)
    assert before.status_code == 409 and before.json()["error"]["detail"]["code"] == "commit_required" and REVEAL not in before.text
    assert _put(client, auth_headers, item, "bp-reminder", {"text": "short"}).status_code == 422
    assert _put(client, auth_headers, item, "bp-reminder", {"text": "Traditional software: the rule is exact."}).status_code == 200
    again = _put(client, auth_headers, item, "bp-reminder", {"text": "No, actually AI would be better here."})
    assert again.status_code == 409 and again.json()["error"]["detail"]["code"] == "already_committed"
    assert [r.content["text"] for r in _rows(db, step_key="bp-reminder", kind=AcademyStepResponseKind.ANSWER)] == ["Traditional software: the rule is exact."]
    reveal = client.post(_url(item, "bp-reminder", "reveal"), headers=auth_headers)
    assert reveal.status_code == 200 and reveal.json()["reveal_md"] == REVEAL
    # the reveal is recorded once, strictly after the commit
    notes = _rows(db, step_key="bp-reminder", kind=AcademyStepResponseKind.NOTE)
    client.post(_url(item, "bp-reminder", "reveal"), headers=auth_headers)
    assert len(_rows(db, step_key="bp-reminder", kind=AcademyStepResponseKind.NOTE)) == len(notes) == 1
    answer = _rows(db, step_key="bp-reminder", kind=AcademyStepResponseKind.ANSWER)[0]
    assert notes[0].created_at >= answer.created_at


def test_the_reveal_never_appears_in_the_ordinary_read_model_even_after_it_was_served(client, auth_headers, item):
    _put(client, auth_headers, item, "bp-reminder", {"text": "Traditional software: the rule is exact."})
    client.post(_url(item, "bp-reminder", "reveal"), headers=auth_headers)
    for url in (f"{L1}/days/1/learning", f"{L1}/items/{item.id}/steps", _url(item, "bp-reminder", "response")):
        blob = client.get(url, headers=auth_headers).text
        assert REVEAL not in blob and CLAIM_WHY not in blob and "reveal_md" not in blob, url
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    step = next(s for s in view["steps"] if s["key"] == "bp-reminder")
    assert step["committed"] is True and step["reveal_available"] is True and step["response"] == {"text": "Traditional software: the rule is exact."}


def test_a_statements_think_step_needs_every_claim_and_the_reveal_reports_matches(client, auth_headers, item):
    full = {"c1": {"choice": "depends", "reasoning": "a test score is not legal advice"}, "c2": {"choice": "true"}}
    assert _put(client, auth_headers, item, "three-claims", {"statements": {"c1": full["c1"]}}).status_code == 422          # missing c2
    assert _put(client, auth_headers, item, "three-claims", {"statements": {**full, "c2": {"choice": "maybe"}}}).status_code == 422
    assert _put(client, auth_headers, item, "three-claims", {"statements": {**full, "c3": {"choice": "true"}}}).status_code == 422   # unknown id
    assert client.post(_url(item, "three-claims", "reveal"), headers=auth_headers).status_code == 409
    assert _put(client, auth_headers, item, "three-claims", {"statements": full}).status_code == 200
    claims = client.post(_url(item, "three-claims", "reveal"), headers=auth_headers).json()["claims"]
    assert claims["c1"] == {"answer": "depends", "why_md": CLAIM_WHY, "matched": True}
    assert claims["c2"]["answer"] == "false" and claims["c2"]["matched"] is False
    assert _complete(client, auth_headers, item, "three-claims").status_code == 200


def test_only_a_think_step_has_a_reveal(client, auth_headers, item):
    assert client.post(_url(item, "what-is-ai", "reveal"), headers=auth_headers).status_code == 409


# -- explain-back: a private outline; the graded work stays in AIL.5C -------------------------------------------------------------


def test_an_explain_back_step_takes_a_private_outline_and_references_the_existing_assessment(client, auth_headers, item, db):
    assert _put(client, auth_headers, item, "explain-ai", {"points": {}}).status_code == 422
    assert _put(client, auth_headers, item, "explain-ai", {"points": {"what-it-is": "abc"}}).status_code == 422
    assert _put(client, auth_headers, item, "explain-ai", {"points": {"what-it-is": "It finds patterns in data."}}).status_code == 200
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    step = next(s for s in view["steps"] if s["key"] == "explain-ai")
    assert step["binding"] == {"kind": "assessment", "definition_key": "level1-day-01-explain-ai"}
    assert step["response"] == {"points": {"what-it-is": "It finds patterns in data."}}
    assert _complete(client, auth_headers, item, "explain-ai").status_code == 200
    # AIL5D writes no assessment attempt, result or evidence for an outline
    assert db.query(LearningEvidence).count() == 0


# -- knowledge check: the existing canonical mechanism ---------------------------------------------------------------------------


def test_a_required_check_step_completes_only_from_a_passed_attempt_and_failures_can_be_retried(client, auth_headers, item, db):
    early = _complete(client, auth_headers, item, "ai-or-not")
    assert early.status_code == 409 and early.json()["error"]["detail"]["code"] == "interaction_required"
    wrong = client.post(f"{L1}/items/{item.id}/knowledge-check", json={"answers": {"q1": "b"}}, headers=auth_headers)
    assert wrong.status_code == 200 and wrong.json()["passed"] is False
    assert [r["passed"] for r in wrong.json()["results"]] == [False] and "explanation" not in wrong.json()["results"][0]
    feedback = wrong.json()["feedback"]
    assert feedback["retry_allowed"] is True and feedback["incorrect_item_ids"] == ["q1"] and feedback["attempt"] == 1
    assert "answer" not in json.dumps(feedback).lower().replace("the answers", "")   # feedback guides; it never gives the answer
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    check = next(s for s in view["steps"] if s["key"] == "ai-or-not")
    assert check["check_result"]["submitted"] is True and check["check_result"]["passed"] is False and check["review"] is None
    assert "answer" not in json.dumps(check["questions"]) and [q["id"] for q in check["questions"]] == ["q1"]
    # an unsuccessful attempt is recorded, creates no demonstrated state, and does NOT complete the required step
    blocked = _complete(client, auth_headers, item, "ai-or-not")
    assert blocked.status_code == 409 and blocked.json()["error"]["detail"]["code"] == "interaction_required"
    assert view["learning_complete"] is False and view["demonstrated"] is False
    # retry until the authored success condition holds, then the step completes from that PASSED evidence
    for _ in range(2):
        again = client.post(f"{L1}/items/{item.id}/knowledge-check", json={"answers": {"q1": "c"}}, headers=auth_headers).json()
        assert again["passed"] is False and _complete(client, auth_headers, item, "ai-or-not").status_code == 409
    right = client.post(f"{L1}/items/{item.id}/knowledge-check", json={"answers": {"q1": "a"}}, headers=auth_headers).json()
    assert right["passed"] is True and right["feedback"] is None
    done = _complete(client, auth_headers, item, "ai-or-not")
    assert done.status_code == 200 and done.json()["step"]["status"] == "completed"
    evidence = db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.KNOWLEDGE_CHECK).order_by(LearningEvidence.created_at).all()
    assert [e.passed for e in evidence] == [False, False, False, True]          # the whole history is preserved
    progress = __import__("app.models.academy", fromlist=["AcademyStepProgress"]).AcademyStepProgress
    assert db.query(progress).filter_by(step_key="ai-or-not").one().completion_basis == {"kind": "knowledge_check_passed", "reference": evidence[-1].id}
    # the learner's own answers are kept with the step for every attempt, linked to its evidence
    kept = _rows(db, step_key="ai-or-not")
    assert [k.content["answers"] for k in kept] == [{"q1": "b"}, {"q1": "c"}, {"q1": "c"}, {"q1": "a"}]
    assert [k.ref_id for k in kept] == [e.id for e in evidence] and {k.ref_type for k in kept} == {"learning_evidence"}
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LESSON_COMPLETED).count() == 0   # other required steps remain


def test_an_unsuccessful_check_never_satisfies_the_day_and_creates_no_demonstrated_state(client, auth_headers, item, db):
    client.post(f"{L1}/items/{item.id}/knowledge-check", json={"answers": {"q1": "zzz"}}, headers=auth_headers)
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    assert view["demonstrated"] is False and view["concept_state"] != "demonstrated" and view["learning_complete"] is False
    failed = db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.KNOWLEDGE_CHECK).one()
    assert failed.passed is False
    from app.services.learner_state_service import LearnerStateService

    assert LearnerStateService(db).state(failed.user_id, item.concept_id).ladder != "demonstrated"



def test_the_authored_rationale_is_returned_only_once_the_check_is_passed(client, auth_headers, db, bootstrap):
    spec = _lecture_spec()
    spec["knowledge_check"] = [{"id": "q1", "prompt": "Q?", "answer": "a", "explanation": "Because the rule is exact."}]
    item = make_structured_item(db, slug="rationale", spec=spec)
    failed = client.post(f"{L1}/items/{item.id}/knowledge-check", json={"answers": {"q1": "z"}}, headers=auth_headers).json()
    assert "Because the rule is exact." not in json.dumps(failed)
    passed = client.post(f"{L1}/items/{item.id}/knowledge-check", json={"answers": {"q1": "a"}}, headers=auth_headers).json()
    assert passed["passed"] is True and passed["results"][0]["explanation"] == "Because the rule is exact."
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    check = next(s for s in view["steps"] if s["key"] == "ai-or-not")
    assert check["review"] == [{"id": "q1", "explanation": "Because the rule is exact."}] and check["check_result"]["attempts"] == 2


def test_the_legacy_knowledge_check_response_is_unchanged_for_non_structured_items(client, auth_headers, bootstrap, db):
    from tests.ail5d1_factories import make_legacy_item

    legacy = make_legacy_item(db, slug="legacy-kc")
    body = client.post(f"{L1}/items/{legacy.id}/knowledge-check", json={"answers": {"q1": "a"}}, headers=auth_headers).json()
    assert body["passed"] is True and body["results"] == [{"id": "q1", "passed": True}]
    assert db.query(AcademyStepResponse).count() == 0


# -- completion is server-authoritative ----------------------------------------------------------------------------------------------


def test_a_lab_step_still_cannot_complete_until_the_lab_slice(db, bootstrap):
    lab_item = make_structured_item(db, slug="lab-day")
    with pytest.raises(Exception) as err:
        AcademyStepService(db).complete_step(bootstrap.user.id, lab_item.id, "lab-run")
    assert "completed by doing it" in str(err.value)
    assert db.query(AcademyStepResponse).count() == 0


def test_a_client_cannot_complete_an_interactive_step_by_any_request(client, auth_headers, item, db):
    for key in ("baseline", "bp-reminder", "three-claims", "ai-or-not", "explain-ai", "compare-answers"):
        forged = client.post(_url(item, key, "complete"), json={"completed": True, "status": "completed", "verified": {"kind": "committed_answer"}}, headers=auth_headers)
        assert forged.status_code == 409, key
    assert client.put(_url(item, "bp-reminder", "response"), json={"completed": True}, headers=auth_headers).status_code == 422
    progress = __import__("app.models.academy", fromlist=["AcademyStepProgress"]).AcademyStepProgress
    assert db.query(progress).filter_by(status=__import__("app.db.enums", fromlist=["AcademyStepStatus"]).AcademyStepStatus.COMPLETED).count() == 0


def test_viewing_and_opening_never_complete_an_interactive_step_even_with_a_saved_response(client, auth_headers, item, db):
    _put(client, auth_headers, item, "baseline", {"text": "saved but not continued"})
    for _ in range(3):
        client.post(_url(item, "baseline", "open"), headers=auth_headers)
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    assert next(s for s in view["steps"] if s["key"] == "baseline")["status"] == "opened"
    assert view["learning_complete"] is False


def test_the_full_required_workflow_completes_the_day_and_earns_lesson_completed_once(client, auth_headers, item, db):
    required = [s["key"] for s in sample_steps() if s["required"] and s["type"] != "lab"]
    assert required == ["what-is-ai", "rules-vs-patterns", "bp-reminder", "three-claims", "ai-or-not"]
    for key in ("what-is-ai", "rules-vs-patterns"):
        assert _complete(client, auth_headers, item, key).status_code == 200
    _put(client, auth_headers, item, "bp-reminder", {"text": "Traditional software: the rule is exact."})
    _put(client, auth_headers, item, "three-claims", {"statements": {"c1": {"choice": "depends"}, "c2": {"choice": "false"}}})
    client.post(f"{L1}/items/{item.id}/knowledge-check", json={"answers": {"q1": "a"}}, headers=auth_headers)
    for key in ("bp-reminder", "three-claims"):
        assert _complete(client, auth_headers, item, key).status_code == 200
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LESSON_COMPLETED).count() == 0   # one required step to go
    assert _complete(client, auth_headers, item, "ai-or-not").status_code == 200
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    assert view["learning_complete"] is True and view["demonstrated"] is False
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LESSON_COMPLETED).count() == 1
    _complete(client, auth_headers, item, "ai-or-not")
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LESSON_COMPLETED).count() == 1


# -- ownership, versions, privacy --------------------------------------------------------------------------------------------------


def test_responses_are_private_to_the_learner(client, auth_headers, item, db, bootstrap):
    _put(client, auth_headers, item, "baseline", {"text": "my private thought about AI"})
    other = make_user(db, org=None, email="second@example.com")
    db.commit()
    _as(db, other)
    assert client.get(_url(item, "baseline", "response"), headers=auth_headers).json()["response"] is None
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    assert "my private thought" not in json.dumps(view)
    assert _complete(client, auth_headers, item, "baseline").status_code == 409        # cannot ride on someone else's response
    assert _put(client, auth_headers, item, "baseline", {"text": "the second learner's own thought"}).status_code == 200
    fastapi_app.dependency_overrides.pop(get_current_user, None)
    assert client.get(_url(item, "baseline", "response"), headers=auth_headers).json()["response"] == {"text": "my private thought about AI"}
    assert {r.user_id for r in _rows(db, step_key="baseline")} == {bootstrap.user.id, other.id}


def test_responses_are_bound_to_the_exact_version_and_carry_only_for_unchanged_steps(client, auth_headers, item, db):
    _put(client, auth_headers, item, "baseline", {"text": "an answer written on version one"})
    _put(client, auth_headers, item, "bp-reminder", {"text": "Traditional software: the rule is exact."})
    spec2 = _lecture_spec()
    next(s for s in spec2["steps"] if s["key"] == "bp-reminder")["content"]["question_md"] = "A reworded question?"
    v2 = new_version(db, item, spec=spec2)
    stale = _put(client, auth_headers, item, "baseline", {"text": "writing to the old version"})
    assert stale.status_code == 409 and stale.json()["error"]["detail"]["code"] == "stale_item_version"
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    by_key = {s["key"]: s for s in view["steps"]}
    assert view["item_id"] == v2.id
    assert by_key["baseline"]["response"] == {"text": "an answer written on version one"}          # unchanged step: carried
    assert by_key["bp-reminder"]["response"] is None and by_key["bp-reminder"]["committed"] is False   # revised step: a fresh commit
    assert {r.learning_item_id for r in _rows(db, step_key="baseline")} == {item.id}                 # history stays on v1


def test_the_database_holds_one_row_per_revision_and_the_service_sets_the_owner(db, item, bootstrap):
    AcademyStepService(db).respond(bootstrap.user.id, item.id, "baseline", {"text": "a first thought"})
    row = _rows(db, step_key="baseline")[0]
    assert row.user_id == bootstrap.user.id and row.lineage_id == item.lineage_id and row.revision == 1 and row.step_fingerprint
    db.add(AcademyStepResponse(user_id=row.user_id, learning_item_id=row.learning_item_id, lineage_id=row.lineage_id, step_key="baseline",
                               step_fingerprint=row.step_fingerprint, kind=AcademyStepResponseKind.REFLECTION, response_key="", revision=1, content={"text": "dup"}))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_the_professor_context_never_contains_learner_responses(db, item, bootstrap):
    from app.schemas.professor import ProfessorContextRequest, ProfessorIntent, ProfessorTarget, ProfessorTargetType
    from app.services.professor_context_service import ProfessorContextAssembler

    AcademyStepService(db).respond(bootstrap.user.id, item.id, "baseline", {"text": "SECRET-LEARNER-THOUGHT about ai"})
    AcademyStepService(db).respond(bootstrap.user.id, item.id, "bp-reminder", {"text": "SECRET-LEARNER-ANSWER traditional"})
    context = ProfessorContextAssembler(db).assemble(
        bootstrap.user.id,
        ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=item.concept_id)),
    )
    blob = json.dumps([r.model_dump(mode="json") for r in context.records], default=str) + json.dumps(context.deterministic_facts, default=str)
    for leaked in ("SECRET-LEARNER", REVEAL, CLAIM_WHY, "step_key", "academy_step_responses"):
        assert leaked not in blob, leaked


def test_the_service_writes_no_evidence_for_responses_and_reveals(db, item, bootstrap):
    svc, user = AcademyStepService(db), bootstrap.user
    svc.respond(user.id, item.id, "baseline", {"text": "a first thought"})
    svc.respond(user.id, item.id, "bp-reminder", {"text": "Traditional software: exact rule."})
    svc.reveal(user.id, item.id, "bp-reminder")
    svc.respond(user.id, item.id, "explain-ai", {"points": {"what-it-is": "It finds patterns."}})
    assert db.query(LearningEvidence).count() == 0
    svc.complete_step(user.id, item.id, "baseline", verified=VerifiedCompletion(kind="test"))
    assert db.query(LearningEvidence).count() == 0  # one optional step is not the last required one
