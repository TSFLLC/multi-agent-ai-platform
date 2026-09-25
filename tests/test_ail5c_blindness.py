"""AIL.5C: the Grader is blind to REAL Mentor / Professor / profile / history records, and a
challenge that is not authored is never credited as independence."""

from datetime import datetime, timedelta, timezone

from app.db.enums import (
    AssessmentOutcome,
    AssistanceLevel,
    DemonstrationEffect,
    EvidenceType,
    ExecutionMode,
    TaskStatus,
)
from app.models.academy import ExplainBackResponse, MilestoneAttempt, ProjectTemplateConceptLink
from app.models.learner import LearnerProfile, LearningEvidence
from app.models.tasks import Task
from app.services import assessment_challenge_service as challenges
from app.services import assessment_service as service_module
from app.services.assessment_service import AssessmentService
from app.services.system_project_service import ensure_ail_system_project
from tests.ail5c_factories import (
    ScriptedGrader,
    add_milestone_evidence,
    explain_definition,
    kc_definition,
    make_ail_concept,
    make_evaluation_run,
    make_owned_run,
    make_project_attempt,
    project_definition,
    second_user,
    setup_free_models,
)
from tests.test_ail5b_product_api import _template

REQ_KC_EB = {
    "requires_all": [
        {"evidence_type": "knowledge_check", "min_passed": 1},
        {"evidence_type": "explain_back", "min_passed": 1},
    ]
}


def test_the_grader_never_receives_mentor_professor_profile_prior_attempt_or_other_learner_content(
    db, bootstrap
):
    setup_free_models(db, count=2)
    user = bootstrap.user
    concept, _v = make_ail_concept(db, requirements=REQ_KC_EB)
    template, _m = _template(db, user.id)
    db.add(ProjectTemplateConceptLink(project_template_id=template.id, concept_id=concept.id, role="applied"))
    db.add(LearnerProfile(user_id=user.id, goal_text="PROFILE-GOAL-SECRET"))
    project = ensure_ail_system_project(db, user)
    db.add(
        Task(
            project_id=project.id,
            title="AI Professor: ASK",
            description="PROFESSOR-QUESTION-SECRET",
            created_by=user.id,
            requirements={"professor_context": {"question": "PROFESSOR-CONTEXT-SECRET"}},
            execution_mode=ExecutionMode.SINGLE_AGENT,
            status=TaskStatus.READY,
        )
    )
    pa = make_project_attempt(db, user, template, levels=(AssistanceLevel.H3,))
    milestone_attempt = db.query(MilestoneAttempt).filter_by(project_attempt_id=pa.id).first()
    db.add(
        ExplainBackResponse(
            user_id=user.id,
            project_attempt_id=pa.id,
            milestone_attempt_id=milestone_attempt.id,
            question="Mentor question text",
            response="MENTOR-EXPLAIN-SECRET",
            assistance_level=AssistanceLevel.H3,
        )
    )
    db.commit()

    # a PRIOR attempt of this learner, and another learner's in-progress work
    other_concept, _c = make_ail_concept(db, name="Other")
    prior_defn = kc_definition(db, user, other_concept, key="kc-prior")
    prior_svc = AssessmentService(db)
    prior = prior_svc.start(user, prior_defn.definition_key)
    prior_answers = {i["entry_key"]: {"selected": [0]} for i in prior.challenge_instance["items"]}
    prior_svc.save_draft(
        user.id, prior.id, {"responses": prior_answers, "fields": {"note": "PRIOR-ATTEMPT-SECRET"}}
    )
    prior_svc.submit(user, prior.id, {"declaration": "no_external_help"})
    stranger = second_user(db, bootstrap.organization, "stranger@example.com")
    stranger_defn = explain_definition(db, user, concept, key="eb-stranger")
    stranger_svc = AssessmentService(db)
    stranger_attempt = stranger_svc.start(stranger, stranger_defn.definition_key)
    stranger_svc.save_draft(
        stranger.id, stranger_attempt.id, {"fields": {"explanation": "OTHER-LEARNER-SECRET " * 5}}
    )

    defn = explain_definition(db, user, concept, key="eb-projectbound", template=template)
    adapter = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(user, defn.definition_key, project_attempt_id=pa.id)
    # the records ARE in the frozen manifest ...
    assert attempt.input_manifest["explain_back_ids"]
    assert attempt.input_manifest["source_work"]["levels"] == ["h3"]
    explanation = "A schema constrains the reply shape; it does not make the content true. " * 2
    svc.save_draft(user.id, attempt.id, {"fields": {"explanation": explanation}})
    svc.submit(user, attempt.id, {"declaration": "no_external_help"})

    # ... yet none of it ever reaches the Grader
    assert adapter.requests
    for request in adapter.requests:
        seen = (request.user_prompt + (request.system_prompt or "")).lower()
        for secret in (
            "PROFILE-GOAL-SECRET",
            "PROFESSOR-QUESTION-SECRET",
            "PROFESSOR-CONTEXT-SECRET",
            "MENTOR-EXPLAIN-SECRET",
            "Mentor question text",
            "PRIOR-ATTEMPT-SECRET",
            "OTHER-LEARNER-SECRET",
            user.email,
            "assistance",
            "h3",
        ):
            assert secret.lower() not in seen, secret


def test_a_challenge_from_a_non_authored_generator_is_never_credited_as_independence(
    db, bootstrap, monkeypatch
):
    """An AI-generated challenge cannot independently establish DEMONSTRATED."""
    user = bootstrap.user
    requirements = {"requires_all": [{"evidence_type": "project_assessment", "min_passed": 1}]}
    concept, version = make_ail_concept(db, requirements=requirements)
    template, _m = _template(db, user.id)
    db.add(ProjectTemplateConceptLink(project_template_id=template.id, concept_id=concept.id, role="applied"))
    db.commit()
    pa = make_project_attempt(db, user, template, levels=(AssistanceLevel.H5,), variant_after_h5=True)
    add_milestone_evidence(db, user, pa, concept, version)
    defn = project_definition(db, user, concept, template, key="proj-ai-challenge", challenge=True)

    real = challenges.draw_challenge

    def ai_generated(*args, **kwargs):
        draw = real(*args, **kwargs)
        return challenges.ChallengeDraw(
            seed=draw.seed, instance={**draw.instance, "generator": "ai_generated_v1"}
        )

    monkeypatch.setattr(service_module, "draw_challenge", ai_generated)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key, project_attempt_id=pa.id)
    assert attempt.challenge_instance["generator"] == "ai_generated_v1"
    needle = attempt.challenge_instance["parameters"]["input_text"]
    run = make_owned_run(
        db,
        user,
        project=ensure_ail_system_project(db, user),
        description=f"run {needle}",
        created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    evaluation = make_evaluation_run(db, user, ["met"])
    svc.save_draft(user.id, attempt.id, {"run_ids": [run.id], "evaluation_run_ids": [evaluation.id]})
    final = svc.effective_result(svc.submit(user, attempt.id, {"declaration": "no_external_help"}).id)

    assert final.outcome == AssessmentOutcome.PASSED  # the work itself is fine ...
    assert final.facts["independence"]["challenge_issued"] is False  # ... but the challenge is not credited
    assert (
        final.demonstration_effect == DemonstrationEffect.FORMATIVE_ONLY
    )  # H5 source + uncredited challenge
    project_rows = db.query(LearningEvidence).filter_by(
        user_id=user.id, evidence_type=EvidenceType.PROJECT_ASSESSMENT
    )
    assert project_rows.count() == 0


def test_only_authored_generators_are_credited():
    assert challenges.is_authored_challenge({"generator": "authored_pool_v1"})
    assert not challenges.is_authored_challenge({"generator": "ai_generated_v1"})
    assert not challenges.is_authored_challenge({})
    assert not challenges.is_authored_challenge(None)
