"""AIL.5B Build With Me + Project Mentor services.

- ProjectTemplateService: load, search, validate, publish
- ProjectAttemptService: create, resume, check readiness
- MilestoneAttemptService: progress, attempt tracking
- HintPolicyEngine: deterministic H0-H5 unlock rules
- AssistanceProvenance: track & persist help level
- EvidenceQualification: candidate→qualified pipeline
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
from sqlalchemy import select
from app.db.enums import AssistanceLevel, MilestoneAttemptMode, MilestoneAttemptStatus, ProjectAttemptStatus
from sqlalchemy.orm import Session


class ProjectTemplateService:
    """Manages project templates: load, search, validate, publish."""

    @staticmethod
    def validate_template(template_data: dict) -> bool:
        """Validate template has required fields."""
        required = {"template_key", "title", "ladder_level", "build_mode", "brief_md"}
        return all(k in template_data for k in required)

    @staticmethod
    def publish_template(db: Session, template_id: str) -> bool:
        """Publish a template (immutable thereafter)."""
        from app.models.academy import ProjectTemplate

        template = db.query(ProjectTemplate).filter(ProjectTemplate.id == template_id).first()
        if template:
            template.status = "published"
            template.published_at = datetime.utcnow()
            db.commit()
            return True
        return False

    @staticmethod
    def seed_foundation_projects(db: Session, author_user_id: str):
        """Idempotently seed the four published learner project versions."""
        from app.academy_curriculum import BUILD_WITH_ME_PROJECTS
        from app.models.academy import ProjectTemplate, ProjectTemplateConceptLink, ProjectMilestone
        from app.models.concepts import Concept
        from app.db.enums import ProjectAudienceLevel, ProjectLadderLevel, ProjectTemplateBuildMode
        created = []
        for spec in BUILD_WITH_ME_PROJECTS:
            if db.query(ProjectTemplate).filter_by(template_key=spec["key"], version=1).first():
                continue
            concepts = [db.query(Concept).filter_by(slug=slug).first() for slug in spec["concepts"]]
            missing = [slug for slug, concept in zip(spec["concepts"], concepts) if concept is None]
            if missing:
                raise ValueError("Missing Concept Graph slugs: " + ", ".join(missing))
            template = ProjectTemplate(id=str(uuid4()), template_key=spec["key"], version=1, status="published", title=spec["title"], audience_level=ProjectAudienceLevel.BEGINNER, ladder_level=ProjectLadderLevel(spec["level"]), build_mode=ProjectTemplateBuildMode(spec["mode"]), brief_md=spec["brief"], difficulty_profile={"project": True}, evidence_requirements={"requires_actual_result": True}, variant_spec={"authored": True}, requires_platform_capability=spec.get("capability"), est_minutes_min=45, est_minutes_max=180, capstone_eligible=False, author_user_id=author_user_id, published_at=datetime.utcnow())
            db.add(template); db.flush()
            for concept in concepts: db.add(ProjectTemplateConceptLink(id=str(uuid4()), project_template_id=template.id, concept_id=concept.id, role="applied"))
            for position, title in enumerate(spec["milestones"], 1):
                db.add(ProjectMilestone(id=str(uuid4()), project_template_id=template.id, position=position, title=title, instructions_md=f"{title}. Record your own work and what you observed.", check_spec={"deterministic": True, "requires_result": True}, evidence_type="lab", hint_content={"variant": f"{title} — use a new example with the same skill."}, reference_solution_ref=f"academy/{spec['key']}/v1/milestone-{position}.md"))
            created.append(template)
        db.commit()
        return created


class ProjectAttemptService:
    """Manages learner project attempts: create, resume, check readiness."""

    @staticmethod
    def create_attempt(db: Session, user_id: str, template_id: str, enrollment_id: Optional[str] = None) -> str:
        """Create a new project attempt."""
        from app.models.academy import ProjectAttempt
        template = db.get(__import__("app.models.academy", fromlist=["ProjectTemplate"]).ProjectTemplate, template_id)
        if template is None or template.status != "published":
            raise ValueError("Published project template not found")
        attempt = ProjectAttempt(
            id=str(uuid4()),
            user_id=user_id,
            project_template_id=template_id,
            enrollment_id=enrollment_id,
            status=ProjectAttemptStatus.ACTIVE,
            brief_snapshot={"template_key": template.template_key, "version": template.version, "title": template.title},
            started_at=datetime.utcnow(),
        )
        db.add(attempt)
        db.commit()
        return attempt.id

    @staticmethod
    def get_attempt(db: Session, attempt_id: str) -> Optional[dict]:
        """Get attempt state."""
        from app.models.academy import ProjectAttempt

        attempt = db.query(ProjectAttempt).filter(ProjectAttempt.id == attempt_id).first()
        return {
            "id": attempt.id,
            "status": attempt.status,
            "started_at": attempt.started_at,
            "template_id": attempt.project_template_id,
            "milestones": [{"id": m.id, "milestone_id": m.project_milestone_id, "status": m.status, "mode": m.mode, "attempts_count": m.attempts_count, "max_assistance_level": m.max_assistance_level} for m in attempt.milestones],
        } if attempt else None

    @staticmethod
    def own_attempt(db: Session, attempt_id: str, user_id: str):
        from app.models.academy import ProjectAttempt
        attempt = db.execute(select(ProjectAttempt).where(ProjectAttempt.id == attempt_id, ProjectAttempt.user_id == user_id)).scalar_one_or_none()
        if attempt is None:
            raise LookupError("Project attempt not found")
        return attempt


class MilestoneAttemptService:
    """Manages milestone work tracking."""

    @staticmethod
    def start_milestone(db: Session, project_attempt_id: str, milestone_id: str) -> str:
        """Start a milestone."""
        from app.models.academy import MilestoneAttempt
        attempt = MilestoneAttempt(
            id=str(uuid4()),
            project_attempt_id=project_attempt_id,
            project_milestone_id=milestone_id,
            status=MilestoneAttemptStatus.IN_PROGRESS,
            attempts_count=0,
            started_at=datetime.utcnow(),
        )
        db.add(attempt)
        db.commit()
        return attempt.id

    @staticmethod
    def start_variant(db: Session, project_attempt_id: str, milestone_id: str) -> str:
        """Create a template-authored variant attempt; never grants state."""
        from app.models.academy import MilestoneAttempt
        attempt = MilestoneAttempt(id=str(uuid4()), project_attempt_id=project_attempt_id, project_milestone_id=milestone_id, status=MilestoneAttemptStatus.IN_PROGRESS, mode=MilestoneAttemptMode.VARIANT, started_at=datetime.utcnow())
        db.add(attempt); db.commit()
        return attempt.id

    @staticmethod
    def attempts(db: Session, project_attempt_id: str, milestone_id: str):
        from app.models.academy import MilestoneAttempt
        return list(db.execute(select(MilestoneAttempt).where(MilestoneAttempt.project_attempt_id == project_attempt_id, MilestoneAttempt.project_milestone_id == milestone_id).order_by(MilestoneAttempt.created_at)).scalars())

    @staticmethod
    def record_attempt(db: Session, milestone_attempt_id: str) -> None:
        """Increment attempt count."""
        from app.models.academy import MilestoneAttempt

        attempt = db.query(MilestoneAttempt).filter(MilestoneAttempt.id == milestone_attempt_id).first()
        if attempt:
            attempt.attempts_count += 1
            db.commit()


class HintPolicyEngine:
    """Deterministic H0-H5 assistance unlock policy (frozen design §H.2)."""

    @staticmethod
    def can_unlock_hint(
        level: AssistanceLevel,
        current_level: Optional[AssistanceLevel],
        attempts_count: int,
        minutes_since_last_level: int,
        failed_attempts: int,
    ) -> bool:
        """Check if hint level is unlocked (deterministic, no LLM)."""

        if level == AssistanceLevel.H1:
            return True  # Always available on "I'm stuck"

        if level == AssistanceLevel.H2:
            return current_level == AssistanceLevel.H1 and attempts_count >= 1

        if level == AssistanceLevel.H3:
            return current_level == AssistanceLevel.H2 and (failed_attempts >= 1 or minutes_since_last_level >= 10)

        if level == AssistanceLevel.H4:
            return current_level == AssistanceLevel.H3 and failed_attempts >= 1

        if level == AssistanceLevel.H5:
            # L0/L1: after H4. L2+: after H4 + 2 fails or study mode
            return current_level == AssistanceLevel.H4

        return False

    @staticmethod
    def record_assistance_level(db: Session, milestone_attempt_id: str, level: AssistanceLevel) -> None:
        """Record max assistance level used on this milestone."""
        from app.models.academy import MilestoneAttempt

        attempt = db.query(MilestoneAttempt).filter(MilestoneAttempt.id == milestone_attempt_id).first()
        if attempt:
            if attempt.max_assistance_level is None or level > attempt.max_assistance_level:
                attempt.max_assistance_level = level
            db.commit()


class AssistanceProvenance:
    """Track and persist help provenance on evidence."""

    @staticmethod
    def record_help_on_evidence(
        db: Session,
        evidence_id: str,
        assistance_level: AssistanceLevel,
        execution_verification: str = "not_applicable",
    ) -> None:
        """Stamp evidence with assistance level and verification method."""
        from app.models.learner import LearningEvidence

        evidence = db.query(LearningEvidence).filter(LearningEvidence.id == evidence_id).first()
        if evidence:
            evidence.assistance_level = assistance_level
            evidence.execution_verification = execution_verification
            db.commit()


class EvidenceQualification:
    """Pipeline: Project Activity → Candidate Evidence → Qualification → Learning Evidence.

    Frozen rules:
    - H5 alone cannot qualify PRACTICED
    - H5 alone cannot qualify DEMONSTRATED
    - Study Mode variant at ≤H2 after H5 shown can qualify
    - Modification/Reproduction at ≤H2 shows independence after help
    """

    @staticmethod
    def h5_alone_cannot_qualify_practiced(assistance_level: Optional[AssistanceLevel]) -> bool:
        """H5 work alone does NOT qualify PRACTICED (max EXPOSED)."""
        return assistance_level == AssistanceLevel.H5

    @staticmethod
    def can_qualify_practiced(assistance_level: Optional[AssistanceLevel]) -> bool:
        """PRACTICED requires assistance ≤ H4."""
        if assistance_level is None:
            return True
        return assistance_level in (
            AssistanceLevel.H0,
            AssistanceLevel.H1,
            AssistanceLevel.H2,
            AssistanceLevel.H3,
            AssistanceLevel.H4,
        )

    @staticmethod
    def can_qualify_demonstrated(
        assistance_level: Optional[AssistanceLevel],
        is_platform_verified: bool,
        is_modification_at_h2_or_less: bool = False,
    ) -> bool:
        """DEMONSTRATED requires:
        - platform-verified work at ≤H2, OR
        - modification/reproduction at ≤H2 after help
        """
        if not is_platform_verified:
            return is_modification_at_h2_or_less

        if assistance_level is None:
            return True
        return assistance_level in (AssistanceLevel.H0, AssistanceLevel.H1, AssistanceLevel.H2)

    @staticmethod
    def qualify_candidate(db: Session, candidate, *, concept_version_id: str):
        """Move candidate evidence through the existing append-only evidence service."""
        from app.db.enums import EvidenceRefType, EvidenceType, GradingMode
        from app.services.learning_evidence_service import LearningEvidenceService
        if not candidate.passed or candidate.concept_id is None:
            return None
        if not EvidenceQualification.can_qualify_practiced(candidate.assistance_level):
            return None
        evidence = LearningEvidenceService(db).record_evidence(user_id=candidate.user_id, concept_id=candidate.concept_id, concept_version_id=concept_version_id, evidence_type=EvidenceType.LAB, grader=GradingMode.DETERMINISTIC, passed=True, ref_type=EvidenceRefType.NONE, ref_id=candidate.id, assistance_level=candidate.assistance_level, execution_verification=candidate.execution_verification, milestone_attempt_id=candidate.milestone_attempt_id, commit=False)
        candidate.qualified = True
        candidate.learning_evidence_id = evidence.id
        db.commit()
        return evidence
