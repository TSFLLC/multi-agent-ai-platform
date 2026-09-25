"""AIL.5C review-driven hardening: attempt immutability, learner export/delete, draft
retention, one policy for H-level rules, and record links after a confirmed review."""

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.assessment_outcome import _effect
from app.db.enums import (
    AssessmentAttemptStatus,
    AssessmentReviewDecision,
    AssistanceLevel,
    DemonstrationEffect,
)
from app.models.artifacts_eval import Artifact
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentResult,
    AssessmentReview,
    ImmutableRecordError,
)
from app.models.learner import LearningEvidence
from app.models.tasks import AgentRun, Task
from app.services import independence_policy as policy
from app.services.assessment_mode_guard import DRAFT_RETENTION_DAYS, AssessmentModeGuard
from app.services.assessment_review_service import AssessmentReviewService
from app.services.assessment_service import AssessmentService
from app.services.learner_profile_service import LearnerProfileService
from tests.ail5c_factories import (
    ScriptedGrader,
    explain_definition,
    kc_definition,
    make_ail_concept,
    second_user,
    setup_free_models,
)

REQ_KC_EB = {"requires_all": [{"evidence_type": "knowledge_check", "min_passed": 1}, {"evidence_type": "explain_back", "min_passed": 1}]}
WORDS = "Structured output makes a model follow a schema so answers parse reliably; a schema does not make content true. LIFECYCLE-WORDS"


def _passed_kc(db, bootstrap, *, key="kc-structured-output", user=None):
    user = user or second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db)
    defn = kc_definition(db, bootstrap.user, concept, key=key)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(user.id, attempt.id, {"responses": {i["entry_key"]: {"selected": [1]} for i in attempt.challenge_instance["items"]}})
    return user, concept, svc, svc.submit(user, attempt.id, {"declaration": "no_external_help"})


# -- attempt immutability -------------------------------------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("input_manifest", {"tampered": True}), ("challenge_instance", {"items": []}), ("pinned_versions", {}),
    ("submission", {"responses": {}}), ("attestation", {"declaration": "other"}), ("submission_hash", "0" * 64),
    ("definition_id", "x"), ("user_id", "someone-else"),
])
def test_an_attempts_pinned_and_frozen_fields_cannot_be_edited(db, bootstrap, field, value):
    _user, _c, svc, attempt = _passed_kc(db, bootstrap)
    row = db.get(AssessmentAttempt, attempt.id)
    setattr(row, field, value)
    with pytest.raises(ImmutableRecordError):
        db.commit()
    db.rollback()


def test_the_draft_is_only_editable_while_the_attempt_is_a_draft(db, bootstrap):
    user = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db)
    defn = kc_definition(db, bootstrap.user, concept)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key)
    row = db.get(AssessmentAttempt, attempt.id)
    row.draft = {"responses": {"x": {"selected": [0]}}}
    db.commit()  # allowed: still a draft
    svc.save_draft(user.id, attempt.id, {"responses": {i["entry_key"]: {"selected": [1]} for i in attempt.challenge_instance["items"]}})
    svc.submit(user, attempt.id, {"declaration": "no_external_help"})
    finished = db.get(AssessmentAttempt, attempt.id)
    finished.draft = {"late": True}
    with pytest.raises(ImmutableRecordError):
        db.commit()
    db.rollback()


def test_the_grader_agent_version_is_pinned_once(db, bootstrap):
    setup_free_models(db, count=2)
    user = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db, requirements=REQ_KC_EB)
    defn = explain_definition(db, bootstrap.user, concept)
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: ScriptedGrader())
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(user.id, attempt.id, {"fields": {"explanation": WORDS}})
    done = svc.submit(user, attempt.id, {"declaration": "no_external_help"})
    row = db.get(AssessmentAttempt, done.id)
    assert row.grader_agent_version_id
    row.grader_agent_version_id = "a-different-version"
    with pytest.raises(ImmutableRecordError):
        db.commit()
    db.rollback()


# -- one policy for H-level rules ------------------------------------------------------------------------------------


def test_effect_rules_come_from_the_single_independence_policy():
    levels = {lvl: policy.level_counts_toward_practiced(AssistanceLevel(lvl)) for lvl in ("h0", "h1", "h2", "h3", "h4", "h5")}
    assert levels == {"h0": True, "h1": True, "h2": True, "h3": True, "h4": True, "h5": False}
    assert policy.level_counts_toward_practiced(None) is True

    def effect(source_levels):
        classes = sorted({policy.classify_assistance(AssistanceLevel(l)) for l in source_levels})
        return _effect({"challenge_issued": False, "source_classes": classes, "source_levels": source_levels}, True, False, None)

    assert effect(["h1"]) == DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED
    assert effect(["h3"]) == DemonstrationEffect.COUNTS_TOWARD_PRACTICED_ONLY
    assert effect(["h4"]) == DemonstrationEffect.COUNTS_TOWARD_PRACTICED_ONLY
    assert effect(["h5"]) == DemonstrationEffect.FORMATIVE_ONLY
    assert effect(["h0", "h5"]) == DemonstrationEffect.FORMATIVE_ONLY
    assert policy.ASSESSMENT_MODE_ASSISTANCE == AssistanceLevel.H0


# -- draft retention -----------------------------------------------------------------------------------------------------------


def test_abandoned_drafts_are_cleared_after_thirty_days_but_the_attempt_row_is_kept(db, bootstrap):
    user = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db)
    defn = kc_definition(db, bootstrap.user, concept)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(user.id, attempt.id, {"responses": {"q": {"text": "unsent words"}}})
    svc.abandon(user, attempt.id)
    assert AssessmentModeGuard(db).purge_stale_drafts(user.id) == 0  # too recent
    future = AssessmentModeGuard(db, now=datetime.now(timezone.utc) + timedelta(days=DRAFT_RETENTION_DAYS + 1))
    assert future.purge_stale_drafts(user.id) == 1
    kept = db.get(AssessmentAttempt, attempt.id)
    db.refresh(kept)
    assert kept.status == AssessmentAttemptStatus.ABANDONED and kept.draft == {}


# -- export and deletion ----------------------------------------------------------------------------------------------------------


def _explain_with_review(db, bootstrap):
    setup_free_models(db, count=2)
    learner = second_user(db, bootstrap.organization)
    concept, _v = make_ail_concept(db, requirements=REQ_KC_EB)
    defn = explain_definition(db, bootstrap.user, concept)
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: ScriptedGrader())
    attempt = svc.start(learner, defn.definition_key)
    svc.save_draft(learner.id, attempt.id, {"fields": {"explanation": WORDS}})
    done = svc.submit(learner, attempt.id, {"declaration": "no_external_help"})
    review = AssessmentReviewService(db).request(learner, done.id, reason="Please take another look.", consent=True)
    return learner, svc, done, review


def test_learner_export_includes_their_assessments_and_only_theirs(db, bootstrap):
    learner, _svc, done, review = _explain_with_review(db, bootstrap)
    other, _c, _s, other_attempt = _passed_kc(db, bootstrap, key="kc-other", user=second_user(db, bootstrap.organization, "other@example.com"))
    export = LearnerProfileService(db).export_learner_data(learner.id)["assessments"]
    assert [a["id"] for a in export["attempts"]] == [done.id]
    assert "LIFECYCLE-WORDS" in str(export["attempts"][0]["submission"])
    assert {r["kind"] for r in export["results"]} >= {"deterministic", "grader", "final"}
    assert [v["id"] for v in export["reviews"]] == [review.id]
    assert other_attempt.id not in str(export)


def test_learner_deletion_removes_assessments_evidence_and_grader_artifacts_only_for_that_learner(db, bootstrap):
    learner, svc, done, _review = _explain_with_review(db, bootstrap)
    keep_user, _c, _s, keep_attempt = _passed_kc(db, bootstrap, key="kc-keep", user=second_user(db, bootstrap.organization, "keep@example.com"))
    # a reviewed reversal exercises the supersede pointers on the way out
    kc_user, _c2, _s2, kc_attempt = _passed_kc(db, bootstrap, key="kc-rev", user=learner)
    reviews = AssessmentReviewService(db)
    open_review = db.query(AssessmentReview).filter_by(attempt_id=kc_attempt.id).first() or reviews.request(learner, kc_attempt.id, reason="I disagree with this.", consent=True)
    reviews.decide(bootstrap.user, open_review.id, AssessmentReviewDecision.OVERRIDE_NEEDS_WORK, "The attestation was inconsistent.")

    runs = db.query(AgentRun).filter(AgentRun.role == "grader").all()
    files = [a.storage_ref for a in db.query(Artifact).filter(Artifact.agent_run_id.in_([r.id for r in runs])).all()]
    assert files and all(os.path.exists(f) for f in files)

    LearnerProfileService(db).delete_learner_data(learner.id)

    for model in (AssessmentAttempt, AssessmentResult, AssessmentReview, LearningEvidence):
        assert db.query(model).filter_by(user_id=learner.id).count() == 0, model.__name__
    assert not db.query(Task).filter(Task.created_by == learner.id, Task.title.like("assessment-grading:%")).count()
    assert not any(os.path.exists(f) for f in files), "grader artifact files quoting the learner are removed"
    assert db.query(AssessmentAttempt).filter_by(id=keep_attempt.id).count() == 1  # nobody else is touched
    assert db.query(LearningEvidence).filter_by(user_id=keep_user.id).count() == 1


# -- record link after a confirmed review ---------------------------------------------------------------------------------------------


def test_a_confirmed_review_keeps_exactly_one_record_and_the_result_page_still_links_it(client, db, bootstrap, auth_headers):
    from app.auth import get_current_user
    from app.main import app as fastapi_app
    from app.models.identity import User

    learner, _c, svc, attempt = _passed_kc(db, bootstrap)
    original = svc.effective_result(attempt.id)
    reviews = AssessmentReviewService(db)
    review = reviews.request(learner, attempt.id, reason="Please double check this pass.", consent=True)
    reviews.decide(bootstrap.user, review.id, AssessmentReviewDecision.CONFIRM, "The pass is correct.")

    fastapi_app.dependency_overrides[get_current_user] = lambda: db.get(User, learner.id)
    try:
        view = client.get(f"/academy/assessments/attempts/{attempt.id}/result", headers=auth_headers).json()
        assert view["result"]["kind"] == "human" and view["result"]["has_record"]
        assert view["result"]["record_result_id"] == original.id
        records = client.get("/academy/assessments/records", headers=auth_headers).json()
        assert [r["result_id"] for r in records] == [original.id] and records[0]["status"] == "valid"
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
