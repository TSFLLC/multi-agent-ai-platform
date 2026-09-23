from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.enums import (
    ExecutionMode,
    ExperimentType,
    ModelCallStatus,
    ProviderType,
    SnapshotSource,
    TaskRunStatus,
    TaskStatus,
    VersionStatus,
)
from app.errors import ConflictError, NotFoundError
from app.models.agents import Agent, AgentVersion
from app.models.execution import ModelCall
from app.models.lab import EvalSetVersionTask, Experiment, ExperimentModel
from app.models.providers import Model, Provider, ProviderModel, ProviderModelSnapshot
from app.models.radar import Development
from app.models.tasks import AgentRun, Task, TaskRun
from app.routing_evidence import DEFAULT_V1_CONFIG, EvidenceConfig, RoutingContext, load_evidence
from app.services.lab_service import LabService


def _snapshot(db, model, provider_model, provider, *, priced=True):
    snapshot = ProviderModelSnapshot(
        provider_model_id=provider_model.id,
        model_id=model.id,
        provider_id=provider.id,
        pricing_input_per_mtok=Decimal("1.00") if priced else None,
        pricing_output_per_mtok=Decimal("2.00") if priced else None,
        currency="USD",
        context_window=128000,
        source=SnapshotSource.CATALOG_REFRESH,
        snapshotted_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def test_starter_kits_are_exactly_two_with_six_frozen_task_snapshots(db, bootstrap):
    kits, versions = LabService(db).starter_kits(bootstrap.user)

    assert sorted(kit.name for kit in kits) == ["Code Reviewer", "Software Engineer"]
    assert len(versions) == 2
    for version in versions:
        rows = db.execute(
            select(EvalSetVersionTask).where(EvalSetVersionTask.eval_set_version_id == version.id)
        ).scalars().all()
        assert len(rows) == 6
        assert [row.position for row in rows] == list(range(6))
        assert all(row.task_snapshot["task_id"] == row.task_id for row in rows)


def test_starter_creation_is_idempotent_and_owned(db, bootstrap):
    service = LabService(db)
    first, _ = service.starter_kits(bootstrap.user)
    second, _ = service.starter_kits(bootstrap.user)
    assert [item.id for item in first] == [item.id for item in second]
    assert LabService(db).list_kits(bootstrap.user.id)


def _active_agent(db, project_id: str, version: int, name: str) -> AgentVersion:
    agent = Agent(project_id=project_id, name=name, role="engineer")
    db.add(agent)
    db.flush()
    version_row = AgentVersion(
        agent_id=agent.id, version=version, name=name, role="engineer", status=VersionStatus.ACTIVE
    )
    db.add(version_row)
    db.flush()
    return version_row


def _model_bundle(db, canonical_id: str, provider_name: str):
    provider = Provider(type=ProviderType.LOCAL, name=provider_name)
    model = Model(canonical_model_id=canonical_id)
    db.add_all([provider, model])
    db.flush()
    provider_model = ProviderModel(
        model_id=model.id,
        provider_id=provider.id,
        provider_model_id=canonical_id,
        cost_input_per_mtok=Decimal("1.00"),
        cost_output_per_mtok=Decimal("2.00"),
    )
    db.add(provider_model)
    db.flush()
    return model, provider, provider_model


def test_experiment_draft_validates_snapshot_model_and_never_executes(db, bootstrap):
    service = LabService(db)
    _, versions = service.starter_kits(bootstrap.user)
    agent_a = _active_agent(db, bootstrap.project.id, 1, "Lab Agent A")
    model_a, provider, pm_a = _model_bundle(db, "lab/model-a", "Lab Provider")
    model_b, _, pm_b = _model_bundle(db, "lab/model-b", "Lab Provider 2")
    snap_a = _snapshot(db, model_a, pm_a, provider)
    snap_b = _snapshot(db, model_b, pm_b, provider)
    db.commit()

    from app.schemas.lab import ExperimentCreate, ExperimentModelSelection

    experiment = service.create_experiment(
        bootstrap.user,
        ExperimentCreate(
            experiment_type=ExperimentType.MODEL_COMPARISON,
            hypothesis="Compare two explicit models",
            eval_set_version_id=versions[0].id,
            agent_version_ids=[agent_a.id],
            models=[
                ExperimentModelSelection(model_id=model_a.id, provider_model_snapshot_id=snap_a.id),
                ExperimentModelSelection(model_id=model_b.id, provider_model_snapshot_id=snap_b.id),
            ],
            repetitions=1,
            config={"estimated_tokens_in": 1000, "estimated_tokens_out": 500},
        ),
    )

    assert experiment.status.value == "draft"
    assert experiment.cost_estimate_kind.value == "estimated"
    assert experiment.estimated_cost is not None
    assert db.execute(select(ExperimentModel).where(ExperimentModel.experiment_id == experiment.id)).scalars().all()
    assert not db.execute(select(Experiment)).scalar_one_or_none() is None


def test_snapshot_mismatch_and_unknown_snapshot_fail_closed(db, bootstrap):
    service = LabService(db)
    _, versions = service.starter_kits(bootstrap.user)
    agent = _active_agent(db, bootstrap.project.id, 1, "Mismatch Agent")
    model_a, provider, pm = _model_bundle(db, "lab/mismatch-a", "Mismatch Provider")
    model_b, _, _ = _model_bundle(db, "lab/mismatch-b", "Mismatch Provider 2")
    snapshot = _snapshot(db, model_a, pm, provider)
    db.commit()
    from app.schemas.lab import ExperimentCreate, ExperimentModelSelection

    body = ExperimentCreate(
        experiment_type=ExperimentType.MODEL_COMPARISON,
        eval_set_version_id=versions[0].id,
        agent_version_ids=[agent.id],
        models=[
            ExperimentModelSelection(model_id=model_a.id, provider_model_snapshot_id=snapshot.id),
            ExperimentModelSelection(model_id=model_b.id, provider_model_snapshot_id=snapshot.id),
        ],
    )
    with pytest.raises(ConflictError):
        service.create_experiment(bootstrap.user, body)


def test_user_isolation_for_experiment_reads(db, bootstrap):
    service = LabService(db)
    _, versions = service.starter_kits(bootstrap.user)
    agent = _active_agent(db, bootstrap.project.id, 1, "Variance Agent")
    from app.schemas.lab import ExperimentCreate

    experiment = service.create_experiment(
        bootstrap.user,
        ExperimentCreate(
            experiment_type=ExperimentType.VARIANCE,
            eval_set_version_id=versions[0].id,
            agent_version_ids=[agent.id],
            repetitions=2,
        ),
    )
    assert service.get_experiment(bootstrap.user.id, experiment.id).id == experiment.id
    with pytest.raises(NotFoundError):
        service.get_experiment("not-the-owner", experiment.id)


def test_task_run_experiment_seam_is_nullable(db, bootstrap):
    task = Task(
        project_id=bootstrap.project.id,
        title="seam",
        execution_mode=ExecutionMode.SINGLE_AGENT,
        status=TaskStatus.READY,
    )
    db.add(task)
    db.flush()
    run = TaskRun(task_id=task.id, status=TaskRunStatus.CREATED)
    db.add(run)
    db.flush()
    assert run.experiment_id is None


def test_api_exposes_starter_kits_and_explicit_radar_draft_bridge(client, auth_headers, db, bootstrap):
    starter = client.post("/lab/test-kits/starter", headers=auth_headers)
    assert starter.status_code == 200
    payload = starter.json()
    assert sorted(item["name"] for item in payload["test_kits"]) == ["Code Reviewer", "Software Engineer"]

    agent = _active_agent(db, bootstrap.project.id, 9, "Bridge Agent")
    development = Development(
        title="Controlled Lab bridge development",
        development_type="capability_change",
        candidate_key="controlled-lab-bridge-development",
    )
    db.add(development)
    db.commit()
    body = {
        "experiment_type": "variance",
        "eval_set_version_id": payload["versions"][0]["id"],
        "agent_version_ids": [agent.id],
        "repetitions": 2,
        "config": {"execution_enabled": True},
    }
    response = client.post(
        f"/radar/developments/{development.id}/experiments",
        json=body,
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["development_id"] == development.id
    assert response.json()["config_snapshot"]["execution_enabled"] is False


def test_ma8_evidence_excludes_experiment_tagged_task_runs(db, bootstrap):
    from app.models.lab import Experiment

    agent_version = _active_agent(db, bootstrap.project.id, 11, "Evidence Agent")
    model, provider, provider_model = _model_bundle(db, "lab/evidence-model", "Evidence Provider")
    task = Task(
        project_id=bootstrap.project.id,
        title="evidence task",
        execution_mode=ExecutionMode.SINGLE_AGENT,
        status=TaskStatus.READY,
    )
    db.add(task)
    db.flush()
    experiment = Experiment(
        user_id=bootstrap.user.id,
        experiment_type=ExperimentType.VARIANCE,
        config_snapshot={"execution_enabled": False},
        repetitions=2,
    )
    db.add(experiment)
    db.flush()
    ordinary_run = TaskRun(task_id=task.id, status=TaskRunStatus.COMPLETED)
    experiment_run = TaskRun(task_id=task.id, experiment_id=experiment.id, status=TaskRunStatus.COMPLETED)
    db.add_all([ordinary_run, experiment_run])
    db.flush()
    ordinary_agent_run = AgentRun(
        task_run_id=ordinary_run.id, agent_version_id=agent_version.id, model_id=model.id, provider_id=provider.id
    )
    experiment_agent_run = AgentRun(
        task_run_id=experiment_run.id, agent_version_id=agent_version.id, model_id=model.id, provider_id=provider.id
    )
    db.add_all([ordinary_agent_run, experiment_agent_run])
    db.flush()
    db.add_all(
        [
            ModelCall(
                agent_run_id=ordinary_agent_run.id,
                model_id=model.id,
                provider_id=provider.id,
                provider_model_id=provider_model.id,
                status=ModelCallStatus.SUCCESS,
            ),
            ModelCall(
                agent_run_id=experiment_agent_run.id,
                model_id=model.id,
                provider_id=provider.id,
                provider_model_id=provider_model.id,
                status=ModelCallStatus.SUCCESS,
            ),
        ]
    )
    db.commit()
    profiles = load_evidence(
        db,
        context=RoutingContext(bootstrap.project.id, agent_version.role),
        provider_model_ids=[provider_model.id],
        config=EvidenceConfig.from_json(DEFAULT_V1_CONFIG),
    )
    assert profiles[provider_model.id].completed == 1
