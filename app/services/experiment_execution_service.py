"""AIL.3B execution orchestration over the existing MA3/MA5/MA6 records."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.enums import EvaluationRunStatus, ExperimentStatus, ExperimentType, TaskRunStatus
from app.errors import ConflictError, NotFoundError
from app.models.artifacts_eval import ComparisonCandidate, ComparisonRun
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import ModelCall
from app.models.lab import (
    EvalSetVersionTask,
    Experiment,
    ExperimentAgentVersion,
    ExperimentModel,
    ExperimentTaskRun,
)
from app.models.providers import ProviderModelSnapshot
from app.models.tasks import AgentRun, TaskRun
from app.services.comparison_service import ComparisonService
from app.services.evaluation_execution_service import EvaluationExecutionService
from app.services.lab_service import LabService
from app.services.task_service import TaskService


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExperimentExecutionService:
    def __init__(self, db: Session):
        self.db = db

    def _owned(self, user_id: str, experiment_id: str) -> Experiment:
        experiment = self.db.execute(
            select(Experiment).where(Experiment.id == experiment_id, Experiment.user_id == user_id)
        ).scalar_one_or_none()
        if experiment is None:
            raise NotFoundError("Experiment not found.")
        return experiment

    def _experiment_models(self, experiment_id: str) -> List[ExperimentModel]:
        return list(self.db.execute(
            select(ExperimentModel).where(ExperimentModel.experiment_id == experiment_id).order_by(ExperimentModel.position)
        ).scalars())

    def _agent_ids(self, experiment_id: str) -> List[str]:
        return list(self.db.execute(
            select(ExperimentAgentVersion.agent_version_id)
            .where(ExperimentAgentVersion.experiment_id == experiment_id)
            .order_by(ExperimentAgentVersion.position)
        ).scalars())

    @staticmethod
    def _policy(snapshot: ProviderModelSnapshot) -> dict:
        provider_model = snapshot.provider_model_id
        return {
            "mode": "manual",
            "manual_provider_model_id": provider_model,
            "provider_model_snapshot_id": snapshot.id,
        }

    def _tasks(self, experiment: Experiment) -> List[EvalSetVersionTask]:
        rows = list(self.db.execute(
            select(EvalSetVersionTask)
            .where(EvalSetVersionTask.eval_set_version_id == experiment.eval_set_version_id)
            .order_by(EvalSetVersionTask.position)
        ).scalars())
        if not rows:
            raise ConflictError("The selected Test Kit Version has no tasks.")
        return rows

    def _record_run(self, experiment: Experiment, task_run_id: str, position: int, repetition: int, label: str) -> None:
        self.db.add(ExperimentTaskRun(
            experiment_id=experiment.id,
            task_run_id=task_run_id,
            task_position=position,
            repetition=repetition,
            label=label,
        ))
        self.db.flush()

    def launch(self, user_id: str, experiment_id: str) -> Experiment:
        experiment = self._owned(user_id, experiment_id)
        if experiment.status == ExperimentStatus.RUNNING:
            return experiment
        if experiment.status in (ExperimentStatus.COMPLETED, ExperimentStatus.FAILED, ExperimentStatus.CANCELLED):
            raise ConflictError(f"Experiment is already {experiment.status.value}.")
        if experiment.status == ExperimentStatus.AWAITING_APPROVAL:
            raise ConflictError("Experiment requires approval before execution; the current Approval Service has no experiment scope.")

        tasks = self._tasks(experiment)
        agents = self._agent_ids(experiment.id)
        models = self._experiment_models(experiment.id)
        if experiment.experiment_type == ExperimentType.MODEL_COMPARISON:
            if len(agents) != 1 or len(models) < 2:
                raise ConflictError("Model Face-off requires one Agent Version and at least two models.")
        elif experiment.experiment_type == ExperimentType.PROMPT_COMPARISON:
            if len(agents) < 2 or len(models) != 1:
                raise ConflictError("Prompt Comparison requires multiple Agent Versions and one model.")
        elif experiment.experiment_type == ExperimentType.VARIANCE and (
            len(agents) != 1 or len(models) != 1 or experiment.repetitions < 2
        ):
            raise ConflictError("Consistency Check requires one Agent Version, one model, and repetitions >= 2.")

        if self.db.execute(select(ExperimentTaskRun.id).where(ExperimentTaskRun.experiment_id == experiment.id)).first():
            raise ConflictError("Experiment execution has already been initialized.")

        experiment.status = ExperimentStatus.RUNNING
        experiment.config_snapshot = {**(experiment.config_snapshot or {}), "execution_enabled": True}
        self.db.commit()

        try:
            for task_row in tasks:
                snapshot = task_row.task_snapshot
                if experiment.experiment_type == ExperimentType.VARIANCE:
                    for repetition in range(1, experiment.repetitions + 1):
                        model_snapshot = self.db.get(ProviderModelSnapshot, models[0].provider_model_snapshot_id)
                        if model_snapshot is None:
                            raise ConflictError("The selected provider model snapshot no longer exists.")
                        run = TaskService(self.db).start_task_run(
                            task_id=task_row.task_id,
                            agent_version_id=agents[0],
                            budget_id=experiment.budget_id,
                            experiment_id=experiment.id,
                            model_policy_override=self._policy(model_snapshot),
                            frozen_task_snapshot=snapshot,
                        )
                        self._record_run(experiment, run.id, task_row.position, repetition, f"repetition-{repetition}")
                    self.db.commit()
                    continue

                for repetition in range(1, experiment.repetitions + 1):
                    if experiment.experiment_type == ExperimentType.MODEL_COMPARISON:
                        candidate_specs = []
                        for model in models:
                            model_snapshot = self.db.get(ProviderModelSnapshot, model.provider_model_snapshot_id)
                            if model_snapshot is None:
                                raise ConflictError("The selected provider model snapshot no longer exists.")
                            candidate_specs.append({
                                "agent_version_id": agents[0],
                                "label": f"model-{model.position + 1}",
                                "model_policy_override": self._policy(model_snapshot),
                            })
                    else:
                        model_snapshot = self.db.get(ProviderModelSnapshot, models[0].provider_model_snapshot_id)
                        if model_snapshot is None:
                            raise ConflictError("The selected provider model snapshot no longer exists.")
                        candidate_specs = [
                            {
                                "agent_version_id": agent_id,
                                "label": f"agent-{index + 1}",
                                "model_policy_override": self._policy(model_snapshot),
                            }
                            for index, agent_id in enumerate(agents)
                        ]
                    comparison = ComparisonService(self.db).create_comparison(
                        task_id=task_row.task_id,
                        candidates=candidate_specs,
                        budget_id=experiment.budget_id,
                        experiment_id=experiment.id,
                        experiment_task_position=task_row.position,
                        experiment_repetition=repetition,
                        frozen_task_snapshot=snapshot,
                    )
                    ComparisonService(self.db).launch_comparison(comparison.id)
                    candidates = list(self.db.execute(
                        select(ComparisonCandidate).where(ComparisonCandidate.comparison_run_id == comparison.id)
                    ).scalars())
                    for candidate in candidates:
                        if candidate.task_run_id:
                            self._record_run(experiment, candidate.task_run_id, task_row.position, repetition, candidate.label)
                    self.db.commit()
        except Exception:
            self.db.rollback()
            failed = self._owned(user_id, experiment_id)
            failed.status = ExperimentStatus.FAILED
            failed.config_snapshot = {
                **(failed.config_snapshot or {}),
                "execution_error": "Experiment setup failed before all runs were initialized.",
            }
            self.db.commit()
            raise
        self.db.refresh(experiment)
        return experiment

    def refresh(self, experiment: Experiment) -> Experiment:
        execution_status, runs = self._execution_state(experiment)
        if experiment.status == ExperimentStatus.RUNNING and runs and execution_status in {
            "COMPLETED", "PARTIAL", "FAILED"
        }:
            evaluation_status = self._evaluation_state(experiment, runs)
            if execution_status == "FAILED":
                experiment.status = ExperimentStatus.FAILED
            elif execution_status == "PARTIAL":
                # Preserve partial evidence without presenting the run as a
                # successful complete experiment. The read contract exposes
                # PARTIAL explicitly; the lifecycle column remains compatible
                # with the AIL.3A enum.
                experiment.status = ExperimentStatus.RUNNING
            elif not self._evaluation_required(experiment) or evaluation_status == "COMPLETED":
                experiment.status = ExperimentStatus.COMPLETED
            elif evaluation_status == "FAILED":
                experiment.status = ExperimentStatus.FAILED
            # PENDING/RUNNING evaluation deliberately keeps the experiment in
            # RUNNING until canonical MA6 evidence reaches a terminal success.
            self.db.commit()
        return experiment

    @staticmethod
    def _evaluation_required(experiment: Experiment) -> bool:
        return bool((experiment.config_snapshot or {}).get("evaluation_definition_version_id"))

    def _execution_state(self, experiment: Experiment) -> Tuple[str, List[TaskRun]]:
        runs = list(self.db.execute(
            select(TaskRun).join(ExperimentTaskRun, ExperimentTaskRun.task_run_id == TaskRun.id)
            .where(ExperimentTaskRun.experiment_id == experiment.id)
        ).scalars())
        if not runs or any(run.status not in (
            TaskRunStatus.COMPLETED, TaskRunStatus.FAILED, TaskRunStatus.CANCELLED
        ) for run in runs):
            return "RUNNING", runs
        completed = any(run.status == TaskRunStatus.COMPLETED for run in runs)
        failed_or_cancelled = any(run.status in (TaskRunStatus.FAILED, TaskRunStatus.CANCELLED) for run in runs)
        if completed and failed_or_cancelled:
            return "PARTIAL", runs
        return "COMPLETED" if completed else "FAILED", runs

    def _evaluation_state(self, experiment: Experiment, runs: List[TaskRun]) -> str:
        definition_id = (experiment.config_snapshot or {}).get("evaluation_definition_version_id")
        if not definition_id:
            return "NOT_REQUIRED"
        statuses = []
        for task_run in runs:
            if task_run.status != TaskRunStatus.COMPLETED:
                continue
            agent_run = self.db.execute(
                select(AgentRun).where(AgentRun.task_run_id == task_run.id)
            ).scalar_one_or_none()
            if agent_run is None:
                statuses.append("PENDING")
                continue
            evaluation = self.db.execute(
                select(EvaluationRun)
                .where(
                    EvaluationRun.subject_agent_run_id == agent_run.id,
                    EvaluationRun.evaluation_definition_version_id == definition_id,
                )
                .order_by(EvaluationRun.created_at.desc())
            ).scalars().first()
            statuses.append(evaluation.status.value.upper() if evaluation else "PENDING")
        if not statuses:
            return "PENDING"
        if any(status == EvaluationRunStatus.FAILED.value.upper() for status in statuses):
            return "FAILED"
        if any(status == EvaluationRunStatus.CANCELLED.value.upper() for status in statuses):
            return "FAILED"
        if any(status == EvaluationRunStatus.RUNNING.value.upper() for status in statuses):
            return "RUNNING"
        if any(status == EvaluationRunStatus.PENDING.value.upper() for status in statuses):
            return "PENDING"
        return "COMPLETED"

    def _state_summary(self, experiment: Experiment) -> dict:
        execution_status, runs = self._execution_state(experiment)
        evaluation_status = self._evaluation_state(experiment, runs)
        if execution_status == "RUNNING":
            overall_status = "RUNNING"
        elif execution_status == "PARTIAL":
            overall_status = "PARTIAL"
        elif execution_status == "FAILED":
            overall_status = "FAILED"
        elif evaluation_status == "NOT_REQUIRED" or evaluation_status == "COMPLETED":
            overall_status = "COMPLETED"
        elif evaluation_status == "FAILED":
            overall_status = "EVALUATION_FAILED"
        else:
            overall_status = "EVALUATING"
        if experiment.status == ExperimentStatus.CANCELLED:
            overall_status = "CANCELLED"
        return {
            "execution_status": execution_status,
            "evaluation_status": evaluation_status,
            "evaluation_required": self._evaluation_required(experiment),
            "overall_status": overall_status,
        }

    def cancel(self, user_id: str, experiment_id: str) -> Experiment:
        experiment = self._owned(user_id, experiment_id)
        if experiment.status in (ExperimentStatus.COMPLETED, ExperimentStatus.FAILED, ExperimentStatus.CANCELLED):
            raise ConflictError("Experiment is already terminal.")
        runs = list(self.db.execute(
            select(TaskRun).join(ExperimentTaskRun, ExperimentTaskRun.task_run_id == TaskRun.id)
            .where(ExperimentTaskRun.experiment_id == experiment.id)
        ).scalars())
        for run in runs:
            if run.status not in (TaskRunStatus.COMPLETED, TaskRunStatus.FAILED, TaskRunStatus.CANCELLED, TaskRunStatus.CANCELLING):
                TaskService(self.db).cancel_task_run(run.id, requested_by=user_id)
        experiment.status = ExperimentStatus.CANCELLED
        self.db.commit()
        self.db.refresh(experiment)
        return experiment

    def evaluate(self, user_id: str, experiment_id: str, *, evaluation_definition_version_id: str, method, evaluator_agent_version_id: Optional[str] = None) -> dict:
        experiment = self._owned(user_id, experiment_id)
        configured_definition = (experiment.config_snapshot or {}).get("evaluation_definition_version_id")
        if configured_definition and configured_definition != evaluation_definition_version_id:
            raise ConflictError("This experiment is configured for a different Evaluation Definition Version.")
        if not configured_definition:
            experiment.config_snapshot = {
                **(experiment.config_snapshot or {}),
                "evaluation_definition_version_id": evaluation_definition_version_id,
            }
            if experiment.status == ExperimentStatus.COMPLETED:
                experiment.status = ExperimentStatus.RUNNING
            self.db.commit()
        experiment = self.refresh(experiment)
        comparisons = list(self.db.execute(select(ComparisonRun).where(ComparisonRun.experiment_id == experiment.id)).scalars())
        outcomes = []
        for comparison in comparisons:
            outcomes.extend(EvaluationExecutionService(self.db).analyze_comparison(
                comparison_id=comparison.id,
                evaluation_definition_version_id=evaluation_definition_version_id,
                method=method,
                evaluator_agent_version_id=evaluator_agent_version_id,
                evaluator_model_policy_override=None,
                budget_id=experiment.budget_id,
                requested_by_user_id=user_id,
            ))
        return {"experiment_id": experiment.id, "results": [o.__dict__ for o in outcomes]}

    def read(self, user_id: str, experiment_id: str) -> dict:
        experiment = self.refresh(self._owned(user_id, experiment_id))
        state = self._state_summary(experiment)
        run_rows = list(self.db.execute(
            select(ExperimentTaskRun, TaskRun, AgentRun)
            .join(TaskRun, TaskRun.id == ExperimentTaskRun.task_run_id)
            .join(AgentRun, AgentRun.task_run_id == TaskRun.id)
            .where(ExperimentTaskRun.experiment_id == experiment.id)
            .order_by(ExperimentTaskRun.task_position, ExperimentTaskRun.repetition, ExperimentTaskRun.label)
        ).all())
        items = []
        total_in = total_out = 0
        total_cost = Decimal(0)
        has_calls = False
        unknown_cost = False
        estimated_cost = False
        for slot, task_run, agent_run in run_rows:
            calls = self.db.execute(
                select(
                    func.coalesce(func.sum(ModelCall.tokens_in), 0),
                    func.coalesce(func.sum(ModelCall.tokens_out), 0),
                    func.sum(ModelCall.cost_amount),
                ).where(ModelCall.agent_run_id == agent_run.id)
            ).one()
            tokens_in, tokens_out = int(calls[0] or 0), int(calls[1] or 0)
            model_calls = list(self.db.execute(select(ModelCall).where(ModelCall.agent_run_id == agent_run.id)).scalars())
            has_calls = has_calls or bool(model_calls)
            if any(call.cost_amount is None for call in model_calls):
                unknown_cost = True
            else:
                total_cost += Decimal(calls[2] or 0)
                estimated_cost = estimated_cost or any(call.cost_is_estimated for call in model_calls)
            total_in += tokens_in
            total_out += tokens_out
            items.append({"task_position": slot.task_position, "repetition": slot.repetition, "label": slot.label, "task_run_id": task_run.id, "agent_run_id": agent_run.id, "status": task_run.status.value, "model_id": agent_run.model_id, "provider_model_snapshot_id": agent_run.provider_model_snapshot_id, "tokens_in": tokens_in, "tokens_out": tokens_out})
        cost_kind = "UNKNOWN" if unknown_cost or not has_calls else ("ESTIMATED" if estimated_cost else "KNOWN")
        experiment_read = LabService(self.db).experiment_read(experiment)
        experiment_read.update(state)
        return {"experiment": experiment_read, "progress": {"total": len(items), "completed": sum(i["status"] == "completed" for i in items), "failed": sum(i["status"] == "failed" for i in items), "cancelled": sum(i["status"] == "cancelled" for i in items), "tokens_in": total_in, "tokens_out": total_out, "cost_kind": cost_kind, "cost": str(total_cost) if cost_kind != "UNKNOWN" else None, **state}, "runs": items}
