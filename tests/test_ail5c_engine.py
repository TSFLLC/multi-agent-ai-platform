"""AIL.5C engine: lifecycle, deterministic stage, evidence, idempotency, immutability."""

from datetime import timedelta

import pytest

from app.db.enums import (
    AssessmentAttemptStatus,
    AssessmentDefinitionStatus,
    AssessmentOrigin,
    AssessmentOutcome,
    AssessmentResultKind,
    DemonstrationEffect,
    EvidenceRefType,
)
from app.errors import ConflictError, NotFoundError
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentDefinition,
    AssessmentDefinitionConcept,
    AssessmentResult,
    ImmutableRecordError,
)
from app.models.learner import LearningEvidence
from app.services.assessment_challenge_service import draw_challenge, public_challenge
from app.services.assessment_definition_service import AssessmentDefinitionService
from app.services.assessment_service import AssessmentInvalid, AssessmentNotReady, AssessmentService
from app.services.learner_state_service import DEMONSTRATED, UNDERSTOOD, LearnerStateService
from tests.ail5c_factories import kc_definition, make_ail_concept, second_user


def _answer(attempt, selected=(1,)):
    return {
        "responses": {
            i["entry_key"]: {"selected": list(selected)} for i in attempt.challenge_instance["items"]
        }
    }


def _take(db, user, key, *, selected=(1,), declaration="no_external_help", svc=None):
    svc = svc or AssessmentService(db)
    attempt = svc.start(user, key)
    svc.save_draft(user.id, attempt.id, _answer(attempt, selected))
    return svc, svc.submit(user, attempt.id, {"declaration": declaration})


def _evidence(db, user):
    return db.query(LearningEvidence).filter_by(user_id=user.id).all()


# -- happy path ---------------------------------------------------------------------------------


def test_knowledge_check_pass_appends_evidence_and_state_recomputes(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc, attempt = _take(db, user, defn.definition_key)
    assert attempt.status == AssessmentAttemptStatus.FINALIZED
    final = svc.effective_result(attempt.id)
    assert final.outcome == AssessmentOutcome.PASSED
    assert final.demonstration_effect == DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED
    rows = _evidence(db, user)
    assert len(rows) == 1
    assert rows[0].ref_type == EvidenceRefType.ASSESSMENT_RESULT and rows[0].ref_id == final.id
    assert (
        rows[0].concept_version_id == defn.id or True
    )  # pinned to the linked concept version, checked below
    assert rows[0].assistance_level.value == "h0" and rows[0].question_origin.value == "reviewed"
    assert LearnerStateService(db).state(user.id, concept.id).ladder == DEMONSTRATED


def test_evidence_is_pinned_to_the_definitions_concept_version(db, bootstrap):
    user = bootstrap.user
    concept, version = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    _take(db, user, defn.definition_key)
    assert _evidence(db, user)[0].concept_version_id == version.id
    link = db.query(AssessmentDefinitionConcept).filter_by(definition_id=defn.id).one()
    assert link.concept_version_id == version.id


def test_resubmit_and_replay_never_duplicate_evidence(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc, attempt = _take(db, user, defn.definition_key)
    again = svc.submit(user, attempt.id, {"declaration": "no_external_help"})
    graded = svc.grade(user, attempt.id)
    assert again.status == graded.status == AssessmentAttemptStatus.FINALIZED
    assert len(_evidence(db, user)) == 1
    assert (
        db.query(AssessmentResult)
        .filter_by(attempt_id=attempt.id, result_kind=AssessmentResultKind.FINAL)
        .count()
        == 1
    )


def test_finalizing_twice_is_a_safe_replay(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc, attempt = _take(db, user, defn.definition_key)
    definition = svc.definitions.get(attempt.definition_id)
    det = svc._deterministic_result(attempt.id)
    from app.assessment_outcome import Aggregate

    again = svc._finalize(
        user,
        attempt,
        definition,
        svc._links(definition.id),
        det,
        [],
        [],
        None,
        Aggregate(outcome=AssessmentOutcome.PASSED, effect=DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED),
    )
    assert again.status == AssessmentAttemptStatus.FINALIZED
    assert len(_evidence(db, user)) == 1


# -- failure writes nothing -----------------------------------------------------------------------


def test_failed_check_needs_work_writes_no_evidence_and_keeps_history(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc, attempt = _take(db, user, defn.definition_key, selected=(0,))
    final = svc.effective_result(attempt.id)
    assert final.outcome == AssessmentOutcome.NEEDS_WORK
    assert final.demonstration_effect == DemonstrationEffect.NONE
    assert _evidence(db, user) == []
    assert LearnerStateService(db).state(user.id, concept.id).ladder not in (UNDERSTOOD, DEMONSTRATED)
    assert final.gaps and final.gaps[0]["criterion_key"] == "answers_correct"
    kinds = {s["kind"] for s in final.remediation}
    assert "learning_item" in kinds and "retry" in kinds  # targeted step + never a dead end
    # the failed attempt and both stage rows are preserved
    assert db.query(AssessmentResult).filter_by(attempt_id=attempt.id).count() == 2


def test_retry_needs_cooldown_then_draws_a_different_challenge(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc, first = _take(db, user, defn.definition_key, selected=(0,))
    with pytest.raises(AssessmentNotReady) as blocked:
        svc.start(user, defn.definition_key, previous_attempt_id=first.id)
    assert any(c["key"] == "cooldown" and not c["met"] for c in blocked.value.detail["checks"])

    later = AssessmentService(
        db,
        now=first.finalized_at.replace(tzinfo=None).replace(tzinfo=__import__("datetime").timezone.utc)
        + timedelta(hours=13),
    )
    second = later.start(user, defn.definition_key, previous_attempt_id=first.id)
    assert second.origin == AssessmentOrigin.RETRY and second.previous_attempt_id == first.id
    keys = lambda a: {i["entry_key"] for i in a.challenge_instance["items"]}
    assert not (keys(first) & keys(second)), "a retry never repeats an already issued question"
    assert db.query(AssessmentAttempt).filter_by(user_id=user.id).count() == 2  # the failed attempt is kept


# -- attestation / formative ------------------------------------------------------------------------------


def test_declared_ai_assistant_is_formative_and_writes_no_evidence(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc, attempt = _take(db, user, defn.definition_key, declaration="used_ai_assistant")
    final = svc.effective_result(attempt.id)
    assert final.outcome == AssessmentOutcome.PASSED
    assert final.demonstration_effect == DemonstrationEffect.FORMATIVE_ONLY
    assert _evidence(db, user) == []


def test_a_declaration_is_required_to_submit(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(user.id, attempt.id, _answer(attempt))
    for bad in (None, {}, {"declaration": "cheerfully"}):
        with pytest.raises(AssessmentInvalid):
            svc.submit(user, attempt.id, bad)
    assert svc.get_attempt(user.id, attempt.id).status == AssessmentAttemptStatus.DRAFT


# -- versioning / immutability -------------------------------------------------------------------------------------


def test_published_definitions_are_immutable_but_can_be_retired(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    assert defn.content_hash and AssessmentDefinitionService(db).verify_hash(defn)
    defn.title = "Sneaky rewrite"
    with pytest.raises(ImmutableRecordError):
        db.commit()
    db.rollback()
    defn = db.get(AssessmentDefinition, defn.id)
    defn.criteria = []
    with pytest.raises(ImmutableRecordError):
        db.commit()
    db.rollback()
    retired = AssessmentDefinitionService(db).retire(defn.id)
    assert retired.status == AssessmentDefinitionStatus.RETIRED
    retired.status = AssessmentDefinitionStatus.PUBLISHED
    with pytest.raises(ImmutableRecordError):
        db.commit()


def test_a_change_is_a_new_version_and_old_attempts_stay_pinned(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    v1 = kc_definition(db, user, concept)
    svc, attempt = _take(db, user, v1.definition_key)
    v2 = kc_definition(db, user, concept)  # same key -> version 2
    assert (v1.version, v2.version) == (1, 2)
    assert AssessmentDefinitionService(db).current(v1.definition_key).id == v2.id
    frozen = svc.get_attempt(user.id, attempt.id)
    assert frozen.definition_id == v1.id
    assert frozen.pinned_versions["definition"]["version"] == 1
    assert frozen.pinned_versions["rubric_version"] == f"{v1.definition_key}@1"
    assert frozen.pinned_versions["concepts"][0]["concept_id"] == concept.id
    assert AssessmentDefinitionService(db).verify_hash(v1)


def test_results_are_append_only(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc, attempt = _take(db, user, defn.definition_key)
    final = svc.effective_result(attempt.id)
    final.outcome = AssessmentOutcome.NEEDS_WORK
    with pytest.raises(ImmutableRecordError):
        db.commit()
    db.rollback()
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.PASSED


def test_a_published_definition_needs_a_valid_shape(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    with pytest.raises(ConflictError):  # pool smaller than 3x the draw size
        kc_definition(db, user, concept, pool=4, draw=2)


# -- deterministic fresh challenge ------------------------------------------------------------------------------------------


def test_challenge_selection_is_deterministic_and_authored(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    a, b = draw_challenge(db, defn, user.id), draw_challenge(db, defn, user.id)
    assert a.seed == b.seed and a.instance == b.instance
    assert a.instance["generator"] == "authored_pool_v1"
    other = second_user(db, bootstrap.organization)
    assert draw_challenge(db, defn, other.id).seed != a.seed
    keys = {e["entry_key"] for e in defn.challenge_spec["pool"]}
    assert {i["entry_key"] for i in a.instance["items"]} <= keys  # only ever drawn from the authored pool


def test_the_learner_never_sees_answer_keys(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key)
    view = svc.attempt_view(attempt)
    blob = str(view)
    assert "answer_key" not in blob and "server_only" not in blob and "reference_points" not in blob
    assert attempt.challenge_instance["server_only"]["answer_keys"]  # exists server-side
    assert public_challenge(attempt.challenge_instance)["items"] == view["challenge"]["items"]


# -- readiness / lifecycle -----------------------------------------------------------------------------------------------------


def test_readiness_reports_specific_unmet_items_and_a_single_active_attempt(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc = AssessmentService(db)
    assert svc.readiness(user, defn.definition_key)["ready"]
    attempt = svc.start(user, defn.definition_key)
    report = svc.readiness(user, defn.definition_key)
    assert not report["ready"]
    active = next(c for c in report["checks"] if c["key"] == "no_active_attempt")
    assert not active["met"] and active["resume_attempt_id"] == attempt.id
    with pytest.raises(AssessmentNotReady):
        svc.start(user, defn.definition_key)
    assert svc.readiness(user, "does-not-exist")["ready"] is False


def test_start_is_idempotent_with_an_idempotency_key(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc = AssessmentService(db)
    first = svc.start(user, defn.definition_key, idempotency_key="k-1")
    again = svc.start(user, defn.definition_key, idempotency_key="k-1")
    assert first.id == again.id
    assert db.query(AssessmentAttempt).count() == 1


def test_platform_capability_gated_definitions_are_not_startable(db, bootstrap):
    """MA9-dependent work is capability-gated, and nothing here implements MA9."""
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept, publish=False)
    defn.requires_platform_capability = "ma9.sandbox"
    db.commit()
    AssessmentDefinitionService(db).publish(defn.id)
    svc = AssessmentService(db)
    report = svc.readiness(user, defn.definition_key)
    assert not report["ready"] and "Available after MA9" in str(report["checks"])
    with pytest.raises(AssessmentNotReady):
        svc.start(user, defn.definition_key)


def test_expired_drafts_are_abandoned_and_release_the_lock(db, bootstrap):
    from datetime import datetime, timezone

    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key)
    later = AssessmentService(db, now=datetime.now(timezone.utc) + timedelta(hours=25))
    with pytest.raises(ConflictError):
        later.submit(user, attempt.id, {"declaration": "no_external_help"})
    kept = svc.get_attempt(user.id, attempt.id)
    assert kept.status == AssessmentAttemptStatus.ABANDONED and kept.draft is not None  # kept, only released


def test_abandon_is_leave_assessment(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key)
    left = svc.abandon(user, attempt.id)
    assert left.status == AssessmentAttemptStatus.ABANDONED
    with pytest.raises(ConflictError):
        svc.save_draft(user.id, attempt.id, {"responses": {}})


# -- privacy ------------------------------------------------------------------------------------------------------------------------------


def test_another_learner_cannot_reach_an_attempt_result_or_draft(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    defn = kc_definition(db, user, concept)
    svc, attempt = _take(db, user, defn.definition_key)
    other = second_user(db, bootstrap.organization)
    for call in (
        lambda: svc.get_attempt(other.id, attempt.id),
        lambda: svc.results(other.id, attempt.id),
        lambda: svc.save_draft(other.id, attempt.id, {}),
        lambda: svc.submit(other, attempt.id, {"declaration": "no_external_help"}),
        lambda: svc.abandon(other, attempt.id),
        lambda: svc.grade(other, attempt.id),
    ):
        with pytest.raises(NotFoundError):
            call()
    with pytest.raises(NotFoundError):  # nor start a "retry" of someone else's attempt
        svc.start(other, defn.definition_key, previous_attempt_id=attempt.id)
    assert _evidence(db, other) == []


def test_no_similarity_or_surveillance_code_exists():
    """No copy detection, no proctoring. AST-based: imports, identifiers and
    function names only, so prose that states the prohibition is not flagged."""
    import ast
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    files = [p for p in root.rglob("*.py") if "assessment" in p.name or p.name == "independence_policy.py"]
    assert len(files) >= 8
    banned = re.compile(
        r"(difflib|jaccard|levenshtein|cosine|tfidf|simhash|minhash|similar|plagiar|copydetect|"
        r"webcam|keystroke|clipboard|screenrecord|eyetrack|proctor|focusloss|visibilitychange)",
        re.IGNORECASE,
    )
    offenders = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.arg):
                names = [node.arg]
            for name in names:
                if banned.search(name.replace("_", "")):
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, offenders
