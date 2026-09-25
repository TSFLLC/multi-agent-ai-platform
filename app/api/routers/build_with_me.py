"""AIL.5B Build With Me + Project Mentor API routes.

GET  /projects                                  - library with facets
GET  /projects/{template_id}                    - details & prerequisites
POST /attempts                                  - start project
GET  /attempts/{attempt_id}                     - current state
GET  /attempts/{attempt_id}/milestones/{m_id}  - milestone workspace
POST /attempts/{attempt_id}/milestones/{m_id}/test - run checks
POST /attempts/{attempt_id}/milestones/{m_id}/hint - get help (deterministic policy)
POST /attempts/{attempt_id}/milestones/{m_id}/complete - mark ready
GET  /attempts/{attempt_id}/evidence           - summary
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from app.auth import get_current_user
from app.models.identity import User
from app.errors import ForbiddenError, ConflictError
from app.db.enums import AssistanceLevel, EvidenceType, ExecutionVerification, GradingMode, MilestoneAttemptStatus
from sqlalchemy.orm import Session
from typing import Optional, List
from app.db.session import get_db
from app.services.build_with_me_service import ProjectTemplateService, ProjectAttemptService, MilestoneAttemptService

router = APIRouter(prefix="/academy/build-with-me", tags=["Build With Me"])


class MentorRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    assistance_level: AssistanceLevel = AssistanceLevel.H1


class ExplainBackRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    response: str = Field(min_length=1, max_length=12000)


class ExperimentLinkRequest(BaseModel):
    experiment_id: str
    project_question: Optional[str] = None


class DecisionRequest(BaseModel):
    learner_decision: str = Field(min_length=1, max_length=4000)


class EvidenceRequest(BaseModel):
    concept_id: Optional[str] = None
    passed: bool
    source_type: str = Field(min_length=1, max_length=50)
    source_id: Optional[str] = None
    assistance_level: Optional[AssistanceLevel] = None
    execution_verification: ExecutionVerification = ExecutionVerification.NOT_APPLICABLE


class CountLearningRequest(BaseModel):
    confirmed: bool


@router.get("/projects")
def list_projects(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    audience_level: Optional[str] = Query(None),
    ladder_level: Optional[str] = Query(None),
    skill: Optional[str] = Query(None),
):
    """List projects with filtering."""
    from app.models.academy import ProjectTemplate

    query = db.query(ProjectTemplate).filter(ProjectTemplate.status == "published")

    if audience_level:
        query = query.filter(ProjectTemplate.audience_level == audience_level)
    if ladder_level:
        query = query.filter(ProjectTemplate.ladder_level == ladder_level)

    projects = query.all()
    return [
        {
            "id": p.id,
            "template_key": p.template_key,
            "title": p.title,
            "audience_level": p.audience_level,
            "ladder_level": p.ladder_level,
            "build_mode": p.build_mode,
            "est_minutes_min": p.est_minutes_min,
            "est_minutes_max": p.est_minutes_max,
            "concepts": [{"id": c.concept_id, "role": c.role} for c in p.concept_links],
            "availability": "available_after_ma9" if p.requires_platform_capability else "available",
            "status": "not_started",
        }
        for p in projects
    ]


@router.post("/projects/seed-foundation")
def seed_foundation_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Owner-triggered content seed; no duplicate Concepts are created."""
    try:
        projects = ProjectTemplateService.seed_foundation_projects(db, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"created": len(projects), "template_keys": [p.template_key for p in projects]}


@router.get("/projects/{template_id}")
def get_project(template_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Get project template details and prerequisite status."""
    from app.models.academy import ProjectTemplate

    template = db.query(ProjectTemplate).filter(ProjectTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Project template not found")

    return {
        "id": template.id,
        "title": template.title,
        "brief": template.brief_md,
        "ladder_level": template.ladder_level,
        "concepts": [{"id": c.concept_id, "role": c.role} for c in template.concept_links],
        "milestone_count": len(template.milestones),
        "requires_platform_capability": template.requires_platform_capability,
        "capstone_eligible": template.capstone_eligible,
        "estimated_minutes": {"min": template.est_minutes_min, "max": template.est_minutes_max},
        "milestones": [{"id": m.id, "position": m.position, "title": m.title, "instructions": m.instructions_md, "acceptance": m.check_spec, "study_mode": bool(m.reference_solution_ref), "requires_capability": template.requires_platform_capability} for m in template.milestones],
    }


@router.post("/attempts")
def start_project(template_id: str, user_id: Optional[str] = None, enrollment_id: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Start a new project attempt."""
    if user_id is not None and user_id != user.id:
        raise HTTPException(status_code=403, detail="Cannot start a project for another learner")
    attempt_id = ProjectAttemptService.create_attempt(db, user.id, template_id, enrollment_id)
    return {"attempt_id": attempt_id, "status": "active"}


@router.get("/attempts/{attempt_id}")
def get_attempt(attempt_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Get project attempt current state."""
    try: ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    except LookupError: raise HTTPException(status_code=404, detail="Attempt not found")
    attempt = ProjectAttemptService.get_attempt(db, attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    return attempt


@router.get("/attempts/{attempt_id}/milestones/{milestone_id}")
def get_milestone_workspace(attempt_id: str, milestone_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Get milestone workspace (instructions, checks, artifact, help level)."""
    from app.models.academy import MilestoneAttempt, ProjectMilestone

    try: ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    except LookupError: raise HTTPException(status_code=404, detail="Milestone not found")
    milestone_attempt = (
        db.query(MilestoneAttempt)
        .filter(MilestoneAttempt.project_attempt_id == attempt_id, MilestoneAttempt.project_milestone_id == milestone_id)
        .first()
    )

    if not milestone_attempt:
        raise HTTPException(status_code=404, detail="Milestone not found")

    milestone = db.query(ProjectMilestone).filter(ProjectMilestone.id == milestone_id).first()

    return {
        "milestone_id": milestone.id,
        "title": milestone.title,
        "instructions": milestone.instructions_md,
        "position": milestone.position,
        "check_spec": milestone.check_spec,
        "status": milestone_attempt.status,
        "attempts_count": milestone_attempt.attempts_count,
        "max_assistance_level": milestone_attempt.max_assistance_level,
        "maximum_unlocked_level": __import__("app.services.build_with_me_service", fromlist=["HintPolicyEngine"]).HintPolicyEngine.maximum_unlocked_level(milestone_attempt.max_assistance_level, milestone_attempt.attempts_count, milestone_attempt.attempts_count if milestone_attempt.status == MilestoneAttemptStatus.FAILED else 0),
        "mode": milestone_attempt.mode,
        "variants": [{"id": a.id, "mode": a.mode, "status": a.status, "attempts_count": a.attempts_count} for a in MilestoneAttemptService.attempts(db, attempt_id, milestone_id)],
    }


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/test")
def run_milestone_checks(attempt_id: str, milestone_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Run objective checks on the current milestone artifact."""
    from app.models.academy import MilestoneAttempt

    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    milestone_attempt = (
        db.query(MilestoneAttempt)
        .filter(MilestoneAttempt.project_attempt_id == attempt_id, MilestoneAttempt.project_milestone_id == milestone_id)
        .first()
    )

    if not milestone_attempt:
        raise HTTPException(status_code=404, detail="Milestone not found")

    MilestoneAttemptService.record_attempt(db, milestone_attempt.id)

    return {"status": "checking", "attempts_count": milestone_attempt.attempts_count, "checks": [], "execution_available": False, "message": "No execution capability is available in AIL.5B; submit an actual platform result or continue after MA9."}


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/start")
def start_milestone(attempt_id: str, milestone_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    from app.models.academy import MilestoneAttempt
    row = db.query(MilestoneAttempt).filter_by(project_attempt_id=attempt_id, project_milestone_id=milestone_id).order_by(MilestoneAttempt.created_at.desc()).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Milestone not found")
    row.status = MilestoneAttemptStatus.IN_PROGRESS
    if row.started_at is None: row.started_at = datetime.utcnow()
    db.commit()
    return {"milestone_attempt_id": row.id, "status": row.status, "mode": row.mode}


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/hint")
def request_hint(attempt_id: str, milestone_id: str, hint_level: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Request help at the next available hint level (deterministic policy)."""
    from app.models.academy import MilestoneAttempt
    from app.services.build_with_me_service import HintPolicyEngine

    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    milestone_attempt = (
        db.query(MilestoneAttempt)
        .filter(MilestoneAttempt.project_attempt_id == attempt_id, MilestoneAttempt.project_milestone_id == milestone_id)
        .first()
    )

    if not milestone_attempt:
        raise HTTPException(status_code=404, detail="Milestone not found")

    requested = AssistanceLevel(hint_level or "h1")
    if not HintPolicyEngine.can_unlock_hint(requested, milestone_attempt.max_assistance_level, milestone_attempt.attempts_count, 0, milestone_attempt.attempts_count if milestone_attempt.status == MilestoneAttemptStatus.FAILED else 0):
        raise HTTPException(status_code=409, detail="Requested assistance level is not unlocked")
    MilestoneAttemptService.record_attempt(db, milestone_attempt.id)
    from app.services.build_with_me_service import AssistanceProvenance
    AssistanceProvenance.record_help_on_evidence  # explicit provenance API reused by Mentor/evidence flow
    return {
        "hint_level": requested,
        "content": (milestone_attempt.milestone.hint_content or {}).get(requested.value, "Think about the main goal of this step."),
        "maximum_authorized_level": requested,
        "unlocked_next_level": "h2_unlocks_after_one_attempt" if requested == AssistanceLevel.H1 else None,
    }


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/complete")
def complete_milestone(attempt_id: str, milestone_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Mark milestone ready for review/submission."""
    from app.models.academy import MilestoneAttempt

    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    milestone_attempt = (
        db.query(MilestoneAttempt)
        .filter(MilestoneAttempt.project_attempt_id == attempt_id, MilestoneAttempt.project_milestone_id == milestone_id)
        .first()
    )

    if not milestone_attempt:
        raise HTTPException(status_code=404, detail="Milestone not found")

    milestone_attempt.status = "passed"
    milestone_attempt.completed_at = datetime.utcnow()
    db.commit()

    return {"status": "completed"}


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/evidence")
def record_evidence(attempt_id: str, milestone_id: str, body: EvidenceRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.academy import CandidateEvidence, MilestoneAttempt
    from app.models.concepts import ConceptVersion
    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    milestone_attempt = db.query(MilestoneAttempt).filter_by(project_attempt_id=attempt_id, project_milestone_id=milestone_id).order_by(MilestoneAttempt.created_at.desc()).first()
    if milestone_attempt is None: raise HTTPException(status_code=404, detail="Milestone attempt not found")
    row = CandidateEvidence(id=__import__("uuid").uuid4().hex, user_id=user.id, project_attempt_id=attempt_id, milestone_attempt_id=milestone_attempt.id, concept_id=body.concept_id, source_type=body.source_type, source_id=body.source_id, evidence_type="lab", passed=body.passed, assistance_level=body.assistance_level or milestone_attempt.max_assistance_level, execution_verification=body.execution_verification)
    db.add(row); db.flush()
    learning = None
    if row.concept_id:
        version = db.query(ConceptVersion).filter(ConceptVersion.concept_id == row.concept_id).order_by(ConceptVersion.created_at.desc()).first()
        if version: learning = __import__("app.services.build_with_me_service", fromlist=["EvidenceQualification"]).EvidenceQualification.qualify_candidate(db, row, concept_version_id=version.id)
    else: db.commit()
    learner_state = None
    if learning:
        from app.services.learner_state_service import LearnerStateService
        learner_state = LearnerStateService(db).state(user.id, learning.concept_id)
    return {"candidate_evidence_id": row.id, "qualified": row.qualified, "learning_evidence_id": learning.id if learning else None, "learner_state_recomputed": learner_state is not None, "learner_state": {"ladder": learner_state.ladder, "overlays": sorted(learner_state.overlays)} if learner_state else None}


@router.get("/attempts/{attempt_id}/evidence")
def get_attempt_evidence(attempt_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Get summary of evidence collected on this project."""
    try: ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    except LookupError: raise HTTPException(status_code=404, detail="Attempt not found")
    from app.models.academy import CandidateEvidence, ExplainBackResponse
    rows = db.query(CandidateEvidence).filter(CandidateEvidence.project_attempt_id == attempt_id, CandidateEvidence.user_id == user.id).all()
    return {
        "attempt_id": attempt_id,
        "project_activity": [{"id": e.id, "source_type": e.source_type, "source_id": e.source_id} for e in rows],
        "candidate_evidence": [{"id": e.id, "passed": e.passed, "qualified": e.qualified, "assistance_level": e.assistance_level} for e in rows],
        "qualified_learning_evidence": [{"id": e.learning_evidence_id, "concept_id": e.concept_id} for e in rows if e.qualified],
        "explain_back": [{"id": e.id, "question": e.question, "response": e.response} for e in db.query(ExplainBackResponse).filter(ExplainBackResponse.project_attempt_id == attempt_id, ExplainBackResponse.user_id == user.id).all()],
    }


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/study-mode")
def start_study_mode(attempt_id: str, milestone_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    from app.models.academy import ProjectMilestone
    milestone = db.get(ProjectMilestone, milestone_id)
    if milestone is None or not milestone.reference_solution_ref:
        raise HTTPException(status_code=409, detail="Study Mode is not authored for this milestone")
    milestone_attempt = db.query(__import__("app.models.academy", fromlist=["MilestoneAttempt"]).MilestoneAttempt).filter_by(project_attempt_id=attempt_id, project_milestone_id=milestone_id).first()
    if milestone_attempt is None or milestone_attempt.max_assistance_level != AssistanceLevel.H5:
        raise HTTPException(status_code=409, detail="Study Mode unlocks after the authored assistance path")
    variant_id = MilestoneAttemptService.start_variant(db, attempt_id, milestone_id)
    return {"variant_attempt_id": variant_id, "worked_example_ref": milestone.reference_solution_ref, "variant": (milestone.hint_content or {}).get("variant"), "assistance_level": "h0"}


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/mentor")
def mentor(attempt_id: str, milestone_id: str, body: MentorRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.services.project_mentor_service import ProjectMentorService
    result, level, milestone_attempt_id = ProjectMentorService(db).ask(user, attempt_id, milestone_id, body.question, body.assistance_level)
    return {"response": result.model_dump(mode="json"), "assistance_level": level, "milestone_attempt_id": milestone_attempt_id, "provenance": {"agent_run_id": result.agent_run_id, "task_run_id": result.task_run_id}}


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/explain-back")
def explain_back(attempt_id: str, milestone_id: str, body: ExplainBackRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.academy import ExplainBackResponse, MilestoneAttempt
    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    attempt = db.query(MilestoneAttempt).filter_by(project_attempt_id=attempt_id, project_milestone_id=milestone_id).order_by(MilestoneAttempt.created_at.desc()).first()
    if attempt is None: raise HTTPException(status_code=404, detail="Milestone attempt not found")
    row = ExplainBackResponse(id=__import__("uuid").uuid4().hex, user_id=user.id, project_attempt_id=attempt_id, milestone_attempt_id=attempt.id, question=body.question, response=body.response, assistance_level=attempt.max_assistance_level)
    db.add(row); db.commit()
    return {"id": row.id, "captured": True, "formally_graded": False}


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/experiments")
def link_experiment(attempt_id: str, milestone_id: str, body: ExperimentLinkRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.academy import ProjectExperimentLink, MilestoneAttempt
    from app.models.lab import Experiment
    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    experiment = db.query(Experiment).filter(Experiment.id == body.experiment_id, Experiment.user_id == user.id).first()
    if experiment is None: raise HTTPException(status_code=404, detail="Experiment not found")
    milestone = db.query(MilestoneAttempt).filter_by(project_attempt_id=attempt_id, project_milestone_id=milestone_id).order_by(MilestoneAttempt.created_at.desc()).first()
    row = ProjectExperimentLink(id=__import__("uuid").uuid4().hex, project_attempt_id=attempt_id, milestone_attempt_id=milestone.id if milestone else None, experiment_id=experiment.id, project_question=body.project_question)
    db.add(row); db.commit()
    return {"id": row.id, "experiment_id": experiment.id, "conclusion_owned_by": "learner"}


@router.put("/attempts/{attempt_id}/experiments/{link_id}/decision")
def save_project_decision(attempt_id: str, link_id: str, body: DecisionRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.academy import ProjectExperimentLink
    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    row = db.query(ProjectExperimentLink).filter_by(id=link_id, project_attempt_id=attempt_id).first()
    if row is None: raise HTTPException(status_code=404, detail="Experiment link not found")
    row.learner_decision = body.learner_decision; db.commit()
    return {"id": row.id, "learner_decision": row.learner_decision}


@router.get("/attempts/{attempt_id}/experiments/eligible")
def eligible_experiments(attempt_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.lab import Experiment
    from app.services.experiment_learning_qualification_service import ExperimentLearningQualificationService
    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    experiments = db.query(Experiment).filter(Experiment.user_id == user.id).order_by(Experiment.created_at.desc()).limit(40).all()
    return [{"id": e.id, "hypothesis": e.hypothesis, "status": e.status, "qualification": ExperimentLearningQualificationService(db).assess(user.id, e.id)} for e in experiments]


@router.post("/attempts/{attempt_id}/experiments/{experiment_id}/count-toward-learning")
def count_experiment(attempt_id: str, experiment_id: str, body: CountLearningRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required")
    from app.models.academy import CandidateEvidence
    from app.models.lab import Experiment
    from app.services.experiment_learning_qualification_service import ExperimentLearningQualificationService
    from app.services.learner_state_service import LearnerStateService
    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id, Experiment.user_id == user.id).first()
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    candidate = db.query(CandidateEvidence).filter_by(project_attempt_id=attempt_id, source_type="experiment", source_id=experiment_id, user_id=user.id).first()
    if candidate is None:
        candidate = CandidateEvidence(id=__import__("uuid").uuid4().hex, user_id=user.id, project_attempt_id=attempt_id, concept_id=experiment.concept_id, source_type="experiment", source_id=experiment_id, evidence_type="lab", passed=False, qualified=False)
        db.add(candidate)
        db.commit()
    evidence, created, qualification = ExperimentLearningQualificationService(db).count_toward_learning(user.id, experiment_id)
    if evidence is None:
        raise HTTPException(status_code=409, detail=qualification)
    candidate.passed = True
    candidate.qualified = True
    candidate.learning_evidence_id = evidence.id
    db.commit()
    state = LearnerStateService(db).state(user.id, evidence.concept_id)
    return {"created": created, "candidate_evidence": {"id": candidate.id, "source": "experiment", "source_id": experiment_id, "qualified": candidate.qualified}, "learning_evidence_id": evidence.id, "qualification": qualification, "learner_state": {"ladder": state.ladder, "overlays": sorted(state.overlays)}}


@router.post("/attempts/{attempt_id}/submit")
def submit_for_assessment(attempt_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.academy import AssessmentReadySubmission, CandidateEvidence, ExplainBackResponse, MilestoneAttempt
    attempt = ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    template = attempt.template
    milestone_attempts = db.query(MilestoneAttempt).filter_by(project_attempt_id=attempt_id).all()
    evidence = db.query(CandidateEvidence).filter_by(project_attempt_id=attempt_id, user_id=user.id).all()
    explain = db.query(ExplainBackResponse).filter_by(project_attempt_id=attempt_id, user_id=user.id).all()
    snapshot = {"program_version_id": attempt.enrollment.program_version_id if attempt.enrollment else None, "project_template_id": template.id, "project_template_version": template.version, "project_attempt_id": attempt.id, "milestone_attempt_ids": [m.id for m in milestone_attempts], "candidate_evidence_ids": [e.id for e in evidence], "learning_evidence_ids": [e.learning_evidence_id for e in evidence if e.learning_evidence_id], "explain_back_ids": [e.id for e in explain], "grader_invoked": False}
    row = db.query(AssessmentReadySubmission).filter_by(project_attempt_id=attempt_id).first()
    if row is None: row = AssessmentReadySubmission(id=__import__("uuid").uuid4().hex, user_id=user.id, project_attempt_id=attempt_id, snapshot=snapshot); db.add(row)
    else: row.snapshot = snapshot
    attempt.status = "submitted"; attempt.submitted_at = datetime.utcnow(); db.commit()
    return {"id": row.id, "status": row.status, "snapshot": row.snapshot, "ready_for": "AIL.5C", "grader_invoked": False}


@router.get("/attempts/{attempt_id}/submission")
def read_submission(attempt_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.academy import AssessmentReadySubmission
    ProjectAttemptService.own_attempt(db, attempt_id, user.id)
    row = db.query(AssessmentReadySubmission).filter_by(project_attempt_id=attempt_id, user_id=user.id).first()
    if row is None: raise HTTPException(status_code=404, detail="Assessment-ready submission not prepared")
    return {"id": row.id, "status": row.status, "snapshot": row.snapshot, "grader_invoked": False}
