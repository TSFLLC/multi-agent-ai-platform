"""Agent-Evaluator Execution — MA6 Slice 3B.

One manually requested EvaluationRun executes one real evaluator Agent
Run (AgentRunRole.EVALUATOR) against one exact subject AgentRun/Artifact/
hash, through the *unmodified* MA3 execution pipeline
(AgentExecutionService, JobType.AGENT_RUN, ProviderAdapter, model
resolution/override, ProviderModelSnapshot, ModelCall, BudgetGovernor,
retries, cancellation checkpoints, Artifact creation, queue leases/
heartbeat/fencing) -- no second inference system, no live network (every
test drives AgentExecutionService with a FakeAdapter, exactly like
test_comparison_service.py / test_execution_service.py).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, List

from sqlalchemy import select

from app.db.enums import (
    AgentRunRole,
    AgentRunStatus,
    EvaluationFinding,
    EvaluationMethod,
    EvaluationRunStatus,
    ExecutionMode,
    JobQueueStatus,
    JobType,
    VersionStatus,
)
from app.models.execution import JobQueue, ModelCall
from app.models.observability import ExecutionEvent
from app.models.reviews import AgentReview
from app.models.tasks import AgentRun, Task, TaskRun
from app.providers.base import InvokeRequest, InvokeResponse
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.evaluation_definition_service import EvaluationDefinitionService
from app.services.evaluation_execution_service import EvaluationExecutionService
from app.services.execution_service import AgentExecutionService
from tests.conftest import (
    make_agent,
    make_agent_run,
    make_agent_version,
    make_artifact_with_content,
    make_budget,
    make_evaluation_definition,
    make_model,
    make_project,
    make_provider,
    make_provider_model,
    make_task,
    make_task_run,
)

_repo = JobQueueRepository()


# -- fakes / helpers -------------------------------------------------------------


def _manual(pm) -> dict:
    return {"mode": "manual", "manual_provider_model_id": pm.id}


@dataclass
class FakeAdapter:
    responses: List[Any] = field(default_factory=list)
    calls: List[InvokeRequest] = field(default_factory=list)

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        self.calls.append(request)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _factory_for(adapter: FakeAdapter):
    return lambda db, provider: adapter


def _published_agent_version(db, project, name="Agent", free=True):
    agent = make_agent(db, project=project, name=name)
    version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    model = make_model(db, canonical_model_id=f"model/{name}")
    provider = make_provider(db)
    cost_kwargs = {"cost_input_per_mtok": Decimal(0), "cost_output_per_mtok": Decimal(0)} if free else {}
    pm = make_provider_model(db, model=model, provider=provider, **cost_kwargs)
    version.model_policy = _manual(pm)
    return version, pm


def _subject(db, tmp_path, *, project=None, content="candidate output") -> SimpleNamespace:
    project = project or make_project(db)
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task)
    agent_version, _pm = _published_agent_version(db, project, name="Candidate")
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    artifact = make_artifact_with_content(db, tmp_path, agent_run=agent_run, content=content)
    db.commit()
    return SimpleNamespace(
        project=project, task=task, task_run=task_run, agent_run=agent_run, artifact=artifact
    )


def _published_version(db, project, criteria):
    definition = make_evaluation_definition(db, project=project)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id, description=None, criteria=criteria
    )
    return EvaluationDefinitionService(db).publish_version(version.id)


def _criteria(*keys):
    return [{"key": k, "label": k.title()} for k in keys]


def _valid_evaluator_response(findings: dict) -> str:
    import json

    return json.dumps(
        {
            "criteria": [
                {"key": key, "finding": finding, "rationale": f"rationale for {key}"}
                for key, finding in findings.items()
            ]
        }
    )


def _run_evaluator(db, evaluator_agent_run_id, *, responses) -> FakeAdapter:
    adapter = FakeAdapter(responses=list(responses))
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
        evaluator_agent_run_id, worker_id="test-worker"
    )
    return adapter


def _events_for(db, task_run_id) -> List[str]:
    stmt = (
        select(ExecutionEvent.event_type)
        .where(ExecutionEvent.task_run_id == task_run_id)
        .order_by(ExecutionEvent.sequence_number)
    )
    return [row[0] for row in db.execute(stmt).all()]


# -- end-to-end success -----------------------------------------------------------


def test_agent_evaluator_end_to_end_success(db, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("correctness", "coverage"))
    svc = EvaluationExecutionService(db)

    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    assert run.method == EvaluationMethod.AGENT_EVALUATOR
    assert run.status == EvaluationRunStatus.RUNNING
    assert run.evaluator_agent_version_id == evaluator_version.id
    evaluator_agent_run_id = run.evaluator_agent_run_id
    assert evaluator_agent_run_id is not None

    response_text = _valid_evaluator_response({"correctness": "MET", "coverage": "PARTIAL"})
    adapter = _run_evaluator(
        db,
        evaluator_agent_run_id,
        responses=[InvokeResponse(text=response_text, tokens_in=40, tokens_out=25)],
    )
    assert len(adapter.calls) == 1  # one inference for the complete rubric, not one per criterion

    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED
    results = {r.criterion_key: r for r in run.criterion_results}
    assert len(results) == 2
    assert results["correctness"].finding == EvaluationFinding.MET
    assert results["coverage"].finding == EvaluationFinding.PARTIAL


def test_agent_evaluator_correct_provenance(db, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    evaluator_agent_run = db.get(AgentRun, run.evaluator_agent_run_id)

    # evaluator bookkeeping Task/Task Run/Agent Run are dedicated, never
    # the subject's own.
    evaluator_task_run = db.get(TaskRun, evaluator_agent_run.task_run_id)
    evaluator_task = db.get(Task, evaluator_task_run.task_id)
    assert evaluator_task_run.id != s.task_run.id
    assert evaluator_task.id != s.task.id
    assert evaluator_task.execution_mode == ExecutionMode.SINGLE_AGENT
    assert evaluator_agent_run.role == AgentRunRole.EVALUATOR
    assert evaluator_agent_run.agent_version_id == evaluator_version.id
    assert run.evaluator_task_run_id == evaluator_task_run.id
    assert run.evaluator_agent_run_id == evaluator_agent_run.id

    _run_evaluator(
        db,
        evaluator_agent_run.id,
        responses=[InvokeResponse(text=_valid_evaluator_response({"a": "MET"}), tokens_in=10, tokens_out=5)],
    )
    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED


def test_agent_evaluator_manual_model_override(db, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _default_pm = _published_agent_version(db, s.project, name="Evaluator")
    override_model = make_model(db, canonical_model_id="model/override")
    override_pm = make_provider_model(
        db,
        model=override_model,
        provider=make_provider(db),
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)

    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
        evaluator_model_policy_override=_manual(override_pm),
    )
    evaluator_agent_run = db.get(AgentRun, run.evaluator_agent_run_id)
    assert evaluator_agent_run.model_policy_override_json == _manual(override_pm)

    adapter = _run_evaluator(
        db,
        evaluator_agent_run.id,
        responses=[InvokeResponse(text=_valid_evaluator_response({"a": "MET"}), tokens_in=10, tokens_out=5)],
    )
    assert adapter.calls[0].provider_model_id == override_pm.provider_model_id
    db.refresh(evaluator_agent_run)
    assert evaluator_agent_run.provider_model_snapshot_id is not None
    from app.models.providers import ProviderModelSnapshot

    snapshot = db.get(ProviderModelSnapshot, evaluator_agent_run.provider_model_snapshot_id)
    assert snapshot.provider_model_id == override_pm.id


def test_agent_evaluator_produces_normal_model_call_with_usage(db, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    evaluator_agent_run_id = run.evaluator_agent_run_id

    _run_evaluator(
        db,
        evaluator_agent_run_id,
        responses=[
            InvokeResponse(
                text=_valid_evaluator_response({"a": "MET"}), tokens_in=123, tokens_out=45, latency_ms=678
            )
        ],
    )

    calls = list(
        db.execute(select(ModelCall).where(ModelCall.agent_run_id == evaluator_agent_run_id)).scalars()
    )
    assert len(calls) == 1
    call = calls[0]
    assert call.tokens_in == 123
    assert call.tokens_out == 45
    assert call.latency_ms == 678
    assert call.cost_amount == Decimal(0)  # FREE pricing in this test's fixture
    assert call.provider_model_snapshot_id is not None

    # EvaluationRun itself stores none of this -- reachable only via
    # evaluator_agent_run_id (avoid unnecessary denormalization).
    db.refresh(run)
    assert not hasattr(run, "tokens_in")


def test_agent_evaluator_criterion_results_include_structured_evidence(db, tmp_path):
    import json

    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("correctness"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    text = json.dumps(
        {
            "criteria": [
                {
                    "key": "correctness",
                    "finding": "MET",
                    "rationale": "matches spec",
                    "evidence": [{"quote": "return n % 2 == 0", "criterion_key": "correctness"}],
                }
            ]
        }
    )
    _run_evaluator(
        db, run.evaluator_agent_run_id, responses=[InvokeResponse(text=text, tokens_in=1, tokens_out=1)]
    )

    db.refresh(run)
    result = run.criterion_results[0]
    assert result.finding == EvaluationFinding.MET
    assert result.evidence_refs == [{"quote": "return n % 2 == 0", "criterion_key": "correctness"}]


# -- malformed output -------------------------------------------------------------


def test_agent_evaluator_malformed_output_fails_with_zero_partial_results(db, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a", "b"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )

    _run_evaluator(
        db,
        run.evaluator_agent_run_id,
        responses=[InvokeResponse(text="not valid json at all {{{", tokens_in=1, tokens_out=1)],
    )

    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED
    assert run.failure_reason is not None
    assert list(run.criterion_results) == []
    # the underlying evaluator Agent Run itself succeeded -- inference happened
    evaluator_agent_run = db.get(AgentRun, run.evaluator_agent_run_id)
    assert evaluator_agent_run.status == AgentRunStatus.COMPLETED


def test_agent_evaluator_missing_criterion_fails(db, tmp_path):
    import json

    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a", "b"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    text = json.dumps({"criteria": [{"key": "a", "finding": "MET", "rationale": "ok"}]})  # missing "b"
    _run_evaluator(
        db, run.evaluator_agent_run_id, responses=[InvokeResponse(text=text, tokens_in=1, tokens_out=1)]
    )

    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED
    assert list(run.criterion_results) == []


def test_agent_evaluator_duplicate_criterion_fails(db, tmp_path):
    import json

    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    text = json.dumps(
        {
            "criteria": [
                {"key": "a", "finding": "MET", "rationale": "ok"},
                {"key": "a", "finding": "NOT_MET", "rationale": "dup"},
            ]
        }
    )
    _run_evaluator(
        db, run.evaluator_agent_run_id, responses=[InvokeResponse(text=text, tokens_in=1, tokens_out=1)]
    )

    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED
    assert list(run.criterion_results) == []


def test_agent_evaluator_unknown_criterion_fails(db, tmp_path):
    import json

    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    text = json.dumps(
        {
            "criteria": [
                {"key": "a", "finding": "MET", "rationale": "ok"},
                {"key": "invented_by_model", "finding": "MET", "rationale": "no"},
            ]
        }
    )
    _run_evaluator(
        db, run.evaluator_agent_run_id, responses=[InvokeResponse(text=text, tokens_in=1, tokens_out=1)]
    )

    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED
    assert list(run.criterion_results) == []


# -- stale subject artifact / hash -------------------------------------------------


def test_agent_evaluator_stale_subject_hash_fails(db, tmp_path):
    s = _subject(db, tmp_path, content="original")
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )

    # Simulate the subject artifact's content changing underneath the
    # bound hash between creation and evaluator execution.
    s.artifact.content_hash = "0" * 64
    db.commit()

    _run_evaluator(
        db,
        run.evaluator_agent_run_id,
        responses=[InvokeResponse(text=_valid_evaluator_response({"a": "MET"}), tokens_in=1, tokens_out=1)],
    )

    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED
    assert list(run.criterion_results) == []
    # caught pre-invocation (cost avoidance) -- no provider call happened,
    # so the underlying evaluator Agent Run itself is FAILED, not COMPLETED.
    evaluator_agent_run = db.get(AgentRun, run.evaluator_agent_run_id)
    assert evaluator_agent_run.status == AgentRunStatus.FAILED


# -- provider/model/execution failure -----------------------------------------------


def test_agent_evaluator_provider_failure_fails_evaluation_run(db, tmp_path):
    from app.providers.base import ProviderTimeoutError

    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )

    _run_evaluator(
        db,
        run.evaluator_agent_run_id,
        responses=[ProviderTimeoutError("timed out"), ProviderTimeoutError("timed out again")],
    )

    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED
    assert list(run.criterion_results) == []


def test_agent_evaluator_budget_rejection_fails_evaluation_run(db, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator", free=False)
    version = _published_version(db, s.project, _criteria("a"))
    budget = make_budget(db, project=s.project, limit_amount="0.0000001")
    db.commit()
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
        budget_id=budget.id,
    )

    _run_evaluator(db, run.evaluator_agent_run_id, responses=[])  # never reaches the provider

    db.refresh(run)
    assert run.status == EvaluationRunStatus.FAILED
    assert list(run.criterion_results) == []


# -- cancellation -----------------------------------------------------------------


def test_agent_evaluator_cancellation_sets_cancelled(db, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    evaluator_task_run = db.get(TaskRun, run.evaluator_task_run_id)
    evaluator_task_run.cancellation_requested_at = datetime.now(timezone.utc)
    db.commit()

    _run_evaluator(db, run.evaluator_agent_run_id, responses=[])  # cancellation observed before invocation

    db.refresh(run)
    assert run.status == EvaluationRunStatus.CANCELLED


# -- worker lease/reclaim/fencing --------------------------------------------------


def test_agent_evaluator_worker_lease_reclaim_and_fencing(db, session_factory, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    job = db.execute(select(JobQueue).where(JobQueue.payload_ref == run.evaluator_agent_run_id)).scalar_one()
    assert job.job_type == JobType.AGENT_RUN  # never JobType.EVALUATION

    claim_db = session_factory()
    try:
        claimed = _repo.claim_one(claim_db, worker_id="worker-a", lease_seconds=30)
        assert claimed is not None
        assert claimed.id == job.id
        zombie_fencing_token = claimed.fencing_token
        claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        claim_db.commit()
    finally:
        claim_db.close()

    reclaim_db = session_factory()
    try:
        reclaimed = _repo.claim_one(reclaim_db, worker_id="worker-b", lease_seconds=30)
    finally:
        reclaim_db.close()

    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.fencing_token == zombie_fencing_token + 1

    worker_b_completed = _repo.complete(
        db,
        job_id=job.id,
        worker_id="worker-b",
        fencing_token=reclaimed.fencing_token,
        status=JobQueueStatus.DONE,
    )
    assert worker_b_completed is True

    zombie_completed = _repo.complete(
        db,
        job_id=job.id,
        worker_id="worker-a",
        fencing_token=zombie_fencing_token,
        status=JobQueueStatus.FAILED,
    )
    assert zombie_completed is False


# -- evaluator never enters MA4 review orchestration -------------------------------


def test_agent_evaluator_never_enters_review_orchestration(db, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    _run_evaluator(
        db,
        run.evaluator_agent_run_id,
        responses=[InvokeResponse(text=_valid_evaluator_response({"a": "MET"}), tokens_in=1, tokens_out=1)],
    )

    assert list(db.execute(select(AgentReview)).scalars().all()) == []
    db.refresh(run)
    assert run.status == EvaluationRunStatus.COMPLETED


# -- subject Agent Run / Task Run untouched ----------------------------------------


def test_agent_evaluator_never_touches_subject_agent_run_or_task_run(db, tmp_path):
    s = _subject(db, tmp_path)
    subject_status_before = s.agent_run.status
    subject_task_run_status_before = s.task_run.status
    subject_hash_before = s.artifact.content_hash

    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    _run_evaluator(
        db,
        run.evaluator_agent_run_id,
        responses=[InvokeResponse(text=_valid_evaluator_response({"a": "MET"}), tokens_in=1, tokens_out=1)],
    )

    db.refresh(s.agent_run)
    db.refresh(s.task_run)
    db.refresh(s.artifact)
    assert s.agent_run.status == subject_status_before
    assert s.task_run.status == subject_task_run_status_before
    assert s.artifact.content_hash == subject_hash_before


# -- Flight Recorder lifecycle ------------------------------------------------------


def test_agent_evaluator_flight_recorder_lifecycle(db, tmp_path):
    s = _subject(db, tmp_path)
    evaluator_version, _pm = _published_agent_version(db, s.project, name="Evaluator")
    version = _published_version(db, s.project, _criteria("a"))
    svc = EvaluationExecutionService(db)
    run = svc.create_agent_evaluator_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
        evaluator_agent_version_id=evaluator_version.id,
    )
    evaluator_agent_run = db.get(AgentRun, run.evaluator_agent_run_id)
    evaluator_task_run_id = evaluator_agent_run.task_run_id

    _run_evaluator(
        db,
        run.evaluator_agent_run_id,
        responses=[InvokeResponse(text=_valid_evaluator_response({"a": "MET"}), tokens_in=1, tokens_out=1)],
    )

    subject_events = _events_for(db, s.task_run.id)
    assert "evaluation_run.requested" in subject_events

    evaluator_events = _events_for(db, evaluator_task_run_id)
    assert "evaluation_run.evaluator_queued" in evaluator_events
    assert "evaluation_run.completed" in evaluator_events
    # generic AgentRun/ModelCall events are not duplicated -- they already
    # exist under the evaluator's own Task Run stream via the unmodified
    # execution engine.
    assert "agent_run.completed" in evaluator_events
    assert "model_call.completed" in evaluator_events


# -- deterministic Slice 2 path unaffected -----------------------------------------


def test_deterministic_evaluation_still_works_unchanged(db, tmp_path):
    """Sanity check that Slice 3B's execution_service.py hooks are purely
    additive -- the deterministic path (JobType.EVALUATION, never touched
    by this slice) still works exactly as before."""
    s = _subject(db, tmp_path, content="real output")
    version = _published_version(db, s.project, [{"key": "non_empty_output", "label": "Non-empty output"}])
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run.id, worker_id="test-worker")

    db.refresh(run)
    assert run.method == EvaluationMethod.DETERMINISTIC
    assert run.status == EvaluationRunStatus.COMPLETED
    assert run.criterion_results[0].finding == EvaluationFinding.MET
