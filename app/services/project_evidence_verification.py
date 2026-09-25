"""AIL.5C prerequisite (P0-2): derive project-evidence facts from PLATFORM
records instead of trusting client claims.

AIL.5B's evidence route used to accept a client-supplied ``passed`` and
``execution_verification``. A learner could therefore mint
``platform_verified`` evidence with a single request. Now the pass/fail and
the verification level are *derived* from the cited record, and only when the
cited record provably belongs to the learner. A claim that cannot be tied to
a platform record is recorded honestly as ``self_reported`` — it can still
support PRACTICED, and never DEMONSTRATED.

Ownership never comes from project membership: every learner is OWNER of the
shared per-organisation AIL project, so membership proves nothing.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.db.enums import (
    AgentRunStatus,
    EvaluationFinding,
    EvaluationRunStatus,
    ExecutionVerification,
    ExperimentStatus,
)
from app.models.evaluation_runs import EvaluationRun
from app.models.lab import Experiment
from app.models.tasks import AgentRun, Task, TaskRun

SUPPORTED_SOURCE_TYPES = ("evaluation_run", "experiment", "agent_run")


@dataclass(frozen=True)
class DerivedFacts:
    passed: bool
    execution_verification: ExecutionVerification
    basis: str  # machine-readable reason: which platform record decided this


def _owned_agent_run(db: Session, user_id: str, agent_run_id: str) -> Optional[AgentRun]:
    agent_run = db.get(AgentRun, agent_run_id)
    if agent_run is None:
        return None
    task_run = db.get(TaskRun, agent_run.task_run_id)
    task = db.get(Task, task_run.task_id) if task_run is not None else None
    if task is None or task.created_by != user_id:
        return None
    return agent_run


def derive_candidate_facts(
    db: Session,
    user_id: str,
    source_type: str,
    source_id: Optional[str],
    *,
    claimed_passed: bool,
) -> DerivedFacts:
    """Return the platform-derived facts for a candidate-evidence claim.

    Raises ``LookupError`` when a cited platform record does not exist or is
    not the learner's own (callers answer 404 — never confirming existence).
    """
    if source_id is None or source_type not in SUPPORTED_SOURCE_TYPES:
        return DerivedFacts(bool(claimed_passed), ExecutionVerification.SELF_REPORTED, "self_reported_claim")

    if source_type == "evaluation_run":
        run = db.get(EvaluationRun, source_id)
        if run is None or run.requested_by_user_id != user_id:
            raise LookupError("Evaluation run not found")
        if run.status != EvaluationRunStatus.COMPLETED:
            return DerivedFacts(
                False, ExecutionVerification.PLATFORM_VERIFIED, "evaluation_run_not_completed"
            )
        findings = [r.finding for r in run.criterion_results]
        passed = bool(findings) and all(
            f in (EvaluationFinding.MET, EvaluationFinding.NOT_APPLICABLE) for f in findings
        )
        return DerivedFacts(passed, ExecutionVerification.PLATFORM_VERIFIED, "evaluation_run_findings")

    if source_type == "experiment":
        experiment = db.get(Experiment, source_id)
        if experiment is None or experiment.user_id != user_id:
            raise LookupError("Experiment not found")
        return DerivedFacts(
            experiment.status == ExperimentStatus.COMPLETED,
            ExecutionVerification.PLATFORM_VERIFIED,
            "experiment_status",
        )

    agent_run = _owned_agent_run(db, user_id, source_id)
    if agent_run is None:
        raise LookupError("Agent run not found")
    return DerivedFacts(
        agent_run.status == AgentRunStatus.COMPLETED,
        ExecutionVerification.PLATFORM_VERIFIED,
        "agent_run_status",
    )
