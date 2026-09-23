"""Experiment Learning Qualification Service — AIL.3C (CORRECTED)

Deterministic service for qualifying when an experiment can produce learning
evidence. Never grants PRACTICED/DEMONSTRATED directly; only appends qualifying
evidence that LearnerStateService will evaluate per existing semantics.

KEY PRINCIPLE: Qualification is NOT about predicting mastery. It's about
answering: "Does this completed experiment provide legitimate hands-on LAB
evidence for this Concept?"

LearnerStateService determines the resulting ladder state using all canonical
evidence requirements.
"""

from typing import Dict, List, Optional, Tuple
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.db.enums import EvaluationFinding, EvaluationRunStatus, EvidenceRefType, EvidenceType, ExperimentStatus, ExperimentType, GradingMode
from app.models.lab import Experiment, ExperimentTaskRun
from app.models.evaluation_runs import EvaluationRun, EvaluationCriterionResult
from app.models.learner import LearningEvidence
from app.models.tasks import TaskRun, AgentRun
from app.services.learning_evidence_service import LearningEvidenceService


class ExperimentLearningQualificationService:
    """Deterministic qualification rules for experiment-backed learning evidence.

    FIXED: Proper MA6 association, canonical evaluation evidence, no synthesis.
    """

    def __init__(self, db: Session):
        self.db = db
        self.evidence_service = LearningEvidenceService(db)

    def can_count_toward_learning(
        self,
        user_id: str,
        experiment_id: str
    ) -> Tuple[bool, Dict[str, any]]:
        """
        Deterministic check: can this experiment create learning evidence?

        Returns: (qualified: bool, reasons: dict with details)

        Reasons dict includes:
        - qualified: bool (overall result)
        - experiment_status_ok: bool
        - concept_associated: bool
        - evaluation_complete: bool
        - details: List[str] (human-readable reasons if not qualified)
        """
        reasons = {
            "qualified": False,
            "experiment_status_ok": False,
            "concept_associated": bool,
            "evaluation_complete": False,
            "details": []
        }

        # Fetch experiment with authorization
        experiment = self._get_experiment_or_raise(user_id, experiment_id)

        # Check 1: Experiment status
        # Only COMPLETED experiments can create evidence. FAILED explicitly excluded.
        # Evidence from FAILED experiments is non-qualifying (not a pass).
        if experiment.status != ExperimentStatus.COMPLETED:
            reasons["details"].append(
                f"Experiment status is {experiment.status.value}, not COMPLETED"
            )
            return False, reasons
        reasons["experiment_status_ok"] = True

        # Check 2: Concept associated
        # User must have selected a concept for learning purposes.
        if not experiment.concept_id:
            reasons["details"].append("No concept selected for this experiment")
            return False, reasons
        reasons["concept_associated"] = True

        # Check 3: Concept version frozen
        # When experiment is counted toward learning, freeze the version.
        # This check ensures the version is available (checked at count time).
        if not experiment.concept_version_id:
            reasons["details"].append("Concept version not frozen at experiment creation")
            return False, reasons

        # Check 4: Evaluation complete
        # Query canonical MA6 EvaluationRuns using correct provenance chain:
        # Experiment → ExperimentTaskRun → TaskRun → AgentRun → EvaluationRun
        # ALL required evaluations must be complete.
        evaluation_complete, eval_reason = self._check_evaluation_complete(experiment_id, experiment.experiment_type)
        if not evaluation_complete:
            reasons["details"].append(eval_reason)
            return False, reasons
        reasons["evaluation_complete"] = True

        reasons["qualified"] = True
        return True, reasons

    def count_toward_learning(
        self,
        user_id: str,
        experiment_id: str
    ) -> Tuple[Optional[LearningEvidence], str]:
        """
        Idempotent action: create learning evidence if experiment qualifies.

        Returns: (evidence: LearningEvidence | None, message: str)

        Idempotency: returns existing evidence if already created for this experiment.
        Database-level unique constraint prevents duplicates on concurrent requests.
        """
        # Verify qualification
        can_count, reasons = self.can_count_toward_learning(user_id, experiment_id)

        if not can_count:
            reason_msg = "; ".join(reasons["details"])
            return None, f"Cannot count toward learning: {reason_msg}"

        # Check idempotency: does evidence for this experiment already exist?
        existing = self._find_experiment_evidence(user_id, experiment_id)
        if existing:
            return existing, "Evidence already recorded for this experiment"

        # Fetch experiment to get frozen concept info
        experiment = self._get_experiment_or_raise(user_id, experiment_id)

        # Derive passed from canonical evaluation evidence, not from status alone
        passed, derivation_reason = self._derive_passed_from_evaluation(experiment_id)
        if not passed:
            return None, f"Cannot create passing evidence: {derivation_reason}"

        # Create evidence: experiment-backed evidence is LAB type with EXPERIMENT ref_type
        # passed is derived from canonical platform evidence, grader is DETERMINISTIC
        evidence = self.evidence_service.record_evidence(
            user_id=user_id,
            concept_id=experiment.concept_id,
            concept_version_id=experiment.concept_version_id,  # Use frozen version
            evidence_type=EvidenceType.LAB,  # Hands-on platform evidence
            grader=GradingMode.DETERMINISTIC,  # Platform-verified execution
            score=None,  # Experiments don't have scores; evaluation results are in MA6
            passed=passed,  # Derived from canonical evaluation evidence
            ref_type=EvidenceRefType.EXPERIMENT,
            ref_id=experiment_id,
        )

        return evidence, "Learning evidence recorded"

    def _get_experiment_or_raise(self, user_id: str, experiment_id: str) -> Experiment:
        """Fetch experiment with authorization check."""
        stmt = select(Experiment).where(
            and_(
                Experiment.id == experiment_id,
                Experiment.user_id == user_id  # Authorization: user owns this experiment
            )
        )
        experiment = self.db.execute(stmt).scalar_one_or_none()
        if not experiment:
            raise ValueError(f"Experiment not found or not owned by user: {experiment_id}")
        return experiment

    def _check_evaluation_complete(
        self,
        experiment_id: str,
        experiment_type: ExperimentType
    ) -> Tuple[bool, str]:
        """
        Check if ALL required MA6 evaluations are complete for this experiment.

        Trace: Experiment → ExperimentTaskRun → TaskRun → AgentRun → EvaluationRun

        Returns: (complete: bool, reason: str)

        Rules:
        - Every AgentRun produced by the experiment must have a COMPLETED EvaluationRun
        - Pending/Running/Failed evaluations cause rejection
        - Unrelated evaluations are ignored (must join through experiment)
        """
        # Step 1: Get all AgentRuns for this experiment via the proper chain
        stmt = select(AgentRun).join(
            TaskRun,
            TaskRun.id == AgentRun.task_run_id
        ).where(
            TaskRun.experiment_id == experiment_id
        )
        agent_runs = list(self.db.execute(stmt).scalars())

        if not agent_runs:
            return False, "No agent runs found for this experiment"

        # Step 2: For each AgentRun, verify it has a COMPLETED EvaluationRun
        for agent_run in agent_runs:
            eval_stmt = select(EvaluationRun).where(
                and_(
                    EvaluationRun.subject_agent_run_id == agent_run.id,
                    EvaluationRun.status == EvaluationRunStatus.COMPLETED
                )
            ).limit(1)

            evaluation = self.db.execute(eval_stmt).scalar_one_or_none()
            if not evaluation:
                return False, f"Agent run {agent_run.id} has no completed evaluation"

        return True, ""

    def _derive_passed_from_evaluation(self, experiment_id: str) -> Tuple[bool, str]:
        """
        Derive the passed value from canonical MA6 evaluation evidence.

        CRITICAL: Do NOT synthesize. Do NOT copy criteria counts. Do NOT guess.

        A passing hands-on (LAB) result means:
        - All evaluation criteria that apply found MET or NOT_APPLICABLE
        - At least some criteria produced findings (not all NOT_APPLICABLE)
        - No NOT_MET or PARTIAL findings exist

        Returns: (passed: bool, reason: str)
        """
        # Get all evaluations for this experiment's agent runs
        stmt = select(EvaluationRun).join(
            AgentRun,
            AgentRun.id == EvaluationRun.subject_agent_run_id
        ).join(
            TaskRun,
            TaskRun.id == AgentRun.task_run_id
        ).where(
            TaskRun.experiment_id == experiment_id
        )
        evaluations = list(self.db.execute(stmt).scalars())

        if not evaluations:
            return False, "No evaluations found"

        # Check all evaluations for failed criteria
        all_passed = True
        has_findings = False

        for evaluation in evaluations:
            for criterion_result in evaluation.criterion_results:
                # NOT_APPLICABLE doesn't count against passing
                if criterion_result.finding == EvaluationFinding.NOT_APPLICABLE:
                    continue

                has_findings = True

                # Any NOT_MET or PARTIAL means not passed
                if criterion_result.finding in (EvaluationFinding.NOT_MET, EvaluationFinding.PARTIAL):
                    all_passed = False
                    return False, f"Criterion {criterion_result.criterion_key} not met: {criterion_result.rationale}"

        if not has_findings:
            return False, "All evaluation criteria were not applicable (no actual evaluation)"

        if all_passed and has_findings:
            return True, ""

        return False, "Evaluation did not establish a passing result"

    def _find_experiment_evidence(self, user_id: str, experiment_id: str) -> Optional[LearningEvidence]:
        """
        Check if evidence already exists for this experiment (idempotency).

        Database unique constraint enforces exactly-once at constraint level.
        This query is for application-level verification.
        """
        stmt = select(LearningEvidence).where(
            and_(
                LearningEvidence.user_id == user_id,
                LearningEvidence.ref_type == EvidenceRefType.EXPERIMENT,
                LearningEvidence.ref_id == experiment_id
            )
        ).limit(1)

        return self.db.execute(stmt).scalar_one_or_none()
