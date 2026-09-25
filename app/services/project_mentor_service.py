"""Project Mentor adapter: Professor execution plus deterministic project policy."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import AssistanceLevel
from app.errors import ConflictError, ForbiddenError
from app.models.academy import MilestoneAttempt, ProjectAttempt, ProjectMilestone
from app.schemas.professor import ProfessorContextRequest, ProfessorIntent, ProfessorTarget, ProfessorTargetType
from app.services.assessment_mode_guard import AssessmentModeGuard
from app.services.build_with_me_service import HintPolicyEngine, AssistanceProvenance
from app.services.professor_execution_service import ProfessorExecutionService


class ProjectMentorService:
    def __init__(self, db: Session):
        self.db = db

    def ask(self, user, project_attempt_id: str, milestone_id: str, question: str, requested_level: AssistanceLevel):
        row = self.db.execute(
            select(MilestoneAttempt, ProjectAttempt, ProjectMilestone)
            .join(ProjectAttempt, ProjectAttempt.id == MilestoneAttempt.project_attempt_id)
            .join(ProjectMilestone, ProjectMilestone.id == MilestoneAttempt.project_milestone_id)
            .where(MilestoneAttempt.project_attempt_id == project_attempt_id, MilestoneAttempt.project_milestone_id == milestone_id, ProjectAttempt.user_id == user.id)
        ).first()
        if row is None:
            raise ForbiddenError("Project mentor context is not available.")
        milestone_attempt, project_attempt, milestone = row
        # AIL.5C Assessment Mode: while the learner has a live assessment attempt
        # for this project or Concept, normal Mentor assistance is locked.
        # Mentor history is never touched.
        AssessmentModeGuard(self.db).assert_mentor_available(
            user.id,
            project_attempt_id=project_attempt.id,
            concept_ids=self._concept_ids(project_attempt, milestone),
        )
        current = milestone_attempt.max_assistance_level
        if not HintPolicyEngine.can_unlock_hint(requested_level, current, milestone_attempt.attempts_count, 0, milestone_attempt.attempts_count if milestone_attempt.status.value == "failed" else 0):
            raise ConflictError("That assistance level is not currently unlocked.")
        concept_id = None
        if milestone.learning_item_id:
            from app.models.concepts import LearningItem
            item = self.db.get(LearningItem, milestone.learning_item_id)
            concept_id = item.concept_id if item else None
        prompt = f"Project Mentor mode. Authorized maximum assistance: {requested_level.value}. Do not complete the learner's work. Milestone: {milestone.title}. Instructions: {milestone.instructions_md}\nLearner question: {question}"
        request = ProfessorContextRequest(intent=ProfessorIntent.ASK_PROFESSOR, question=prompt, target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=concept_id) if concept_id else None)
        result = ProfessorExecutionService(self.db).create_and_execute(user, request, mentor_lock_checked=True)
        if result.status in {"complete", "completed"}:
            milestone_attempt.max_assistance_level = max(milestone_attempt.max_assistance_level or AssistanceLevel.H0, requested_level)
            self.db.commit()
        return result, requested_level, milestone_attempt.id

    def _concept_ids(self, project_attempt, milestone):
        from app.models.academy import ProjectTemplateConceptLink
        from app.models.concepts import LearningItem

        ids = set(
            self.db.execute(
                select(ProjectTemplateConceptLink.concept_id).where(
                    ProjectTemplateConceptLink.project_template_id == project_attempt.project_template_id
                )
            ).scalars()
        )
        if milestone.learning_item_id:
            item = self.db.get(LearningItem, milestone.learning_item_id)
            if item is not None:
                ids.add(item.concept_id)
        return ids
