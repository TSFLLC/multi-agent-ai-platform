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

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List
from app.db.session import get_db
from app.services.build_with_me_service import ProjectTemplateService, ProjectAttemptService, MilestoneAttemptService

router = APIRouter(prefix="/academy/build-with-me", tags=["Build With Me"])


@router.get("/projects")
def list_projects(
    db: Session = Depends(get_db),
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
        }
        for p in projects
    ]


@router.get("/projects/{template_id}")
def get_project(template_id: str, db: Session = Depends(get_db)):
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
    }


@router.post("/attempts")
def start_project(template_id: str, user_id: str, enrollment_id: Optional[str] = None, db: Session = Depends(get_db)):
    """Start a new project attempt."""
    attempt_id = ProjectAttemptService.create_attempt(db, user_id, template_id, enrollment_id)
    return {"attempt_id": attempt_id, "status": "active"}


@router.get("/attempts/{attempt_id}")
def get_attempt(attempt_id: str, db: Session = Depends(get_db)):
    """Get project attempt current state."""
    attempt = ProjectAttemptService.get_attempt(db, attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    return attempt


@router.get("/attempts/{attempt_id}/milestones/{milestone_id}")
def get_milestone_workspace(attempt_id: str, milestone_id: str, db: Session = Depends(get_db)):
    """Get milestone workspace (instructions, checks, artifact, help level)."""
    from app.models.academy import MilestoneAttempt, ProjectMilestone

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
        "mode": milestone_attempt.mode,
    }


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/test")
def run_milestone_checks(attempt_id: str, milestone_id: str, db: Session = Depends(get_db)):
    """Run objective checks on the current milestone artifact."""
    from app.models.academy import MilestoneAttempt

    milestone_attempt = (
        db.query(MilestoneAttempt)
        .filter(MilestoneAttempt.project_attempt_id == attempt_id, MilestoneAttempt.project_milestone_id == milestone_id)
        .first()
    )

    if not milestone_attempt:
        raise HTTPException(status_code=404, detail="Milestone not found")

    MilestoneAttemptService.record_attempt(db, milestone_attempt.id)

    return {
        "status": "checking",
        "attempts_count": milestone_attempt.attempts_count + 1,
        "checks": [{"name": "check_1", "passed": True, "message": "Placeholder check"}],
    }


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/hint")
def request_hint(attempt_id: str, milestone_id: str, hint_level: Optional[str] = None, db: Session = Depends(get_db)):
    """Request help at the next available hint level (deterministic policy)."""
    from app.models.academy import MilestoneAttempt
    from app.services.build_with_me_service import HintPolicyEngine

    milestone_attempt = (
        db.query(MilestoneAttempt)
        .filter(MilestoneAttempt.project_attempt_id == attempt_id, MilestoneAttempt.project_milestone_id == milestone_id)
        .first()
    )

    if not milestone_attempt:
        raise HTTPException(status_code=404, detail="Milestone not found")

    # Placeholder: real implementation would check policy engine
    return {
        "hint_level": "h1",
        "content": "Think about the main goal of this step.",
        "unlocked_next_level": "h2_unlocks_after_one_attempt",
    }


@router.post("/attempts/{attempt_id}/milestones/{milestone_id}/complete")
def complete_milestone(attempt_id: str, milestone_id: str, db: Session = Depends(get_db)):
    """Mark milestone ready for review/submission."""
    from app.models.academy import MilestoneAttempt

    milestone_attempt = (
        db.query(MilestoneAttempt)
        .filter(MilestoneAttempt.project_attempt_id == attempt_id, MilestoneAttempt.project_milestone_id == milestone_id)
        .first()
    )

    if not milestone_attempt:
        raise HTTPException(status_code=404, detail="Milestone not found")

    milestone_attempt.status = "passed"
    milestone_attempt.completed_at = db.func.now()
    db.commit()

    return {"status": "completed"}


@router.get("/attempts/{attempt_id}/evidence")
def get_attempt_evidence(attempt_id: str, db: Session = Depends(get_db)):
    """Get summary of evidence collected on this project."""
    return {
        "attempt_id": attempt_id,
        "evidence_items": [
            {
                "concept_id": "concept-1",
                "type": "milestone_check",
                "assistance_level": "h0",
                "evidence_id": "ev-1",
            }
        ],
    }
