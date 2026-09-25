"""AIL.5C deterministic local UAT — the eight acceptance scenarios, end to end.

Runs entirely against a disposable temp database with a scripted fake Grader
provider: no network, no real model, never the real database. Run as
``python -m scripts.ail5c_uat`` for a readable transcript, or via pytest.

  UAT 1  Successful independent demonstration
  UAT 2  H5 source work
  UAT 3  Needs Work -> remediation -> retry
  UAT 4  Low confidence / disagreement
  UAT 5  Provider failure
  UAT 6  Human review
  UAT 7  Personal Lab
  UAT 8  Cross-user isolation
"""

from datetime import datetime, timedelta, timezone

from app.auth import get_current_user
from app.db.enums import AssessmentOutcome, AssistanceLevel, EvidenceType
from app.main import app as fastapi_app
from app.models.identity import User
from app.models.learner import LearningEvidence
from app.models.tasks import AgentRun
from app.providers.base import ProviderConnectionError
from app.services.assessment_review_service import AssessmentReviewService
from app.services.assessment_service import AssessmentService
from app.services.learner_state_service import DEMONSTRATED, LearnerStateService
from app.services.system_project_service import ensure_ail_system_project
from tests.ail5c_factories import (
    ScriptedGrader,
    add_milestone_evidence,
    default_findings,
    explain_definition,
    judgment,
    kc_definition,
    make_ail_concept,
    make_evaluation_run,
    make_experiment,
    make_owned_run,
    make_project_attempt,
    project_definition,
    second_user,
    setup_free_models,
)
from tests.test_ail5b_product_api import _template

B = "/academy/assessments"
REQ_KC_EB = {
    "requires_all": [
        {"evidence_type": "knowledge_check", "min_passed": 1},
        {"evidence_type": "explain_back", "min_passed": 1},
    ]
}
REQ_PROJECT = {"requires_all": [{"evidence_type": "project_assessment", "min_passed": 1}]}
WORDS = "Structured output makes a model follow a schema so answers parse reliably; a schema does not make content true."


def _http(client, headers, method, path, **kw):
    response = getattr(client, method)(B + path, headers=headers, **kw)
    return response


def _evidence(db, user, evidence_type=None):
    rows = db.query(LearningEvidence).filter_by(user_id=user.id).all()
    return [r for r in rows if evidence_type is None or r.evidence_type == evidence_type]


def test_uat1_successful_independent_demonstration(client, db, bootstrap, auth_headers):
    """READY -> ASSESS -> FRESH CHALLENGE -> PASS -> LEARNING EVIDENCE -> STATE RECOMPUTATION -> DEMONSTRATION RECORD"""
    user = bootstrap.user
    concept, _v = make_ail_concept(db, core=True)
    kc_definition(db, user, concept)

    center = _http(client, auth_headers, "get", "/center").json()
    assert [r["definition_key"] for r in center["ready_for_assessment"]] == ["kc-structured-output"]  # READY
    attempt = _http(
        client, auth_headers, "post", "/attempts", json={"definition_key": "kc-structured-output"}
    ).json()  # ASSESS
    assert attempt["mentor_locked"] and attempt["challenge"]["items"]  # FRESH CHALLENGE
    draft = {"responses": {i["entry_key"]: {"selected": [1]} for i in attempt["challenge"]["items"]}}
    _http(client, auth_headers, "put", f"/attempts/{attempt['id']}/draft", json={"draft": draft})
    result = _http(
        client,
        auth_headers,
        "post",
        f"/attempts/{attempt['id']}/submit",
        json={"attestation": {"declaration": "no_external_help"}},
    ).json()
    assert result["result"]["outcome"] == "passed"  # PASS
    assert result["result"]["demonstration_effect"] == "counts_toward_demonstrated"
    rows = _evidence(db, user)
    assert len(rows) == 1 and rows[0].ref_id == result["result"]["id"]  # LEARNING EVIDENCE
    assert LearnerStateService(db).state(user.id, concept.id).ladder == DEMONSTRATED  # STATE RECOMPUTATION
    assert result["result"]["report"]["answers"]["learner_state"][concept.id]["after"] == "demonstrated"
    records = _http(client, auth_headers, "get", "/records").json()  # DEMONSTRATION RECORD
    assert len(records) == 1
    record = _http(client, auth_headers, "get", f"/records/{records[0]['result_id']}").json()
    assert record["record"]["notice"] == "Not a certificate or credential."


def test_uat2_h5_source_work_needs_a_fresh_independent_challenge(client, db, bootstrap, auth_headers):
    """H5-assisted project -> ASSESSMENT -> fresh independent challenge required -> source project alone cannot demonstrate"""
    from app.models.academy import ProjectTemplateConceptLink

    user = bootstrap.user
    concept, version = make_ail_concept(db, requirements=REQ_PROJECT)
    template, _m = _template(db, user.id)
    db.add(ProjectTemplateConceptLink(project_template_id=template.id, concept_id=concept.id, role="applied"))
    db.commit()
    pa = make_project_attempt(db, user, template, levels=(AssistanceLevel.H5,), variant_after_h5=True)
    add_milestone_evidence(db, user, pa, concept, version)
    project_definition(db, user, concept, template, key="proj-nochallenge", challenge=False)
    project_definition(db, user, concept, template, key="proj-fresh", challenge=True)

    readiness = _http(
        client, auth_headers, "get", f"/definitions/proj-nochallenge/readiness?project_attempt_id={pa.id}"
    ).json()
    assert (
        readiness["fresh_required"] and not readiness["ready"]
    )  # the source project alone cannot demonstrate
    assert (
        _http(
            client,
            auth_headers,
            "post",
            "/attempts",
            json={"definition_key": "proj-nochallenge", "project_attempt_id": pa.id},
        ).status_code
        == 409
    )

    attempt = _http(
        client,
        auth_headers,
        "post",
        "/attempts",
        json={"definition_key": "proj-fresh", "project_attempt_id": pa.id},
    ).json()
    assert attempt["fresh_required"] and attempt["challenge"]["items"]
    needle = attempt["challenge"]["parameters"]["input_text"]
    run = make_owned_run(
        db,
        user,
        project=ensure_ail_system_project(db, user),
        description=f"rerun {needle}",
        created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    evaluation = make_evaluation_run(db, user, ["met"])
    _http(
        client,
        auth_headers,
        "put",
        f"/attempts/{attempt['id']}/draft",
        json={"draft": {"run_ids": [run.id], "evaluation_run_ids": [evaluation.id]}},
    )
    view = _http(
        client,
        auth_headers,
        "post",
        f"/attempts/{attempt['id']}/submit",
        json={"attestation": {"declaration": "no_external_help"}},
    ).json()
    assert view["result"]["outcome"] == "passed"
    row = _evidence(db, user, EvidenceType.PROJECT_ASSESSMENT)[0]
    assert row.assistance_level == AssistanceLevel.H0  # qualified by the fresh challenge...
    assert (
        "h5" in view["result"]["report"]["platform_fact"]["assistance"]["source_levels"]
    )  # ...with the H5 history still visible


def test_uat3_needs_work_remediation_retry(db, bootstrap):
    """ASSESSMENT -> GAP -> NEEDS_WORK -> REMEDIATION -> RETRY"""
    setup_free_models(db, count=2)
    user = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db, requirements=REQ_KC_EB)
    defn = explain_definition(db, bootstrap.user, concept)
    weak = ScriptedGrader(
        lambda n, keys, quote: [
            judgment(
                k,
                quote,
                finding="not_met" if k == "limits" else "met",
                gap="No limit is stated." if k == "limits" else None,
            )
            for k in keys
        ]
    )
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: weak)
    first = svc.start(user, defn.definition_key)
    svc.save_draft(user.id, first.id, {"fields": {"explanation": WORDS}})
    done = svc.submit(user, first.id, {"declaration": "no_external_help"})
    final = svc.effective_result(done.id)
    assert final.outcome == AssessmentOutcome.NEEDS_WORK  # NEEDS_WORK
    assert [g["criterion_key"] for g in final.gaps] == ["limits"]  # SPECIFIC GAP
    assert any(s["kind"] == "learning_item" for s in final.remediation) and any(
        s["kind"] == "retry" for s in final.remediation
    )  # REMEDIATION
    assert _evidence(db, user) == []

    good = ScriptedGrader()
    later = AssessmentService(
        db, now=datetime.now(timezone.utc) + timedelta(hours=13), adapter_factory=lambda _d, _p: good
    )
    second = later.start(user, defn.definition_key, previous_attempt_id=first.id)  # RETRY
    assert {i["entry_key"] for i in second.challenge_instance["items"]} != {
        i["entry_key"] for i in first.challenge_instance["items"]
    }
    later.save_draft(user.id, second.id, {"fields": {"explanation": WORDS}})
    assert (
        later.effective_result(later.submit(user, second.id, {"declaration": "no_external_help"}).id).outcome
        == AssessmentOutcome.PASSED
    )
    assert later.get_attempt(user.id, first.id).finalized_at is not None  # the failed attempt is preserved


def test_uat4_low_confidence_writes_no_mastery_evidence(db, bootstrap):
    """Grader uncertainty -> PROVISIONAL / HUMAN_REVIEW_REQUIRED -> no mastery evidence"""
    setup_free_models(db, count=2)
    user = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db, requirements=REQ_KC_EB)
    unsure = ScriptedGrader(lambda n, keys, quote: default_findings(keys, quote, confidence="low"))
    d1 = explain_definition(db, bootstrap.user, concept, key="eb-low")
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: unsure)
    a = svc.start(user, d1.definition_key)
    svc.save_draft(user.id, a.id, {"fields": {"explanation": WORDS}})
    assert (
        svc.effective_result(svc.submit(user, a.id, {"declaration": "no_external_help"}).id).outcome
        == AssessmentOutcome.PROVISIONAL
    )

    split = ScriptedGrader(
        lambda n, keys, quote: default_findings(keys, quote, finding="met" if n == 1 else "not_met")
    )
    d2 = explain_definition(db, bootstrap.user, concept, key="eb-split")
    svc2 = AssessmentService(db, adapter_factory=lambda _d, _p: split)
    b = svc2.start(user, d2.definition_key)
    svc2.save_draft(user.id, b.id, {"fields": {"explanation": WORDS}})
    assert (
        svc2.effective_result(svc2.submit(user, b.id, {"declaration": "no_external_help"}).id).outcome
        == AssessmentOutcome.HUMAN_REVIEW_REQUIRED
    )
    assert _evidence(db, user) == []


def test_uat5_provider_failure_is_safe(db, bootstrap):
    """Grader failure -> assessment preserved -> no false result -> safe retry"""
    setup_free_models(db, count=2)
    user = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db, requirements=REQ_KC_EB)
    defn = explain_definition(db, bootstrap.user, concept)
    down = ScriptedGrader(lambda n, keys, quote: ProviderConnectionError("provider down", status_code=503))
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: down)
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(user.id, attempt.id, {"fields": {"explanation": WORDS}})
    stuck = svc.submit(user, attempt.id, {"declaration": "no_external_help"})
    assert (
        stuck.status.value == "awaiting_grading" and svc.effective_result(attempt.id) is None
    )  # preserved, no false result
    assert _evidence(db, user) == []

    ok = ScriptedGrader()
    retry = AssessmentService(db, adapter_factory=lambda _d, _p: ok)
    assert retry.effective_result(retry.grade(user, attempt.id).id).outcome == AssessmentOutcome.PASSED
    retry.grade(user, attempt.id)
    assert len(_evidence(db, user)) == 1  # safe, idempotent retry


def test_uat6_human_review_is_audited_and_preserves_history(db, bootstrap):
    """learner requests review -> authorized review -> audited decision -> history preserved"""
    from app.models.observability import AuditEvent

    learner = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db)
    defn = kc_definition(db, bootstrap.user, concept)
    svc = AssessmentService(db)
    attempt = svc.start(learner, defn.definition_key)
    svc.save_draft(
        learner.id,
        attempt.id,
        {"responses": {i["entry_key"]: {"selected": [0]} for i in attempt.challenge_instance["items"]}},
    )
    done = svc.submit(learner, attempt.id, {"declaration": "no_external_help"})
    original = svc.effective_result(done.id)
    assert original.outcome == AssessmentOutcome.NEEDS_WORK

    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason="I think the wording was ambiguous.", consent=True)
    reviews.detail(bootstrap.user, review.id)  # authorized reviewer reads (audited)
    reviews.decide(
        bootstrap.user,
        review.id,
        __import__("app.db.enums", fromlist=["x"]).AssessmentReviewDecision.CONFIRM,
        "The wording was clear; the result stands.",
    )
    assert svc.effective_result(attempt.id).supersedes_result_id == original.id
    db.refresh(original)
    assert original.outcome == AssessmentOutcome.NEEDS_WORK  # history preserved
    events = [e.event_type for e in db.query(AuditEvent).all()]
    assert {"assessment.review_requested", "assessment.review_viewed", "assessment.review_decided"} <= set(
        events
    )


def test_uat7_personal_lab_layers_stay_distinct(db, bootstrap):
    """experiment -> learner conclusion -> assessment judgment -> all three remain distinct"""
    from tests.test_ail5c_independence import _interpretation

    user, _c, defn = _interpretation(db, bootstrap)
    experiment = make_experiment(db, user, labels={"A": ["met", "met"], "B": ["met", "not_met"]})
    db.refresh(experiment)
    conclusion = experiment.conclusion_text
    grader = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: grader)
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(
        user.id,
        attempt.id,
        {
            "experiment_id": experiment.id,
            "fields": {"claim_best": "A", "explanation": "A passed more cases; n was small."},
        },
    )
    final = svc.effective_result(svc.submit(user, attempt.id, {"declaration": "no_external_help"}).id)
    prompt = grader.requests[0].user_prompt
    assert (
        "PLATFORM OBSERVATION (experiment result)" in prompt
        and "LEARNER CONCLUSION (human interpretation)" in prompt
    )
    db.refresh(experiment)
    assert experiment.conclusion_text == conclusion  # the human conclusion is untouched
    assert final.report["grader_judgment"]["ran"] and final.report["platform_fact"]["deterministic_checks"]
    assert final.report["grader_judgment"]["criteria"][0]["key"] == "faithful"  # judgment is its own record


def test_uat8_cross_user_isolation(client, db, bootstrap, auth_headers):
    """Learner A cannot retrieve Learner B's assessment / evidence / review / Grader records"""
    setup_free_models(db, count=2)
    a = bootstrap.user
    concept, _v = make_ail_concept(db, requirements=REQ_KC_EB)
    kc = kc_definition(db, a, concept)
    eb = explain_definition(db, a, concept)
    grader = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: grader)
    att = svc.start(a, eb.definition_key)
    svc.save_draft(a.id, att.id, {"fields": {"explanation": WORDS + " ALICE-PRIVATE"}})
    svc.submit(a, att.id, {"declaration": "no_external_help"})
    kc_attempt = svc.start(a, kc.definition_key)
    svc.save_draft(
        a.id,
        kc_attempt.id,
        {"responses": {i["entry_key"]: {"selected": [0]} for i in kc_attempt.challenge_instance["items"]}},
    )
    svc.submit(a, kc_attempt.id, {"declaration": "no_external_help"})
    review = AssessmentReviewService(db).request(
        a, kc_attempt.id, reason="Please look again at this.", consent=True
    )
    grader_run = db.query(AgentRun).filter(AgentRun.role == "grader").first()
    result_id = svc.effective_result(att.id).id

    bob = second_user(db, bootstrap.organization)
    ensure_ail_system_project(
        db, bob
    )  # every learner is OWNER of the shared AIL project: RBAC alone cannot isolate them
    db.commit()
    fastapi_app.dependency_overrides[get_current_user] = lambda: db.get(User, bob.id)
    try:
        for path in (f"/attempts/{att.id}", f"/attempts/{att.id}/result", f"/records/{result_id}"):
            assert client.get(B + path, headers=auth_headers).status_code == 404, path
        assert client.post(f"{B}/reviews/{review.id}/withdraw", headers=auth_headers).status_code == 404
        assert client.get(f"{B}/records", headers=auth_headers).json() == []
        assert "ALICE-PRIVATE" not in client.get(f"{B}/center", headers=auth_headers).text
        # the Grader's own run, its artifact and its bookkeeping task are private to Alice too (shared AIL project)
        assert client.get(f"/agent-runs/{grader_run.id}", headers=auth_headers).status_code == 404
        assert client.get(f"/agent-runs/{grader_run.id}/artifacts", headers=auth_headers).status_code == 404
        assert client.get(f"{B}/reviews", headers=auth_headers).status_code == 403
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    assert _evidence(db, bob) == []
