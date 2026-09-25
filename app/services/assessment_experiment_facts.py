"""AIL.5C: READ-ONLY platform observation of a learner's own Personal Lab
experiment (per-label MA6 finding counts).

Kept in its own tiny module so the Grader packet builder can show the platform
observation WITHOUT importing the deterministic-check catalog (and through it,
learner evidence rows). Nothing here writes; the learner's conclusion is not read.
"""

from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import EvaluationRunStatus, ExperimentStatus
from app.models.evaluation_runs import EvaluationRun
from app.models.lab import Experiment, ExperimentTaskRun
from app.models.tasks import AgentRun


def experiment_facts(db: Session, user_id: str, experiment_id: str) -> Optional[Dict[str, Any]]:
    """READ-ONLY platform observation of one of the learner's own experiments:
    per-label MA6 finding counts. The learner's conclusion is NOT read here."""
    experiment = db.get(Experiment, experiment_id)
    if experiment is None or experiment.user_id != user_id:
        return None
    labels: Dict[str, Dict[str, int]] = {}
    rows = db.execute(select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)).scalars()
    for etr in rows:
        counts = labels.setdefault(etr.label, {"met": 0, "partial": 0, "not_met": 0, "not_applicable": 0})
        for agent_run in db.execute(select(AgentRun).where(AgentRun.task_run_id == etr.task_run_id)).scalars():
            latest = db.execute(
                select(EvaluationRun)
                .where(EvaluationRun.subject_agent_run_id == agent_run.id, EvaluationRun.status == EvaluationRunStatus.COMPLETED)
                .order_by(EvaluationRun.created_at.desc())
            ).scalars().first()
            if latest is not None:
                for result in latest.criterion_results:
                    counts[result.finding.value] += 1
    return {
        "experiment_id": experiment.id,
        "status": experiment.status.value,
        "completed": experiment.status == ExperimentStatus.COMPLETED,
        "labels": labels,
        "has_findings": any(sum(c.values()) for c in labels.values()),
    }
