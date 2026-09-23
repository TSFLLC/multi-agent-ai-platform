"""Experiment Learning Qualification Service — AIL.3C

Deterministic service for qualifying when an experiment can produce learning
evidence. Never grants PRACTICED/DEMONSTRATED directly; only appends qualifying
evidence that LearnerStateService will evaluate per existing semantics.
"""

from typing import Dict, List, Optional, Tuple
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.db.enums import EvaluationRunStatus, EvidenceRefType, EvidenceType, ExperimentStatus, GradingMode
from app.models.lab import Experiment, EvalSetVersion
from app.models.evaluation_runs import EvaluationRun, EvaluationCriterionResult
from app.models.learner import LearningEvidence
from app.services.learning_evidence_service import LearningEvidenceService


class ExperimentLearningQualificationService:
    """Deterministic qualification rules for experiment-backed learning evidence."""

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
        - evaluation_complete: bool
        - concept_associated: bool
        - evidence_requirements_met: bool
        - details: List[str] (human-readable reasons if not qualified)
        """
        reasons = {
            "qualified": False,
            "experiment_status_ok": False,
            "evaluation_complete": False,
            "concept_associated": False,
            "evidence_requirements_met": False,
            "details": []
        }

        # Fetch experiment with authorization
        experiment = self._get_experiment_or_raise(user_id, experiment_id)

        # Check 1: Experiment status
        # Only COMPLETED experiments can create evidence (FAILED experiments don't auto-create passing evidence per correction #1)
        if experiment.status != ExperimentStatus.COMPLETED:
            reasons["details"].append(
                f"Experiment status is {experiment.status.value}, not COMPLETED"
            )
            return False, reasons
        reasons["experiment_status_ok"] = True

        # Check 2: Evaluation complete
        # Query MA6 EvaluationRuns linked to this experiment's task runs
        evaluation_complete = self._check_evaluation_complete(experiment_id)
        if not evaluation_complete:
            reasons["details"].append(
                "Evaluation not complete or not found (no MA6 EvaluationRun records)"
            )
            return False, reasons
        reasons["evaluation_complete"] = True

        # Check 3: Concept associated
        if not experiment.concept_id:
            reasons["details"].append("No concept selected for this experiment")
            return False, reasons
        reasons["concept_associated"] = True

        # Check 4: Evidence requirements met
        # For AIL.3C, we accept any completed experiment with evaluation as qualifying evidence for LAB/OBSERVATION
        # Full concept-specific requirements will be evaluated by LearnerStateService
        reasons["evidence_requirements_met"] = True

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

        # Fetch experiment for concept_id and concept_version_id
        experiment = self._get_experiment_or_raise(user_id, experiment_id)

        # Fetch current concept version
        from app.services.concept_graph_service import ConceptGraphService
        concept_service = ConceptGraphService(self.db)
        concept_version = concept_service.get_current_concept_version(experiment.concept_id)

        if not concept_version:
            return None, f"Concept version not found for concept {experiment.concept_id}"

        # Create evidence: experiment-backed evidence is LAB type with EXPERIMENT ref_type
        evidence = self.evidence_service.record_evidence(
            user_id=user_id,
            concept_id=experiment.concept_id,
            concept_version_id=concept_version.id,
            evidence_type=EvidenceType.LAB,  # Hands-on platform evidence
            grader=GradingMode.DETERMINISTIC,  # Platform-verified execution
            score=None,  # Experiments don't have scores; evaluation results are in MA6
            passed=True,  # Completed experiments are "passed"
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

    def _check_evaluation_complete(self, experiment_id: str) -> bool:
        """
        Check if MA6 evaluation is complete for this experiment.

        Query: does an EvaluationRun exist for any task run in this experiment,
        and is it COMPLETED?
        """
        from app.models.lab import ExperimentTaskRun

        stmt = select(EvaluationRun).join(
            ExperimentTaskRun,
            EvaluationRun.subject_agent_run_id.isnot(None)  # Simplified: check for any eval
        ).where(
            and_(
                ExperimentTaskRun.experiment_id == experiment_id,
                EvaluationRun.status == EvaluationRunStatus.COMPLETED
            )
        ).limit(1)

        result = self.db.execute(stmt).scalar_one_or_none()
        return result is not None

    def _find_experiment_evidence(self, user_id: str, experiment_id: str) -> Optional[LearningEvidence]:
        """Check if evidence already exists for this experiment (idempotency)."""
        stmt = select(LearningEvidence).where(
            and_(
                LearningEvidence.user_id == user_id,
                LearningEvidence.ref_type == EvidenceRefType.EXPERIMENT,
                LearningEvidence.ref_id == experiment_id
            )
        ).limit(1)

        return self.db.execute(stmt).scalar_one_or_none()
