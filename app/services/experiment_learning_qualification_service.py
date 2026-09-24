"""Experiment learning qualification — AIL.3C.

Answers one question: "does this Experiment provide legitimate hands-on LAB
evidence for its Concept?" It never predicts or assigns mastery;
``LearnerStateService`` derives the ladder from all canonical evidence.

Execution/evaluation completeness is AIL.3B's own contract, reused from
``ExperimentExecutionService`` rather than modelled a second time:

- the required executions are the ``ExperimentTaskRun`` slots whose Task Run
  COMPLETED;
- evaluation is required only when ``config_snapshot`` names an
  ``evaluation_definition_version_id``;
- for each slot's Agent Run the latest ``EvaluationRun`` on that exact
  definition version is authoritative.

MET / PARTIAL / NOT_MET findings describe how the *candidates* performed and
stay canonical MA6 evidence. They do not decide whether the learner completed
a legitimate hands-on activity, so they never disqualify an experiment. What
does: every required execution must carry a completed evaluation with at least
one meaningful finding (anything other than NOT_APPLICABLE). ``passed=True``
on the LAB evidence therefore means "qualifying hands-on activity", never
"the candidates did well". Nothing is copied from MA6 into the evidence row
and no score is invented.
"""

from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.enums import (
    EvaluationFinding,
    EvaluationRunStatus,
    EvidenceRefType,
    EvidenceType,
    ExperimentStatus,
    GradingMode,
    TaskRunStatus,
)
from app.errors import NotFoundError
from app.models.concepts import ConceptVersion
from app.models.evaluation_definitions import EvaluationCriterion
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.lab import Experiment
from app.models.learner import LearningEvidence
from app.models.tasks import AgentRun, TaskRun
from app.services.experiment_execution_service import ExperimentExecutionService
from app.services.learning_evidence_service import LearningEvidenceService

READY = "READY"
ALREADY_COUNTED = "ALREADY_COUNTED"
MISSING_CONCEPT = "MISSING_CONCEPT"
CONCEPT_REBIND_REQUIRED = "CONCEPT_REBIND_REQUIRED"
EXPERIMENT_INCOMPLETE = "EXPERIMENT_INCOMPLETE"
EVALUATION_NOT_CONFIGURED = "EVALUATION_NOT_CONFIGURED"
EVALUATION_PENDING = "EVALUATION_PENDING"
EVALUATION_FAILED = "EVALUATION_FAILED"
EVALUATION_INCOMPLETE = "EVALUATION_INCOMPLETE"
NO_MEANINGFUL_EVALUATION = "NO_MEANINGFUL_EVALUATION"

_MESSAGES = {
    READY: "Ready to count toward learning.",
    ALREADY_COUNTED: "Already counted toward learning.",
    MISSING_CONCEPT: "Choose a Concept first.",
    CONCEPT_REBIND_REQUIRED: "Choose the Concept again to confirm which version this experiment relates to.",
    EXPERIMENT_INCOMPLETE: "The experiment has not finished running successfully yet.",
    EVALUATION_NOT_CONFIGURED: "This experiment was not evaluated, so there is no verified result to count.",
    EVALUATION_PENDING: "Evaluation is still pending.",
    EVALUATION_FAILED: "Evaluation failed.",
    EVALUATION_INCOMPLETE: "Some required evaluations are incomplete.",
    NO_MEANINGFUL_EVALUATION: "The evaluation produced no meaningful results to learn from.",
}


class ExperimentLearningQualificationService:
    def __init__(self, db: Session):
        self.db = db
        self.evidence_service = LearningEvidenceService(db)

    # -- Public ---------------------------------------------------------------

    def assess(self, user_id: str, experiment_id: str) -> Dict:
        experiment = self._owned(user_id, experiment_id)
        existing = self._find_evidence(user_id, experiment.id)
        if existing is not None:
            return self._result(ALREADY_COUNTED, experiment, evidence_id=existing.id)

        if not experiment.concept_id:
            return self._result(MISSING_CONCEPT, experiment)
        version = (
            self.db.get(ConceptVersion, experiment.concept_version_id) if experiment.concept_version_id else None
        )
        if version is None or version.concept_id != experiment.concept_id:
            return self._result(CONCEPT_REBIND_REQUIRED, experiment)

        execution = ExperimentExecutionService(self.db)
        experiment = execution.refresh(experiment)
        execution_status, runs = execution._execution_state(experiment)
        if experiment.status == ExperimentStatus.CANCELLED or execution_status != "COMPLETED":
            return self._result(EXPERIMENT_INCOMPLETE, experiment)

        definition_id = (experiment.config_snapshot or {}).get("evaluation_definition_version_id")
        if not definition_id:
            return self._result(EVALUATION_NOT_CONFIGURED, experiment)
        evaluation_status = execution._evaluation_state(experiment, runs)
        if evaluation_status == "FAILED":
            return self._result(EVALUATION_FAILED, experiment)
        if evaluation_status != "COMPLETED":
            return self._result(EVALUATION_PENDING, experiment)

        evaluations = self._latest_evaluations(runs, definition_id)
        if evaluations is None:
            return self._result(EVALUATION_INCOMPLETE, experiment)

        findings_per_evaluation = self._findings(evaluations, definition_id)
        if findings_per_evaluation is None:
            return self._result(EVALUATION_INCOMPLETE, experiment)
        if any(not any(f in _MEANINGFUL for f in findings) for findings in findings_per_evaluation):
            return self._result(NO_MEANINGFUL_EVALUATION, experiment)
        return self._result(READY, experiment, ready=True)

    def count_toward_learning(
        self, user_id: str, experiment_id: str
    ) -> Tuple[Optional[LearningEvidence], bool, Dict]:
        """Returns (evidence, created, assessment). ``created`` is False for a
        retry or a lost race; evidence is None when not READY."""
        assessment = self.assess(user_id, experiment_id)
        if assessment["status"] == ALREADY_COUNTED:
            return self._find_evidence(user_id, experiment_id), False, assessment
        if not assessment["ready"]:
            return None, False, assessment

        experiment = self._owned(user_id, experiment_id)
        try:
            evidence = self.evidence_service.record_evidence(
                user_id=user_id,
                concept_id=experiment.concept_id,
                concept_version_id=experiment.concept_version_id,
                evidence_type=EvidenceType.LAB,
                grader=GradingMode.DETERMINISTIC,
                score=None,
                passed=True,
                ref_type=EvidenceRefType.EXPERIMENT,
                ref_id=experiment.id,
            )
        except IntegrityError:
            # A concurrent request inserted first; the partial unique index
            # rejected this one. Re-read the canonical row. If none exists the
            # failure was something else (CHECK/FK) and must not be masked.
            self.db.rollback()
            existing = self._find_evidence(user_id, experiment_id)
            if existing is None:
                raise
            return existing, False, self.assess(user_id, experiment_id)
        return evidence, True, self.assess(user_id, experiment_id)

    # -- Internals ------------------------------------------------------------

    def _owned(self, user_id: str, experiment_id: str) -> Experiment:
        experiment = self.db.execute(
            select(Experiment).where(Experiment.id == experiment_id, Experiment.user_id == user_id)
        ).scalar_one_or_none()
        if experiment is None:
            raise NotFoundError("Experiment not found.")
        return experiment

    def _find_evidence(self, user_id: str, experiment_id: str) -> Optional[LearningEvidence]:
        return self.db.execute(
            select(LearningEvidence).where(
                LearningEvidence.user_id == user_id,
                LearningEvidence.ref_type == EvidenceRefType.EXPERIMENT,
                LearningEvidence.ref_id == experiment_id,
            )
        ).scalars().first()

    def _latest_evaluations(self, runs: List[TaskRun], definition_id: str) -> Optional[List[EvaluationRun]]:
        """Latest EvaluationRun on the configured definition version for each
        COMPLETED slot's Agent Run (AIL.3B's rule). None if any is missing or
        not COMPLETED."""
        evaluations: List[EvaluationRun] = []
        for task_run in runs:
            if task_run.status != TaskRunStatus.COMPLETED:
                continue
            agent_run = self.db.execute(
                select(AgentRun).where(AgentRun.task_run_id == task_run.id)
            ).scalar_one_or_none()
            if agent_run is None:
                return None
            evaluation = ExperimentExecutionService(self.db).latest_evaluation(agent_run.id, definition_id)
            if evaluation is None or evaluation.status != EvaluationRunStatus.COMPLETED:
                return None
            evaluations.append(evaluation)
        return evaluations or None

    def _findings(
        self, evaluations: List[EvaluationRun], definition_id: str
    ) -> Optional[List[List[EvaluationFinding]]]:
        """Findings per evaluation, or None unless every evaluation covers
        every criterion of the configured definition version."""
        expected = set(
            self.db.execute(
                select(EvaluationCriterion.key).where(
                    EvaluationCriterion.evaluation_definition_version_id == definition_id
                )
            ).scalars()
        )
        per_evaluation: List[List[EvaluationFinding]] = []
        for evaluation in evaluations:
            results = list(
                self.db.execute(
                    select(EvaluationCriterionResult).where(
                        EvaluationCriterionResult.evaluation_run_id == evaluation.id
                    )
                ).scalars()
            )
            if not results or not expected.issubset({r.criterion_key for r in results}):
                return None
            per_evaluation.append([r.finding for r in results])
        return per_evaluation

    @staticmethod
    def _result(status: str, experiment: Experiment, *, ready: bool = False, **extra) -> Dict:
        return {
            "status": status,
            "ready": ready,
            "message": _MESSAGES[status],
            "experiment_id": experiment.id,
            "concept_id": experiment.concept_id,
            "concept_version_id": experiment.concept_version_id,
            **extra,
        }


_MEANINGFUL = (EvaluationFinding.MET, EvaluationFinding.PARTIAL, EvaluationFinding.NOT_MET)
