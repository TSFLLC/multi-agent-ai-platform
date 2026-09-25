"""AIL.5C independence, fresh challenges, project assessment, Personal Lab, MA6 and Assessment Mode."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.enums import (
    AssessmentAttemptStatus,
    AssessmentOutcome,
    AssistanceLevel,
    DemonstrationEffect,
    EvidenceType,
    ExecutionVerification,
    GradingMode,
    ProjectAttemptStatus,
)
from app.errors import ConflictError
from app.models.academy import AssessmentReadySubmission, MilestoneAttempt, ProjectAttempt
from app.models.evaluation_runs import EvaluationRun
from app.models.learner import LearningEvidence
from app.services.assessment_mode_guard import AssessmentModeActive, AssessmentModeGuard
from app.services.assessment_service import AssessmentNotReady, AssessmentService
from app.services.learner_state_service import DEMONSTRATED, PRACTICED, LearnerStateService
from app.services.project_mentor_service import ProjectMentorService
from tests.ail5c_factories import (
    REQ_MOD,
    ScriptedGrader,
    add_milestone_evidence,
    default_findings,
    judgment,
    kc_definition,
    make_ail_concept,
    make_evaluation_run,
    make_experiment,
    make_owned_run,
    make_project_attempt,
    modification_definition,
    project_definition,
    second_user,
    setup_free_models,
)
from tests.test_ail5b_product_api import _template

H = AssistanceLevel
REQ_PROJECT = {"requires_all": [{"evidence_type": "project_assessment", "min_passed": 1}]}


def _evidence(db, user):
    return db.query(LearningEvidence).filter_by(user_id=user.id).all()


def _project(db, bootstrap, *, levels=(H.H1,), fresh="if_assisted", challenge=False, study=False, evidence_verified=True, variant=False):
    from app.models.academy import ProjectTemplateConceptLink

    user = bootstrap.user
    concept, version = make_ail_concept(db, requirements=REQ_PROJECT)
    template, _milestone = _template(db, user.id)
    db.add(ProjectTemplateConceptLink(project_template_id=template.id, concept_id=concept.id, role="applied"))
    db.commit()
    pa = make_project_attempt(db, user, template, levels=levels, study=study, variant_after_h5=variant)
    add_milestone_evidence(db, user, pa, concept, version, verified=evidence_verified)
    defn = project_definition(db, user, concept, template, fresh=fresh, challenge=challenge)
    return user, concept, template, pa, defn


def _submit_project(db, user, defn, pa, *, runs=None, evaluation=None, declaration="no_external_help"):
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key, project_attempt_id=pa.id)
    evaluation = evaluation or make_evaluation_run(db, user, ["met", "met"])
    svc.save_draft(user.id, attempt.id, {"evaluation_run_ids": [evaluation.id], "run_ids": runs or [], "fields": {}})
    return svc, svc.submit(user, attempt.id, {"declaration": declaration})


# -- independent source work ---------------------------------------------------------------------------------


def test_independent_verified_project_work_demonstrates_and_finalizes_the_5b_records(db, bootstrap):
    user, concept, _t, pa, defn = _project(db, bootstrap, levels=(H.H0, H.H1))
    svc, attempt = _submit_project(db, user, defn, pa)
    final = svc.effective_result(attempt.id)
    assert final.outcome == AssessmentOutcome.PASSED
    assert final.demonstration_effect == DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED
    row = _evidence(db, user)[-1]
    assert row.evidence_type == EvidenceType.PROJECT_ASSESSMENT
    assert row.execution_verification == ExecutionVerification.PLATFORM_VERIFIED and row.assistance_level == H.H1
    assert LearnerStateService(db).state(user.id, concept.id).ladder == DEMONSTRATED
    # 5C (and only 5C) writes 5B's project status and finalizes the hand-off
    assert db.get(ProjectAttempt, pa.id).status == ProjectAttemptStatus.PASSED
    assert db.query(AssessmentReadySubmission).filter_by(project_attempt_id=pa.id).one().finalized_at is not None


def test_the_5b_handoff_is_consumed_as_a_frozen_copy(db, bootstrap):
    user, _c, _t, pa, defn = _project(db, bootstrap)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key, project_attempt_id=pa.id)
    manifest = attempt.input_manifest
    assert manifest["source_submission"]["id"] and manifest["project"]["project_attempt_id"] == pa.id
    assert len(manifest["milestone_attempts"]) == 1 and manifest["source_work"]["levels"] == ["h1"]
    frozen_hash = attempt.input_manifest_hash
    # 5B later overwrites its snapshot; the frozen attempt is unaffected
    row = db.query(AssessmentReadySubmission).filter_by(project_attempt_id=pa.id).one()
    row.snapshot = {"tampered": True}
    db.commit()
    again = svc.get_attempt(user.id, attempt.id)
    assert again.input_manifest_hash == frozen_hash and again.input_manifest == manifest


def test_5b_candidate_claims_are_not_facts(db, bootstrap):
    """A candidate row that CLAIMS platform_verified proves nothing: without a cited
    platform record the project cannot pass."""
    from app.models.academy import CandidateEvidence

    user, concept, _t, pa, defn = _project(db, bootstrap)
    db.add(CandidateEvidence(user_id=user.id, project_attempt_id=pa.id, concept_id=concept.id, source_type="manual",
                             evidence_type="lab", passed=True, execution_verification=ExecutionVerification.PLATFORM_VERIFIED))
    db.commit()
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key, project_attempt_id=pa.id)
    svc.save_draft(user.id, attempt.id, {"evaluation_run_ids": [], "fields": {}})
    final = svc.effective_result(svc.submit(user, attempt.id, {"declaration": "no_external_help"}).id)
    assert final.outcome == AssessmentOutcome.NEEDS_WORK and _evidence(db, user)[-1:] == _evidence(db, user)[-1:]
    assert not [e for e in _evidence(db, user) if e.evidence_type == EvidenceType.PROJECT_ASSESSMENT]


def test_another_learners_evaluation_run_cannot_be_cited(db, bootstrap):
    user, _c, _t, pa, defn = _project(db, bootstrap)
    other = second_user(db, bootstrap.organization)
    foreign = make_evaluation_run(db, other, ["met"])
    svc, attempt = _submit_project(db, user, defn, pa, evaluation=foreign)
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.NEEDS_WORK
    assert not [e for e in _evidence(db, user) if e.evidence_type == EvidenceType.PROJECT_ASSESSMENT]


def test_ma6_evaluation_runs_are_read_only_facts(db, bootstrap):
    user, _c, _t, pa, defn = _project(db, bootstrap)
    evaluation = make_evaluation_run(db, user, ["met", "met"])
    snapshot = lambda: [(r.id, r.status, r.subject_artifact_content_hash, r.requested_by_user_id, r.method,
                         [(c.criterion_key, c.finding, c.rationale) for c in r.criterion_results]) for r in db.query(EvaluationRun).all()]
    before = snapshot()
    _submit_project(db, user, defn, pa, evaluation=evaluation)
    assert snapshot() == before  # not one MA6 row was touched or added
    assert db.query(EvaluationRun).count() == len(before)


# -- assisted source work: H3 partial, H4 formative, H5 never ------------------------------------------------------------


def test_h5_source_work_needs_a_fresh_independent_challenge_and_alone_cannot_demonstrate(db, bootstrap):
    user, concept, template, pa, _n = _project(db, bootstrap, levels=(H.H5,))
    no_pool = project_definition(db, user, concept, template, key="proj-nopool", challenge=False)
    svc = AssessmentService(db)
    report = svc.readiness(user, no_pool.definition_key, project_attempt_id=pa.id)
    assert report["fresh_required"] and report["fresh_reason"] == "source_work_substantially_assisted"
    with pytest.raises(AssessmentNotReady):  # the source project alone can never demonstrate: no fresh challenge, no attempt
        svc.start(user, no_pool.definition_key, project_attempt_id=pa.id)

    fresh = project_definition(db, user, concept, template, key="proj-fresh", challenge=True)
    attempt = svc.start(user, fresh.definition_key, project_attempt_id=pa.id)
    assert attempt.fresh_required and attempt.challenge_instance["items"]


def test_h5_source_work_with_a_fresh_challenge_qualifies_at_assessment_mode_independence(db, bootstrap):
    user, concept, template, pa, _n = _project(db, bootstrap, levels=(H.H5,), variant=True)
    defn = project_definition(db, user, concept, template, key="proj-fresh", challenge=True)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key, project_attempt_id=pa.id)
    needle = attempt.challenge_instance["parameters"]["input_text"]
    run = make_owned_run(db, user, project=_project_of(db, bootstrap), description=f"Run on {needle}")
    svc.save_draft(user.id, attempt.id, {"evaluation_run_ids": [make_evaluation_run(db, user, ["met"]).id], "run_ids": [run.id]})
    done = svc.submit(user, attempt.id, {"declaration": "no_external_help"})
    final = svc.effective_result(done.id)
    assert final.outcome == AssessmentOutcome.PASSED
    assert final.demonstration_effect == DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED
    row = [e for e in _evidence(db, user) if e.evidence_type == EvidenceType.PROJECT_ASSESSMENT][-1]
    assert row.assistance_level == H.H0  # Assessment Mode: the Mentor was locked
    assert "h5" in final.facts["independence"]["source_levels"]  # the H5 history stays visible
    assert LearnerStateService(db).state(user.id, concept.id).ladder == DEMONSTRATED


def test_h3_source_work_counts_toward_practiced_only(db, bootstrap):
    user, concept, template, pa, _n = _project(db, bootstrap, levels=(H.H3,))
    defn = project_definition(db, user, concept, template, key="proj-never", fresh="never")
    svc, attempt = _submit_project(db, user, defn, pa)
    final = svc.effective_result(attempt.id)
    assert final.outcome == AssessmentOutcome.PASSED
    assert final.demonstration_effect == DemonstrationEffect.COUNTS_TOWARD_PRACTICED_ONLY
    project_rows = [e for e in _evidence(db, user) if e.evidence_type == EvidenceType.PROJECT_ASSESSMENT]
    assert project_rows and project_rows[0].assistance_level == H.H3
    assert LearnerStateService(db).state(user.id, concept.id).ladder == PRACTICED


def test_h5_only_passing_project_is_formative_and_writes_no_evidence(db, bootstrap):
    user, concept, template, pa, _n = _project(db, bootstrap, levels=(H.H5,), variant=True)
    add_count = len(_evidence(db, user))
    defn = project_definition(db, user, concept, template, key="proj-never", fresh="never")
    svc, attempt = _submit_project(db, user, defn, pa)
    final = svc.effective_result(attempt.id)
    assert final.outcome == AssessmentOutcome.PASSED and final.demonstration_effect == DemonstrationEffect.FORMATIVE_ONLY
    assert len(_evidence(db, user)) == add_count
    assert LearnerStateService(db).state(user.id, concept.id).ladder != DEMONSTRATED


def test_study_mode_alone_never_demonstrates_and_forces_a_fresh_challenge(db, bootstrap):
    user, concept, template, pa, defn = _project(db, bootstrap, levels=(H.H0,), study=True)
    report = AssessmentService(db).readiness(user, defn.definition_key, project_attempt_id=pa.id)
    assert report["fresh_required"] and report["fresh_reason"] == "study_mode_used"
    with pytest.raises(AssessmentNotReady):
        AssessmentService(db).start(user, defn.definition_key, project_attempt_id=pa.id)


def test_a_fresh_challenge_must_be_bound_to_a_required_criterion(db, bootstrap):
    from app.services.assessment_definition_service import AssessmentDefinitionService
    from app.db.enums import AssessmentKind

    user = bootstrap.user
    concept, _v = make_ail_concept(db)
    criteria = [{"key": "frozen", "label": "Unchanged", "method": "deterministic", "required": True, "check": {"type": "manifest_unchanged"}}]
    draft = AssessmentDefinitionService(db).create_draft(
        author_user_id=user.id, definition_key="rubber-stamp", kind=AssessmentKind.PROJECT, title="x", instructions_md="x",
        criteria=criteria, concept_links=[{"concept_id": concept.id, "criterion_keys": ["frozen"]}],
        challenge_spec={"entry_kind": "variant", "draw_size": 1, "pool": [{"entry_key": f"v{i}", "title": "t", "statement_md": "s"} for i in range(3)]},
    )
    with pytest.raises(ConflictError):
        AssessmentDefinitionService(db).publish(draft.id)


def _project_of(db, bootstrap):
    from app.services.system_project_service import ensure_ail_system_project

    return ensure_ail_system_project(db, bootstrap.user)


# -- modification challenge (platform-verified runs) ---------------------------------------------------------------------------


def _mod(db, bootstrap):
    user = bootstrap.user
    concept, version = make_ail_concept(db, requirements=REQ_MOD)
    defn = modification_definition(db, user, concept)
    return user, concept, defn, _project_of(db, bootstrap)


def _attempt_mod(db, user, defn, project, *, describe=True, when="after", owner=None, status=None):
    from app.db.enums import AgentRunStatus

    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key)
    needle = attempt.challenge_instance["parameters"]["input_text"]
    created = datetime.now(timezone.utc) + (timedelta(minutes=5) if when == "after" else -timedelta(days=1))
    run = make_owned_run(db, owner or user, project=project, description=f"extract from {needle}" if describe else "something else",
                         created_at=created, **({"status": status} if status else {}))
    svc.save_draft(user.id, attempt.id, {"run_ids": [run.id]})
    return svc, svc.submit(user, attempt.id, {"declaration": "no_external_help"}), run


def test_a_verified_fresh_modification_passes_and_feeds_learner_state(db, bootstrap):
    user, concept, defn, project = _mod(db, bootstrap)
    # first the knowledge-check leg, then the modification leg
    kc_definition(db, user, concept, key="kc-leg")
    svc0 = AssessmentService(db)
    a0 = svc0.start(user, "kc-leg")
    svc0.save_draft(user.id, a0.id, {"responses": {i["entry_key"]: {"selected": [1]} for i in a0.challenge_instance["items"]}})
    svc0.submit(user, a0.id, {"declaration": "no_external_help"})
    assert LearnerStateService(db).state(user.id, concept.id).ladder != DEMONSTRATED  # the modification leg is still missing

    svc, attempt, _run = _attempt_mod(db, user, defn, project)
    final = svc.effective_result(attempt.id)
    assert final.outcome == AssessmentOutcome.PASSED and final.demonstration_effect == DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED
    row = [e for e in _evidence(db, user) if e.evidence_type == EvidenceType.MODIFICATION][0]
    assert row.execution_verification == ExecutionVerification.PLATFORM_VERIFIED and row.assistance_level == H.H0
    assert LearnerStateService(db).state(user.id, concept.id).ladder == DEMONSTRATED  # via the EXISTING service


@pytest.mark.parametrize("kw", [{"when": "before"}, {"describe": False}])
def test_modification_checks_reject_stale_or_unrelated_runs(db, bootstrap, kw):
    user, _c, defn, project = _mod(db, bootstrap)
    svc, attempt, _run = _attempt_mod(db, user, defn, project, **kw)
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.NEEDS_WORK
    assert _evidence(db, user) == []


def test_modification_rejects_another_learners_run_and_unfinished_runs(db, bootstrap):
    from app.db.enums import AgentRunStatus

    user, _c, defn, project = _mod(db, bootstrap)
    other = second_user(db, bootstrap.organization)
    svc, attempt, _ = _attempt_mod(db, user, defn, project, owner=other)
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.NEEDS_WORK
    svc = AssessmentService(db, now=datetime.now(timezone.utc) + timedelta(hours=13))  # past the retry cooldown
    attempt = svc.start(user, defn.definition_key, previous_attempt_id=attempt.id)
    needle = attempt.challenge_instance["parameters"]["input_text"]
    run = make_owned_run(db, user, project=project, description=f"extract from {needle}",
                         created_at=datetime.now(timezone.utc) + timedelta(hours=14), status=AgentRunStatus.FAILED)
    svc.save_draft(user.id, attempt.id, {"run_ids": [run.id]})
    attempt = svc.submit(user, attempt.id, {"declaration": "no_external_help"})
    assert svc.effective_result(attempt.id).outcome == AssessmentOutcome.NEEDS_WORK
    assert _evidence(db, user) == []


# -- Personal Lab: three layers stay distinct ------------------------------------------------------------------------------------------


def _interpretation(db, bootstrap):
    from app.db.enums import AssessmentKind
    from app.services.assessment_definition_service import AssessmentDefinitionService

    setup_free_models(db, count=2)
    user = bootstrap.user
    concept, _ = make_ail_concept(db, requirements={"requires_all": [
        {"evidence_type": "knowledge_check", "min_passed": 1}, {"evidence_type": "interpretation", "min_passed": 1}]})
    criteria = [
        {"key": "concluded", "label": "You concluded your experiment", "method": "deterministic", "required": True,
         "check": {"type": "experiment_concluded", "field": "experiment_id", "min_chars": 5}},
        {"key": "claim_right", "label": "Your claim matches the recorded results", "method": "deterministic", "required": True,
         "check": {"type": "experiment_claim", "field": "experiment_id", "claim_field": "fields.claim_best"}},
        {"key": "faithful", "label": "Your reasoning is faithful and appropriately hedged", "method": "grader", "required": True,
         "reference_points": ["Notes that a small sample limits the claim"], "facts": ["claim_right"]},
    ]
    defn = AssessmentDefinitionService(db).create_draft(
        author_user_id=user.id, definition_key="interp-model", kind=AssessmentKind.EXPERIMENT_INTERPRETATION, title="Read your experiment",
        instructions_md="Interpret your experiment.", criteria=criteria,
        concept_links=[{"concept_id": concept.id, "criterion_keys": ["concluded", "claim_right", "faithful"]}],
        challenge_spec={"entry_kind": "prompt", "draw_size": 1, "pool": [{"entry_key": f"i{n}", "prompt_md": "Explain your result.", "reference_points": ["hedging"]} for n in range(3)]},
    )
    AssessmentDefinitionService(db).publish(defn.id)
    return user, concept, defn


def test_experiment_result_learner_conclusion_and_grader_judgment_stay_distinct(db, bootstrap):
    user, _c, defn = _interpretation(db, bootstrap)
    experiment = make_experiment(db, user, labels={"A": ["met", "met"], "B": ["met", "not_met"]})
    db.refresh(experiment)  # read back as stored (SQLite drops tzinfo), then compare like with like
    before = (experiment.conclusion_text, experiment.conclusion_type, experiment.status, experiment.concluded_at)
    adapter = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(user.id, attempt.id, {"experiment_id": experiment.id, "fields": {"claim_best": "A", "explanation": "A passed more cases, though the sample was small."}})
    done = svc.submit(user, attempt.id, {"declaration": "no_external_help"})
    final = svc.effective_result(done.id)
    assert final.outcome == AssessmentOutcome.PASSED

    prompt = adapter.requests[0].user_prompt
    assert "PLATFORM OBSERVATION (experiment result)" in prompt and "LEARNER CONCLUSION (human interpretation)" in prompt
    assert prompt.index("PLATFORM OBSERVATION") < prompt.index("LEARNER CONCLUSION")
    assert prompt.index("LEARNER CONCLUSION") < prompt.index("A passed more cases")
    report = final.report
    assert report["platform_fact"]["deterministic_checks"] and report["grader_judgment"]["ran"] and report["learner_reflection"]
    # the experiment (platform observation + the learner's human conclusion) is untouched
    db.refresh(experiment)
    assert (experiment.conclusion_text, experiment.conclusion_type, experiment.status, experiment.concluded_at) == before
    # and the judgment lives in its own rows, never written over the conclusion
    assert final.criteria and {c["method"] for c in final.criteria} == {"deterministic", "grader"}


def test_a_claim_that_contradicts_the_recorded_results_needs_work(db, bootstrap):
    user, _c, defn = _interpretation(db, bootstrap)
    experiment = make_experiment(db, user, labels={"A": ["met", "met"], "B": ["met", "not_met"]})
    adapter = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(user.id, attempt.id, {"experiment_id": experiment.id, "fields": {"claim_best": "B", "explanation": "B was better."}})
    final = svc.effective_result(svc.submit(user, attempt.id, {"declaration": "no_external_help"}).id)
    assert final.outcome == AssessmentOutcome.NEEDS_WORK and adapter.requests == []  # a platform fact cannot be argued away


def test_another_learners_experiment_is_not_assessable(db, bootstrap):
    user, _c, defn = _interpretation(db, bootstrap)
    other = second_user(db, bootstrap.organization)
    theirs = make_experiment(db, other, labels={"A": ["met"], "B": ["not_met"]})
    adapter = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(user.id, attempt.id, {"experiment_id": theirs.id, "fields": {"claim_best": "A", "explanation": "x"}})
    final = svc.effective_result(svc.submit(user, attempt.id, {"declaration": "no_external_help"}).id)
    assert final.outcome == AssessmentOutcome.NEEDS_WORK and adapter.requests == []


# -- Assessment Mode ----------------------------------------------------------------------------------------------------------------------------


def test_assessment_mode_locks_the_mentor_and_professor_without_touching_history(db, bootstrap):
    from app.schemas.professor import ProfessorContextRequest, ProfessorIntent, ProfessorTarget, ProfessorTargetType
    from app.services.professor_execution_service import ProfessorExecutionService

    user, concept, template, pa, defn = _project(db, bootstrap)
    other_concept, _ = make_ail_concept(db, name="Unrelated")
    svc = AssessmentService(db)
    ms = db.query(MilestoneAttempt).filter_by(project_attempt_id=pa.id).first()
    history = db.query(MilestoneAttempt).count()

    kc = kc_definition(db, user, concept, key="kc-lock")
    attempt = svc.start(user, kc.definition_key)  # Assessment Mode begins
    guard = AssessmentModeGuard(db)
    assert guard.active_locks(user.id)[0]["attempt_id"] == attempt.id

    with pytest.raises(AssessmentModeActive):
        ProjectMentorService(db).ask(user, pa.id, ms.project_milestone_id, "help?", H.H2)
    with pytest.raises(AssessmentModeActive):
        ProfessorExecutionService(db)._assert_not_in_assessment_mode(
            user, ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=concept.id)))
    with pytest.raises(AssessmentModeActive):  # an untargeted question would sidestep the lock
        ProfessorExecutionService(db)._assert_not_in_assessment_mode(user, ProfessorContextRequest(intent=ProfessorIntent.ASK_PROFESSOR, question="hint?"))
    # an unrelated Concept is not the assessed one
    ProfessorExecutionService(db)._assert_not_in_assessment_mode(
        user, ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=other_concept.id)))
    assert db.query(MilestoneAttempt).count() == history  # Mentor history is untouched

    svc.abandon(user, attempt.id)  # "Leave assessment"
    assert guard.active_locks(user.id) == []
    ProfessorExecutionService(db)._assert_not_in_assessment_mode(user, ProfessorContextRequest(intent=ProfessorIntent.ASK_PROFESSOR, question="hint?"))
    with pytest.raises(ConflictError) as ex:  # the Mentor is reachable again (H2 simply is not unlocked yet)
        ProjectMentorService(db).ask(user, pa.id, ms.project_milestone_id, "help?", H.H2)
    assert not isinstance(ex.value, AssessmentModeActive)


def test_the_lock_is_learner_scoped(db, bootstrap):
    user = bootstrap.user
    concept, _ = make_ail_concept(db)
    kc = kc_definition(db, user, concept)
    AssessmentService(db).start(user, kc.definition_key)
    other = second_user(db, bootstrap.organization)
    AssessmentModeGuard(db).assert_professor_available(other.id, untargeted=True)  # someone else's assessment locks nothing
    assert AssessmentModeGuard(db).active_locks(other.id) == []


def test_hint_and_study_routes_are_locked_during_assessment_mode(client, db, bootstrap, auth_headers):
    user, concept, template, pa, defn = _project(db, bootstrap)
    kc = kc_definition(db, user, concept, key="kc-lock2")
    AssessmentService(db).start(user, kc.definition_key)
    ms = db.query(MilestoneAttempt).filter_by(project_attempt_id=pa.id).first()
    base = f"/academy/build-with-me/attempts/{pa.id}/milestones/{ms.project_milestone_id}"
    for path in ("/hint?hint_level=h1", "/study-mode"):
        response = client.post(base + path, headers=auth_headers)
        assert response.status_code == 409 and response.json()["error"]["code"] == "assessment_mode_active"


def test_no_ma9_runtime_or_sandbox_is_imported_by_assessment_code():
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    banned = ("sandbox_manager", "tool_bus", "mcp", "ma9")
    offenders = []
    for path in [p for p in root.rglob("*.py") if "assessment" in p.name or p.name == "independence_policy.py"]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module or ""] + [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else [])
            offenders += [f"{path.name}:{m}" for m in mods if any(b in m.lower() for b in banned)]
    assert not offenders, offenders
