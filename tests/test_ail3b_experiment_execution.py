"""Focused AIL.3B orchestration tests; no worker or provider is started."""

import pytest
from sqlalchemy import select

from app.db.enums import ArtifactType, EvaluationMethod, EvaluationRunStatus, ExperimentType, TaskRunStatus
from app.errors import NotFoundError
from app.models.artifacts_eval import Artifact
from app.models.evaluation_runs import EvaluationRun
from app.models.lab import ExperimentTaskRun
from app.models.tasks import AgentRun, TaskRun
from app.services.experiment_execution_service import ExperimentExecutionService
from app.services.lab_service import LabService
from tests.conftest import make_evaluation_definition, make_evaluation_definition_version
from tests.test_ail3a_personal_lab import _active_agent, _model_bundle, _snapshot


def _model_experiment(db, bootstrap):
    service = LabService(db)
    _, versions = service.starter_kits(bootstrap.user)
    agent = _active_agent(db, bootstrap.project.id, 31, "Execution Agent")
    model_a, provider_a, provider_model_a = _model_bundle(db, "lab/exec-a", "Exec Provider A")
    model_b, provider_b, provider_model_b = _model_bundle(db, "lab/exec-b", "Exec Provider B")
    snapshot_a = _snapshot(db, model_a, provider_model_a, provider_a)
    snapshot_b = _snapshot(db, model_b, provider_model_b, provider_b)
    db.commit()
    from app.schemas.lab import ExperimentCreate, ExperimentModelSelection

    experiment = service.create_experiment(
        bootstrap.user,
        ExperimentCreate(
            experiment_type=ExperimentType.MODEL_COMPARISON,
            hypothesis="Execution remains traceable to each selected model.",
            eval_set_version_id=versions[0].id,
            agent_version_ids=[agent.id],
            models=[
                ExperimentModelSelection(model_id=model_a.id, provider_model_snapshot_id=snapshot_a.id),
                ExperimentModelSelection(model_id=model_b.id, provider_model_snapshot_id=snapshot_b.id),
            ],
            repetitions=1,
        ),
    )
    return experiment, versions[0]


def test_model_faceoff_initializes_tagged_runs_and_pins_snapshots(db, bootstrap):
    experiment, _ = _model_experiment(db, bootstrap)

    launched = ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    slots = db.execute(
        select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)
    ).scalars().all()
    task_runs = db.execute(
        select(TaskRun).where(TaskRun.experiment_id == experiment.id)
    ).scalars().all()

    assert launched.status.value == "running"
    assert len(slots) == 12
    assert len(task_runs) == 18  # six bookkeeping runs plus twelve candidates
    assert all(run.status in (TaskRunStatus.RUNNING, TaskRunStatus.QUEUED) for run in task_runs)
    assert all(run.config_snapshot["frozen_task_snapshot"]["task_id"] for run in task_runs)
    assert all(run.experiment_id == experiment.id for run in task_runs)

    second = ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    assert second.id == experiment.id
    assert db.execute(select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)).scalars().all() == slots


def test_variance_creates_repetition_slots_without_comparison_winner(db, bootstrap):
    service = LabService(db)
    _, versions = service.starter_kits(bootstrap.user)
    agent = _active_agent(db, bootstrap.project.id, 32, "Variance Execution Agent")
    model, provider, provider_model = _model_bundle(db, "lab/variance", "Variance Provider")
    snapshot = _snapshot(db, model, provider_model, provider)
    db.commit()
    from app.schemas.lab import ExperimentCreate, ExperimentModelSelection

    experiment = service.create_experiment(
        bootstrap.user,
        ExperimentCreate(
            experiment_type=ExperimentType.VARIANCE,
            eval_set_version_id=versions[0].id,
            agent_version_ids=[agent.id],
            models=[ExperimentModelSelection(model_id=model.id, provider_model_snapshot_id=snapshot.id)],
            repetitions=2,
        ),
    )
    ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    slots = db.execute(
        select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)
    ).scalars().all()
    assert len(slots) == 12
    assert sorted({slot.repetition for slot in slots}) == [1, 2]
    assert db.execute(select(TaskRun).where(TaskRun.experiment_id == experiment.id)).scalars().all()


def test_experiment_execution_is_user_scoped(db, bootstrap):
    experiment, _ = _model_experiment(db, bootstrap)
    with pytest.raises(NotFoundError):
        ExperimentExecutionService(db).read("another-user", experiment.id)


def _complete_execution(db, experiment):
    slots = db.execute(
        select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)
    ).scalars().all()
    for slot in slots:
        db.get(TaskRun, slot.task_run_id).status = TaskRunStatus.COMPLETED
    db.commit()


def _require_evaluation(db, experiment, definition_id):
    experiment.config_snapshot = {
        **(experiment.config_snapshot or {}),
        "evaluation_definition_version_id": definition_id,
    }
    db.commit()


def _add_evaluations(db, experiment, definition_id, status):
    slots = db.execute(
        select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)
    ).scalars().all()
    for slot in slots:
        task_run = db.get(TaskRun, slot.task_run_id)
        agent_run = db.execute(select(AgentRun).where(AgentRun.task_run_id == task_run.id)).scalar_one()
        artifact = Artifact(
            agent_run_id=agent_run.id,
            type=ArtifactType.REPORT,
            storage_ref=f"test://{agent_run.id}",
            content_hash="a" * 64,
        )
        db.add(artifact)
        db.flush()
        db.add(EvaluationRun(
            subject_agent_run_id=agent_run.id,
            subject_artifact_id=artifact.id,
            subject_artifact_content_hash=artifact.content_hash,
            evaluation_definition_version_id=definition_id,
            method=EvaluationMethod.DETERMINISTIC,
            status=status,
        ))
    db.commit()


def test_required_evaluation_keeps_execution_complete_separate_from_overall_completion(db, bootstrap):
    experiment, _ = _model_experiment(db, bootstrap)
    definition = make_evaluation_definition_version(
        db, definition=make_evaluation_definition(db, project=bootstrap.project), version=1
    )
    _require_evaluation(db, experiment, definition.id)
    ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    _complete_execution(db, experiment)

    result = ExperimentExecutionService(db).read(bootstrap.user.id, experiment.id)

    assert result["experiment"]["execution_status"] == "COMPLETED"
    assert result["experiment"]["evaluation_status"] == "PENDING"
    assert result["experiment"]["overall_status"] == "EVALUATING"
    assert result["experiment"]["status"].value == "running"


def test_required_evaluation_completion_allows_overall_completion(db, bootstrap):
    experiment, _ = _model_experiment(db, bootstrap)
    definition = make_evaluation_definition_version(
        db, definition=make_evaluation_definition(db, project=bootstrap.project), version=2
    )
    _require_evaluation(db, experiment, definition.id)
    ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    _complete_execution(db, experiment)
    _add_evaluations(db, experiment, definition.id, EvaluationRunStatus.COMPLETED)

    result = ExperimentExecutionService(db).read(bootstrap.user.id, experiment.id)

    assert result["experiment"]["evaluation_status"] == "COMPLETED"
    assert result["experiment"]["overall_status"] == "COMPLETED"
    assert result["experiment"]["status"].value == "completed"


def test_running_required_evaluation_is_distinct_from_pending(db, bootstrap):
    experiment, _ = _model_experiment(db, bootstrap)
    definition = make_evaluation_definition_version(
        db, definition=make_evaluation_definition(db, project=bootstrap.project), version=4
    )
    _require_evaluation(db, experiment, definition.id)
    ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    _complete_execution(db, experiment)
    _add_evaluations(db, experiment, definition.id, EvaluationRunStatus.RUNNING)

    result = ExperimentExecutionService(db).read(bootstrap.user.id, experiment.id)

    assert result["experiment"]["evaluation_status"] == "RUNNING"
    assert result["experiment"]["overall_status"] == "EVALUATING"


def test_failed_required_evaluation_is_visible_and_not_successful_completion(db, bootstrap):
    experiment, _ = _model_experiment(db, bootstrap)
    definition = make_evaluation_definition_version(
        db, definition=make_evaluation_definition(db, project=bootstrap.project), version=3
    )
    _require_evaluation(db, experiment, definition.id)
    ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    _complete_execution(db, experiment)
    _add_evaluations(db, experiment, definition.id, EvaluationRunStatus.FAILED)

    result = ExperimentExecutionService(db).read(bootstrap.user.id, experiment.id)

    assert result["experiment"]["evaluation_status"] == "FAILED"
    assert result["experiment"]["overall_status"] == "EVALUATION_FAILED"
    assert result["experiment"]["status"].value == "failed"


def test_no_required_evaluation_completes_normally(db, bootstrap):
    experiment, _ = _model_experiment(db, bootstrap)
    ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    _complete_execution(db, experiment)

    result = ExperimentExecutionService(db).read(bootstrap.user.id, experiment.id)

    assert result["experiment"]["evaluation_status"] == "NOT_REQUIRED"
    assert result["experiment"]["overall_status"] == "COMPLETED"


def test_partial_execution_is_reported_without_losing_successful_evidence(db, bootstrap):
    experiment, _ = _model_experiment(db, bootstrap)
    ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    slots = db.execute(
        select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)
    ).scalars().all()
    for index, slot in enumerate(slots):
        db.get(TaskRun, slot.task_run_id).status = TaskRunStatus.COMPLETED if index == 0 else TaskRunStatus.FAILED
    db.commit()

    result = ExperimentExecutionService(db).read(bootstrap.user.id, experiment.id)

    assert result["experiment"]["execution_status"] == "PARTIAL"
    assert result["experiment"]["overall_status"] == "PARTIAL"
    assert result["progress"]["completed"] == 1
    assert result["progress"]["failed"] == len(slots) - 1


def test_cancelled_execution_is_not_presented_as_successful(db, bootstrap):
    experiment, _ = _model_experiment(db, bootstrap)
    ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    result = ExperimentExecutionService(db).cancel(bootstrap.user.id, experiment.id)

    assert result.status.value == "cancelled"
    read = ExperimentExecutionService(db).read(bootstrap.user.id, experiment.id)
    assert read["experiment"]["overall_status"] == "CANCELLED"
