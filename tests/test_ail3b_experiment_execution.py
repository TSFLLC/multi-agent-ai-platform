"""Focused AIL.3B orchestration tests; no worker or provider is started."""

import pytest
from sqlalchemy import select

from app.db.enums import ExperimentType, TaskRunStatus
from app.errors import NotFoundError
from app.models.lab import ExperimentTaskRun
from app.models.tasks import TaskRun
from app.services.experiment_execution_service import ExperimentExecutionService
from app.services.lab_service import LabService
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
