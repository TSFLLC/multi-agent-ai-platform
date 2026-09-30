"""Level 1 curriculum orchestration over the existing AIL.5 engines."""

from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.db.enums import VersionStatus, IdempotencyScope
from app.errors import ConflictError, NotFoundError
from app.models.agents import Agent, AgentVersion
from app.models.identity import ProjectMembership, User
from app.models.academy import MilestoneAttempt, ProjectAttempt, ProjectMilestone, ProjectTemplate
from app.models.lab import EvalSetVersion, Experiment
from app.schemas.academy_level1 import (
    Level1AssessmentDraftRequest, Level1AssessmentStartRequest, Level1AssessmentSubmitRequest,
    Level1DayRead, Level1EvidenceRead, Level1GraduationRead, Level1ItemRead, Level1LabRead,
    Level1KnowledgeCheckRequest, Level1ReviewRead, Level1LabLaunchRequest, Level1LabResultRequest,
    Level1AssistanceRequest, Level1LearningRead, Level1RevealRead, Level1StepProgressRead, Level1StepResponseRead,
    Level1StepProfessorRequest, Level1StepResponseRequest, Level1StepsRead,
)
from app.schemas.lab import ExperimentCreate
from app.services.academy_level1_service import (
    AcademyLevel1Service, CAPSTONE_STAGES, LEVEL1_CAPSTONE_TEMPLATE_KEY,
    ensure_level1_capstone_template,
)
from app.academy_steps import strip_private
from app.schemas.professor import ProfessorContextRequest, ProfessorIntent, ProfessorInteractionRead, ProfessorTarget, ProfessorTargetType
from app.services.professor_execution_service import ProfessorExecutionService
from app.services.academy_structured_authoring import author_day_structure
from app.services.academy_step_service import AcademyStepService
from app.services.assessment_service import AssessmentService
from app.services.experiment_learning_qualification_service import ExperimentLearningQualificationService
from app.services.lab_service import LabService
from app.services.build_with_me_service import ProjectAttemptService, ProjectTemplateService
from app.services.learner_state_service import LearnerStateService
from app.services.academy_level1_fixtures import (
    ensure_day10, ensure_day14, ensure_day15, ensure_day19, ensure_day20, qualify_agent_fixture, qualify_workflow_fixture,
)
from app.services.task_service import TaskService
from app.services.workflow_execution_service import WorkflowExecutionService
from app.services.idempotency_service import IdempotencyService
from app.repositories.idempotency_repository import BeginOutcome

router = APIRouter(prefix="/academy/level-1", tags=["academy-level-1"])


def _public_item(item) -> Level1ItemRead:
    # AIL.5D.1: structured steps may carry server-only ``private`` content (reveals, answer keys); strip it here.
    spec = dict(strip_private(item.spec or {}))
    spec["knowledge_check"] = [
        {key: value for key, value in question.items() if key not in ("answer", "explanation")}
        for question in spec.get("knowledge_check", [])
    ]
    return Level1ItemRead(
        id=item.id, day=spec["day"], week=spec["week"], title=item.title,
        kind=spec["kind"], estimated_minutes=item.est_minutes, body_md=item.body_md,
        spec=spec, concept_id=item.concept_id, concept_slug=item.concept.slug,
        capability_boundary=spec.get("capability_boundary"), capstone_stage=spec.get("capstone_stage"),
    )


@router.post("/provision")
def provision(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    key = f"academy-level1-provision:{user.id}:v2"
    idem = IdempotencyService(db)
    begun = idem.begin(key, scope=IdempotencyScope.API_REQUEST, resource_type="academy_level1_provision")
    if begun.outcome == BeginOutcome.ALREADY_COMPLETED:
        # A completed request is not proof that the durable curriculum is
        # complete: an earlier create-only implementation could have marked
        # this key complete while leaving stale V2 rows.  Revalidate the
        # authored contract and converge only when it is incomplete.
        service = AcademyLevel1Service(db)
        try:
            service.validate_level1_contract()
        except ConflictError:
            service.provision(user)
        return begun.key_row.result_ref or {"created": 0, "total": 30, "canonical_concepts": 28}
    try:
        created = AcademyLevel1Service(db).provision(user)
        result = {"created": len(created), "total": 30, "canonical_concepts": 28}
        idem.complete(key, result_ref=result)
        return result
    except Exception:
        idem.fail(key)
        raise


@router.get("/days", response_model=List[Level1DayRead])
def days(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AcademyLevel1Service(db).days(user.id)


@router.get("/days/{day}", response_model=Level1ItemRead)
def day(day: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = [row for row in AcademyLevel1Service(db).days(user.id) if row["day"] == day]
    if not rows:
        raise NotFoundError("Academy Level 1 day is not provisioned")
    return _public_item(AcademyLevel1Service(db).item(rows[0]["item_id"]))


@router.post("/items/{item_id}/open", response_model=Level1EvidenceRead)
def open_item(item_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AcademyLevel1Service(db)
    evidence = service.expose(user.id, item_id)
    if evidence is None:
        # AIL.5D.2: a structured Day is only opened. No evidence is written here, ever.
        item = service.item(item_id)
        ladder = LearnerStateService(db).state(user.id, item.concept_id).ladder
        return {"evidence_id": None, "concept_id": item.concept_id, "learner_state": ladder, "passed": None, "opened": True, "evidence_recorded": False}
    state = LearnerStateService(db).state(user.id, evidence.concept_id)
    return {"evidence_id": evidence.id, "concept_id": evidence.concept_id, "learner_state": state.ladder, "passed": evidence.passed}


@router.post("/items/{item_id}/knowledge-check", response_model=Level1EvidenceRead)
def knowledge_check(item_id: str, body: Level1KnowledgeCheckRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AcademyLevel1Service(db)
    evidence, results, state = service.knowledge_check(user.id, item_id, body.answers)
    feedback = AcademyStepService(db).check_feedback(user.id, service.item(item_id), bool(evidence.passed), results)
    return {"evidence_id": evidence.id, "concept_id": evidence.concept_id, "learner_state": state.ladder, "passed": evidence.passed, "results": results, "feedback": feedback}


@router.post("/days/{day}/start-lab", response_model=Level1LabRead)
def start_lab(day: int, body: Optional[Level1LabLaunchRequest] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AcademyLevel1Service(db)
    rows = [row for row in service.days(user.id) if row["day"] == day and row["kind"] == "lab"]
    if not rows:
        raise NotFoundError("This Academy day is not a Personal Lab day")
    item = service.item(rows[0]["item_id"])
    context = {"academy_program": "practical-ai-foundations", "academy_week": item.spec["week"], "academy_day": day, "academy_title": item.title, "academy_learning_item_id": item.id, "academy_learning_objective": item.spec["objectives"][0], "return_to": f"#/academy/level-1/{day}", "assistance_level": (body.assistance_level if body else None), "capability_boundary": item.spec.get("capability_boundary")}
    if day == 10:
        fixture = ensure_day10(db, user, item)
        run = TaskService(db).start_task_run(task_id=fixture["task"].id, agent_version_id=fixture["agent_version"].id, frozen_task_snapshot={**context, "fixture_key": fixture["fixture_key"], "fixture_version": 1, "input_text": body.input_text if body else None})
        agent_run = run.agent_runs[0]
        return {**context, "title": item.title, "learning_objective": item.spec["objectives"][0], "experiment_id": None, "engine": "agent", "fixture_key": fixture["fixture_key"], "task_id": fixture["task"].id, "task_run_id": run.id, "agent_version_id": fixture["agent_version"].id, "agent_run_id": agent_run.id, "instructions": "Run the controlled structured extractor against one authored example, then submit the Day 10 explain-back."}
    if day == 14:
        fixture = ensure_day14(db, user, item)
        attempt = db.execute(select(ProjectAttempt).where(ProjectAttempt.user_id == user.id, ProjectAttempt.project_template_id == fixture["template"].id, ProjectAttempt.status == "active")).scalars().first()
        if attempt is None:
            attempt_id = ProjectAttemptService.create_attempt(db, user.id, fixture["template"].id)
            attempt = db.get(ProjectAttempt, attempt_id)
        run = TaskService(db).start_task_run(task_id=fixture["task"].id, agent_version_id=fixture["agent_version"].id, frozen_task_snapshot={**context, "fixture_key": fixture["fixture_key"], "project_attempt_id": attempt.id})
        return {**context, "title": item.title, "learning_objective": item.spec["objectives"][0], "experiment_id": None, "engine": "build_with_me_agent", "fixture_key": fixture["fixture_key"], "project_template_id": fixture["template"].id, "project_attempt_id": attempt.id, "task_id": fixture["task"].id, "task_run_id": run.id, "agent_version_id": fixture["agent_version"].id, "agent_run_id": run.agent_runs[0].id, "instructions": "Progress the existing Build With Me milestones, execute the controlled Agent, test it, and submit the authored application reflection."}
    if day == 15:
        fixture = ensure_day15(db, user, item)
        run = TaskService(db).start_task_run(task_id=fixture["task"].id, agent_version_id=fixture["grounded_agent_version"].id, frozen_task_snapshot={**context, "fixture_key": fixture["fixture_key"], "fixture_version": 1, "not_vector_rag": True, "grounded_agent_version_id": fixture["grounded_agent_version"].id, "comparison_agent_version_id": fixture["ungrounded_agent_version"].id})
        return {**context, "title": item.title, "learning_objective": item.spec["objectives"][0], "experiment_id": None, "engine": "bounded_grounding_agent", "fixture_key": fixture["fixture_key"], "task_id": fixture["task"].id, "task_run_id": run.id, "agent_version_id": fixture["grounded_agent_version"].id, "agent_run_id": run.agent_runs[0].id, "instructions": "This lab demonstrates grounding and retrieval principles using bounded supplied context. It is not a production vector-RAG implementation."}
    if day == 19:
        fixture = ensure_day19(db, user, item)
        workflow_run = WorkflowExecutionService(db).start_workflow_run_from_task(fixture["workflow_version"].id, fixture["task"].id)
        return {**context, "title": item.title, "learning_objective": item.spec["objectives"][0], "experiment_id": None, "engine": "ma7_workflow", "fixture_key": fixture["fixture_key"], "task_id": fixture["task"].id, "workflow_id": fixture["workflow"].id, "workflow_version_id": fixture["workflow_version"].id, "workflow_run_id": workflow_run.id, "instructions": "Inspect the published Agent handoff workflow, its node responsibilities, and the resulting MA7 run before submitting the workflow reflection."}
    if day == 20:
        fixture = ensure_day20(db, user, item)
        baseline = TaskService(db).start_task_run(task_id=fixture["task"].id, agent_version_id=fixture["baseline_agent_version"].id, frozen_task_snapshot={**context, "fixture_key": fixture["fixture_key"], "fixture_version": 1, "run_role": "baseline", "requires_improved_run": True, "improved_agent_version_id": fixture["improved_agent_version"].id})
        return {**context, "title": item.title, "learning_objective": item.spec["objectives"][0], "experiment_id": None, "engine": "evaluation_agent", "fixture_key": fixture["fixture_key"], "task_id": fixture["task"].id, "task_run_id": baseline.id, "agent_version_id": fixture["baseline_agent_version"].id, "agent_run_id": baseline.agent_runs[0].id, "instructions": "Run the baseline, record the failure, make only the permitted change, then run the improved version and compare both runs. A conclusion without both runs cannot qualify."}
    if day not in (4, 5, 9):
        raise ConflictError(f"Academy Day {day} has no governed execution binding")
    _kits, versions = LabService(db).starter_kits(user)
    if not versions:
        raise ConflictError("No learner-visible Personal Lab Test Kit is available")
    agent_versions = db.execute(
        select(AgentVersion).join(Agent, Agent.id == AgentVersion.agent_id).join(ProjectMembership, ProjectMembership.project_id == Agent.project_id)
        .where(ProjectMembership.user_id == user.id, AgentVersion.status == VersionStatus.ACTIVE).order_by(AgentVersion.created_at)
    ).scalars().all()
    if len(agent_versions) < 2:
        raise ConflictError("The existing Personal Lab requires two active Agent Versions for Prompt Comparison")
    experiment = LabService(db).create_experiment(user, ExperimentCreate(
        experiment_type="prompt_comparison", hypothesis=f"Academy Day {day}: {item.title}",
        eval_set_version_id=versions[0].id, agent_version_ids=[row.id for row in agent_versions[:2]],
        learning_item_id=item.id, concept_id=item.concept_id,
        config={"academy_program": "practical-ai-foundations", "academy_week": item.spec["week"], "academy_day": day, "academy_title": item.title, "academy_learning_objective": item.spec["objectives"][0], "return_to": f"#/academy/level-1/{day}", "assistance_level": "UNKNOWN", "assistance_required_for_qualification": True, "capability_boundary": item.spec.get("capability_boundary")},
    ))
    return {"academy_program": "practical-ai-foundations", "week": item.spec["week"], "day": day, "title": item.title, "learning_objective": item.spec["objectives"][0], "learning_item_id": item.id, "experiment_id": experiment.id, "engine": "personal_lab_experiment", "fixture_key": f"level1-day-{day}-experiment", "return_to": f"#/academy/level-1/{day}", "capability_boundary": item.spec.get("capability_boundary")}


@router.post("/agent-runs/{agent_run_id}/qualify")
def qualify_agent_run(agent_run_id: str, body: Level1LabResultRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if body.assistance_level.lower() not in {f"h{n}" for n in range(6)}:
        raise ConflictError("Assistance level must be h0 through h5")
    try:
        evidence = qualify_agent_fixture(db, user, agent_run_id, body.assistance_level, body.conclusion, body.observed_change)
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    state = LearnerStateService(db).state(user.id, evidence.concept_id)
    return {"evidence_id": evidence.id, "learning_item_id": evidence.learning_item_id, "learner_state": state.ladder, "passed": evidence.passed, "return_to": f"#/academy/level-1"}


@router.post("/workflow-runs/{workflow_run_id}/qualify")
def qualify_workflow_run(workflow_run_id: str, body: Level1LabResultRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if body.assistance_level.lower() not in {f"h{n}" for n in range(6)}:
        raise ConflictError("Assistance level must be h0 through h5")
    try:
        evidence = qualify_workflow_fixture(db, user, workflow_run_id, body.assistance_level, body.conclusion)
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    state = LearnerStateService(db).state(user.id, evidence.concept_id)
    return {"evidence_id": evidence.id, "learning_item_id": evidence.learning_item_id, "learner_state": state.ladder, "passed": evidence.passed, "return_to": "#/academy/level-1/19"}


@router.post("/days/20/improve", response_model=Level1LabRead)
def improve_day20(body: Level1LabLaunchRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not body.input_text:
        raise ConflictError("baseline_task_run_id is required to launch the linked improved run")
    from app.models.tasks import TaskRun
    baseline = db.get(TaskRun, body.input_text)
    baseline_meta = (baseline.config_snapshot or {}).get("frozen_task_snapshot") or baseline.config_snapshot or {}
    if baseline is None or baseline.task.created_by != user.id or baseline_meta.get("run_role") != "baseline":
        raise NotFoundError("Baseline run not found")
    service = AcademyLevel1Service(db)
    item = service.item(next(row["item_id"] for row in service.days(user.id) if row["day"] == 20))
    fixture = ensure_day20(db, user, item)
    improved = TaskService(db).start_task_run(task_id=fixture["task"].id, agent_version_id=fixture["improved_agent_version"].id, frozen_task_snapshot={"academy_day": 20, "academy_learning_item_id": item.id, "fixture_key": fixture["fixture_key"], "run_role": "improved", "baseline_task_run_id": baseline.id})
    snapshot = dict(improved.config_snapshot or {})
    snapshot.update({"run_role": "improved", "baseline_task_run_id": baseline.id})
    improved.config_snapshot = snapshot
    db.commit()
    return {"academy_program": "practical-ai-foundations", "week": item.spec["week"], "day": 20, "title": item.title, "learning_objective": item.spec["objectives"][0], "learning_item_id": item.id, "experiment_id": None, "engine": "evaluation_agent", "fixture_key": fixture["fixture_key"], "task_id": fixture["task"].id, "task_run_id": improved.id, "agent_version_id": fixture["improved_agent_version"].id, "agent_run_id": improved.agent_runs[0].id, "return_to": "#/academy/level-1/20", "instructions": "Compare this improved run with the linked baseline before qualifying the evaluation."}


@router.post("/days/{day}/start-capstone")
def start_capstone(day: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if day not in range(21, 31):
        raise ConflictError("Capstone orchestration is available only for Days 21–30")
    level1 = AcademyLevel1Service(db)
    level1.provision(user)
    template = ensure_level1_capstone_template(
        db, user.id, {row.spec["day"]: row for row in level1._current_academy_items()}
    )
    if template is None:
        raise ConflictError("The approved capstone preparation project is not available")
    attempt = db.execute(select(ProjectAttempt).where(ProjectAttempt.user_id == user.id, ProjectAttempt.project_template_id == template.id, ProjectAttempt.is_capstone.is_(True))).scalars().first()
    if attempt is None:
        attempt_id = ProjectAttemptService.create_attempt(db, user.id, template.id)
        attempt = db.get(ProjectAttempt, attempt_id)
        attempt.is_capstone = True
        db.commit()
    milestone = db.execute(
        select(ProjectMilestone, MilestoneAttempt)
        .join(MilestoneAttempt, MilestoneAttempt.project_milestone_id == ProjectMilestone.id)
        .where(
            ProjectMilestone.project_template_id == template.id,
            ProjectMilestone.position == day - 20,
            MilestoneAttempt.project_attempt_id == attempt.id,
        )
    ).first()
    if milestone is None:
        raise ConflictError("The capstone stage is not provisioned")
    project_milestone, milestone_attempt = milestone
    return {"day": day, "stage": CAPSTONE_STAGES[day], "project_attempt_id": attempt.id, "project_template_id": template.id, "project_milestone_id": project_milestone.id, "milestone_attempt_id": milestone_attempt.id, "required_artifact_kind": project_milestone.expected_artifact_kind, "project_url": f"#/academy/projects/attempts/{attempt.id}", "assessment_center_url": "#/academy/assessments", "professor_review_day": 30}


@router.get("/labs/{experiment_id}/qualification")
def lab_qualification(experiment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    experiment = db.execute(select(Experiment).where(Experiment.id == experiment_id, Experiment.user_id == user.id)).scalar_one_or_none()
    if experiment is None:
        raise NotFoundError("Experiment not found")
    config = experiment.config_snapshot or {}
    if config.get("assistance_level") in (None, "UNKNOWN"):
        return {"status": "ASSISTANCE_REQUIRED", "ready": False, "message": "Record the actual assistance level before requesting learning qualification."}
    return ExperimentLearningQualificationService(db).assess(user.id, experiment_id)


@router.put("/labs/{experiment_id}/assistance")
def lab_assistance(experiment_id: str, body: Level1AssistanceRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    experiment = db.execute(select(Experiment).where(Experiment.id == experiment_id, Experiment.user_id == user.id)).scalar_one_or_none()
    if experiment is None:
        raise NotFoundError("Experiment not found")
    if body.assistance_level not in {f"h{n}" for n in range(6)}:
        raise ConflictError("Assistance level must be h0 through h5")
    snapshot = dict(experiment.config_snapshot or {})
    snapshot["assistance_level"] = body.assistance_level
    experiment.config_snapshot = snapshot
    db.commit()
    return {"experiment_id": experiment.id, "assistance_level": body.assistance_level}


@router.post("/assessments/start")
def assessment_start(body: Level1AssessmentStartRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.services.academy_level1_service import EXISTING_ASSESSMENT_BINDINGS
    if body.definition_key not in set(EXISTING_ASSESSMENT_BINDINGS.values()):
        raise ConflictError("Assessment definition is not bound to the Level 1 curriculum")
    return AssessmentService(db).attempt_view(AssessmentService(db).start(user, body.definition_key, project_attempt_id=body.project_attempt_id))


@router.put("/assessments/{attempt_id}/draft")
def assessment_draft(attempt_id: str, body: Level1AssessmentDraftRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AssessmentService(db)
    return service.attempt_view(service.save_draft(user.id, attempt_id, body.draft))


@router.post("/assessments/{attempt_id}/submit")
def assessment_submit(attempt_id: str, body: Level1AssessmentSubmitRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AssessmentService(db)
    return service.attempt_view(service.submit(user, attempt_id, body.attestation))


@router.get("/graduation", response_model=Level1GraduationRead)
def graduation(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AcademyLevel1Service(db).graduation(user.id)


@router.get("/days/30/review", response_model=Level1ReviewRead)
def review(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AcademyLevel1Service(db).review(user.id)


# -- AIL.5D.1: structured steps + learner step progress -----------------------------------------------------------------
# Additive and learner-scoped. Opening a step never completes it; interactive steps cannot be completed by request.


@router.post("/days/{day}/author-structure")
def author_structure(day: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """AIL.5D.3: explicitly publish a Day's structured form as the next version of its Learning Item. Idempotent.
    Routine provisioning never does this."""
    return author_day_structure(db, day)


@router.get("/days/{day}/learning", response_model=Level1LearningRead)
def day_learning(day: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The learner read model of a Day. Structured Days return ordered public steps with this learner's status;
    legacy Days answer ``structured: false`` and keep using ``GET /days/{day}``."""
    level1 = AcademyLevel1Service(db)
    rows = [row for row in level1.days(user.id) if row["day"] == day]
    if not rows:
        raise NotFoundError("Academy Level 1 day is not provisioned")
    row = rows[0]
    if not row["structured"]:
        return {"structured": False, "item_id": row["item_id"], "title": row["title"], "day": row["day"], "week": row["week"], "kind": row["kind"], "demonstrated": row["demonstrated"], "concept_state": row["state"]}
    return AcademyStepService(db).learning_view(user.id, row["item_id"])


@router.get("/items/{item_id}/steps", response_model=Level1StepsRead)
def item_steps(item_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AcademyStepService(db)
    return {**service.structure(item_id), "progress": service.progress(user.id, item_id)}


@router.put("/items/{item_id}/steps/{step_key}/response", response_model=Level1StepResponseRead)
def respond_to_step(item_id: str, step_key: str, body: Level1StepResponseRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """AIL.5D.3: save the learner's own response to an interactive step. Private, append-only, never graded."""
    return AcademyStepService(db).respond(user.id, item_id, step_key, body.dict(exclude_none=True))


@router.get("/items/{item_id}/steps/{step_key}/response", response_model=Level1StepResponseRead)
def get_step_response(item_id: str, step_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AcademyStepService(db).responses(user.id, item_id, step_key)


@router.post("/items/{item_id}/steps/{step_key}/reveal", response_model=Level1RevealRead)
def reveal_step(item_id: str, step_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The authored reveal of a think step. Served only after this learner has committed an answer."""
    return AcademyStepService(db).reveal(user.id, item_id, step_key)


@router.get("/items/{item_id}/steps/{step_key}/professor")
def step_professor_status(item_id: str, step_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """AIL.5D.4: what the step Professor is (mode, next hint level), what it can see, and whether it is paused."""
    return ProfessorExecutionService(db).step_help_status(user, item_id, step_key)


@router.post("/items/{item_id}/steps/{step_key}/professor", response_model=ProfessorInteractionRead)
def ask_step_professor(item_id: str, step_key: str, body: Level1StepProfessorRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """AIL.5D.4: ask the AI Professor about ONE step. Context is assembled on the server from the current step and this
    learner only; it is held while an AIL.5C assessment is open; it never creates evidence."""
    request = ProfessorContextRequest(
        intent=ProfessorIntent.EXPLAIN_THIS, question=body.question, help=body.help,
        target=ProfessorTarget(type=ProfessorTargetType.ACADEMY_STEP, id=item_id, step_key=step_key),
    )
    return ProfessorExecutionService(db).create_and_execute(user, request)


@router.post("/items/{item_id}/steps/{step_key}/open", response_model=Level1StepProgressRead)
def open_step(item_id: str, step_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AcademyStepService(db).open_step(user.id, item_id, step_key)


@router.post("/items/{item_id}/steps/{step_key}/complete", response_model=Level1StepProgressRead)
def complete_step(item_id: str, step_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AcademyStepService(db).complete_step(user.id, item_id, step_key)


@router.post("/items/{item_id}/steps/{step_key}/skip", response_model=Level1StepProgressRead)
def skip_step(item_id: str, step_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AcademyStepService(db).skip_step(user.id, item_id, step_key)
