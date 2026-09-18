"""Evaluation Run detail read model — MA6 Slice 3D.

``get_evaluation_run_detail``/``GET /evaluation-runs/{id}`` assembles a
bounded provenance view over the existing normalized tables (EvaluationRun,
EvaluationCriterionResult+EvaluationCriterion, AgentRun/AgentVersion/Agent,
Model/Provider, ModelCall) -- nothing here is stored, no provider/model
call happens, no EvaluationRun/Task/TaskRun/AgentRun/job_queue row is ever
created or mutated by a read. No score/percentage/rank/winner field exists
anywhere in the response (MA6 non-negotiable invariant).
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, List

from sqlalchemy import select

from app.db.enums import (
    EvaluationFinding,
    EvaluationMethod,
    EvaluationRunStatus,
    VersionStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import JobQueue, ModelCall
from app.models.tasks import AgentRun, TaskRun
from app.providers.base import InvokeRequest, InvokeResponse
from app.services.evaluation_definition_service import EvaluationDefinitionService
from app.services.evaluation_execution_service import EvaluationExecutionService, get_evaluation_run_detail
from app.services.execution_service import AgentExecutionService
from tests.conftest import (
    make_agent,
    make_agent_run,
    make_agent_version,
    make_model,
    make_project,
    make_provider,
    make_provider_model,
    make_task,
    make_task_run,
)

# -- fakes / setup helpers -------------------------------------------------------


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


def _manual(pm) -> dict:
    return {"mode": "manual", "manual_provider_model_id": pm.id}


def _published_agent_version(db, project, name="Agent", free=True):
    agent = make_agent(db, project=project, name=name)
    version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    model = make_model(db, canonical_model_id=f"model/{name}")
    provider = make_provider(db, name=f"Provider-{name}")
    cost_kwargs = {"cost_input_per_mtok": Decimal(0), "cost_output_per_mtok": Decimal(0)} if free else {}
    pm = make_provider_model(db, model=model, provider=provider, **cost_kwargs)
    version.model_policy = _manual(pm)
    db.commit()
    return agent, version, model, provider, pm


def _executed_subject(db, tmp_path, project=None, *, content="candidate output") -> SimpleNamespace:
    """A real, executed SINGLE_AGENT run -- not just an artifact injected
    directly (tests/conftest.make_artifact_with_content) -- so the subject
    Agent Run's own model_id/provider_id/provider_model_snapshot_id are
    genuinely resolved (Section: "never infer a model when a provenance
    relation exists"). Mirrors tests/test_comparison_api.py's
    _run_candidate/_wire_free_model pattern at the service layer."""
    project = project or make_project(db)
    agent, agent_version, model, provider, pm = _published_agent_version(db, project, name="Candidate")
    task = make_task(db, project=project)
    task_run = make_task_run(db, task=task)
    agent_run = make_agent_run(db, task_run=task_run, agent_version=agent_version)
    adapter = FakeAdapter(responses=[InvokeResponse(text=content, tokens_in=12, tokens_out=8)])
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
        agent_run.id, worker_id="test-worker"
    )
    db.refresh(agent_run)
    artifact = db.execute(select(Artifact).where(Artifact.agent_run_id == agent_run.id)).scalars().first()
    return SimpleNamespace(
        project=project,
        agent=agent,
        agent_version=agent_version,
        model=model,
        provider=provider,
        provider_model=pm,
        task=task,
        task_run=task_run,
        agent_run=agent_run,
        artifact=artifact,
    )


def _published_version(db, project, criteria):
    from tests.conftest import make_evaluation_definition

    definition = make_evaluation_definition(db, project=project)
    db.commit()
    version = EvaluationDefinitionService(db).create_version(
        evaluation_definition_id=definition.id, description=None, criteria=criteria
    )
    return EvaluationDefinitionService(db).publish_version(version.id), definition


def _valid_evaluator_response(findings: dict) -> str:
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


# -- deterministic detail ----------------------------------------------------------


def test_deterministic_detail_has_full_subject_provenance_no_evaluator_block(db, tmp_path):
    s = _executed_subject(db, tmp_path)
    version, definition = _published_version(
        db, s.project, [{"key": "non_empty_output", "label": "Non-empty output"}]
    )

    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run.id, worker_id="test-worker")

    detail = get_evaluation_run_detail(db, run.id)
    assert detail is not None
    assert detail.run.method == EvaluationMethod.DETERMINISTIC
    assert detail.run.status == EvaluationRunStatus.COMPLETED

    assert detail.subject.agent_run_id == s.agent_run.id
    assert detail.subject.agent.agent_id == s.agent.id
    assert detail.subject.agent.agent_name == s.agent.name
    assert detail.subject.agent.agent_version_id == s.agent_version.id
    assert detail.subject.agent.agent_version == s.agent_version.version
    assert detail.subject.agent.agent_version_role == s.agent_version.role
    assert detail.subject.model is not None
    assert detail.subject.model.model_id == s.model.id
    assert detail.subject.model.canonical_model_id == s.model.canonical_model_id
    assert detail.subject.model.provider_id == s.provider.id
    assert detail.subject.model.provider_name == s.provider.name
    assert detail.subject.model.provider_model_snapshot_id is not None
    assert detail.subject.artifact_id == s.artifact.id
    assert detail.subject.artifact_content_hash == s.artifact.content_hash

    assert detail.evaluation_definition.evaluation_definition_id == definition.id
    assert detail.evaluation_definition.evaluation_definition_name == definition.name
    assert detail.evaluation_definition.evaluation_definition_version_id == version.id
    assert detail.evaluation_definition.evaluation_definition_version == version.version

    # DETERMINISTIC: evaluator block absent, never fabricated
    assert detail.evaluator is None


def test_criterion_label_description_and_order_preserved(db, tmp_path):
    s = _executed_subject(db, tmp_path)
    criteria = [
        {"key": "coverage", "label": "Test coverage", "description": "Are edge cases tested?"},
        {"key": "non_empty_output", "label": "Non-empty output", "description": "Output is not blank"},
    ]
    version, _definition = _published_version(db, s.project, criteria)

    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run.id, worker_id="test-worker")

    detail = get_evaluation_run_detail(db, run.id)
    assert [c.criterion_key for c in detail.criterion_results] == ["coverage", "non_empty_output"]
    by_key = {c.criterion_key: c for c in detail.criterion_results}
    assert by_key["coverage"].criterion_label == "Test coverage"
    assert by_key["coverage"].criterion_description == "Are edge cases tested?"
    assert by_key["coverage"].finding == EvaluationFinding.NOT_APPLICABLE  # no deterministic checker for it
    assert by_key["non_empty_output"].criterion_label == "Non-empty output"
    assert by_key["non_empty_output"].finding == EvaluationFinding.MET
    assert by_key["non_empty_output"].evidence_refs == [s.artifact.id]


def test_deterministic_execution_evidence_null_no_model_calls(db, tmp_path):
    """DETERMINISTIC never invokes a model -- execution_evidence has no
    meaning here, but this proves the read model never fabricates a
    non-null block where no ModelCall rows could possibly exist."""
    s = _executed_subject(db, tmp_path)
    version, _definition = _published_version(
        db, s.project, [{"key": "non_empty_output", "label": "Non-empty output"}]
    )
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run.id, worker_id="test-worker")

    detail = get_evaluation_run_detail(db, run.id)
    assert detail.evaluator is None  # DETERMINISTIC has no evaluator block at all


# -- AGENT_EVALUATOR detail --------------------------------------------------------


def test_agent_evaluator_detail_full_subject_and_evaluator_provenance(db, tmp_path):
    s = _executed_subject(db, tmp_path)
    _evaluator_agent, evaluator_version, evaluator_model, evaluator_provider, _pm = _published_agent_version(
        db, s.project, name="Evaluator"
    )
    version, _definition = _published_version(db, s.project, [{"key": "correctness", "label": "Correctness"}])

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
        responses=[
            InvokeResponse(
                text=_valid_evaluator_response({"correctness": "MET"}), tokens_in=40, tokens_out=25
            )
        ],
    )

    detail = get_evaluation_run_detail(db, run.id)
    assert detail.run.method == EvaluationMethod.AGENT_EVALUATOR
    assert detail.run.status == EvaluationRunStatus.COMPLETED

    # subject provenance unaffected by evaluator method
    assert detail.subject.agent.agent_version_id == s.agent_version.id
    assert detail.subject.model.canonical_model_id == s.model.canonical_model_id

    assert detail.evaluator is not None
    assert detail.evaluator.agent.agent_version_id == evaluator_version.id
    assert detail.evaluator.agent.agent_id == _evaluator_agent.id
    assert detail.evaluator.agent.agent_name == _evaluator_agent.name
    assert detail.evaluator.agent.agent_version == evaluator_version.version
    assert detail.evaluator.agent_run_id == run.evaluator_agent_run_id
    assert detail.evaluator.model is not None
    assert detail.evaluator.model.canonical_model_id == evaluator_model.canonical_model_id
    assert detail.evaluator.model.provider_name == evaluator_provider.name

    assert detail.evaluator.execution_evidence is not None
    ev = detail.evaluator.execution_evidence
    assert ev.tokens_in == 40
    assert ev.tokens_out == 25
    assert ev.total_tokens == 65
    assert ev.cost_amount == Decimal(0)  # free test model
    assert ev.cost_is_estimated in (True, False)
    assert ev.latency_ms >= 0

    # cross-check against the underlying ModelCall row directly -- the
    # read model must never invent a number ModelCall doesn't have.
    calls = list(
        db.execute(select(ModelCall).where(ModelCall.agent_run_id == run.evaluator_agent_run_id))
        .scalars()
        .all()
    )
    assert len(calls) == 1
    assert ev.tokens_in == calls[0].tokens_in
    assert ev.tokens_out == calls[0].tokens_out
    assert ev.latency_ms == (calls[0].latency_ms or 0)


def test_agent_evaluator_detail_null_execution_evidence_before_any_model_call(db, tmp_path):
    """A cancelled evaluator run that never reached the provider has zero
    ModelCall rows -- execution_evidence must be None, never zeros."""
    s = _executed_subject(db, tmp_path)
    _evaluator_agent, evaluator_version, *_rest = _published_agent_version(db, s.project, name="Evaluator")
    version, _definition = _published_version(db, s.project, [{"key": "a", "label": "A"}])
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
    _run_evaluator(db, run.evaluator_agent_run_id, responses=[])  # cancellation observed pre-invocation

    detail = get_evaluation_run_detail(db, run.id)
    assert detail.run.status == EvaluationRunStatus.CANCELLED
    assert detail.run.failure_reason is not None
    assert detail.evaluator is not None
    assert detail.evaluator.execution_evidence is None  # zero ModelCall rows -- null, not zero


# -- failed evaluation detail -------------------------------------------------------


def test_failed_agent_evaluator_detail_still_exposes_provenance_and_evidence(db, tmp_path):
    """An evaluator whose inference *succeeded* (tokens spent) but whose
    output failed contract parsing still lands FAILED -- the read model
    must still expose the real execution evidence that was spent, and a
    safe failure_reason, never silently drop provenance on failure."""
    s = _executed_subject(db, tmp_path)
    _evaluator_agent, evaluator_version, *_rest = _published_agent_version(db, s.project, name="Evaluator")
    version, _definition = _published_version(db, s.project, [{"key": "a", "label": "A"}])
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
        responses=[InvokeResponse(text="not valid json at all {{{", tokens_in=7, tokens_out=3)],
    )

    detail = get_evaluation_run_detail(db, run.id)
    assert detail.run.status == EvaluationRunStatus.FAILED
    assert detail.run.failure_reason is not None
    assert detail.criterion_results == []
    assert detail.evaluator is not None
    assert detail.evaluator.execution_evidence is not None
    assert detail.evaluator.execution_evidence.tokens_in == 7
    assert detail.evaluator.execution_evidence.tokens_out == 3


def test_failed_deterministic_detail_exposes_failure_reason_no_evaluator_block(db, tmp_path):
    s = _executed_subject(db, tmp_path)
    version, _definition = _published_version(
        db, s.project, [{"key": "non_empty_output", "label": "Non-empty output"}]
    )
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    # simulate the subject artifact changing underneath the bound hash
    s.artifact.content_hash = "0" * 64
    db.commit()
    svc.execute(run.id, worker_id="test-worker")

    detail = get_evaluation_run_detail(db, run.id)
    assert detail.run.status == EvaluationRunStatus.FAILED
    assert detail.run.failure_reason is not None
    assert detail.evaluator is None


# -- side-effect freedom / MA5 isolation --------------------------------------------


def test_get_evaluation_run_detail_makes_no_model_or_provider_calls(db, tmp_path, monkeypatch):
    s = _executed_subject(db, tmp_path)
    _evaluator_agent, evaluator_version, *_rest = _published_agent_version(db, s.project, name="Evaluator")
    version, _definition = _published_version(db, s.project, [{"key": "a", "label": "A"}])
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

    def _forbidden_invoke(*args, **kwargs):
        raise AssertionError("get_evaluation_run_detail must never invoke a provider/model adapter")

    monkeypatch.setattr(FakeAdapter, "invoke", _forbidden_invoke)

    detail = get_evaluation_run_detail(db, run.id)  # must not raise
    assert detail is not None


def test_get_evaluation_run_detail_creates_no_jobs_or_runs(db, tmp_path):
    s = _executed_subject(db, tmp_path)
    version, _definition = _published_version(
        db, s.project, [{"key": "non_empty_output", "label": "Non-empty output"}]
    )
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run.id, worker_id="test-worker")

    jobs_before = db.query(JobQueue).count()
    runs_before = db.query(EvaluationRun).count()
    agent_runs_before = db.query(AgentRun).count()

    for _ in range(3):
        get_evaluation_run_detail(db, run.id)

    assert db.query(JobQueue).count() == jobs_before
    assert db.query(EvaluationRun).count() == runs_before
    assert db.query(AgentRun).count() == agent_runs_before


def test_evaluation_run_detail_response_has_no_score_percentage_rank_or_winner_field(db, tmp_path):
    s = _executed_subject(db, tmp_path)
    version, _definition = _published_version(
        db, s.project, [{"key": "non_empty_output", "label": "Non-empty output"}]
    )
    svc = EvaluationExecutionService(db)
    run = svc.create_run(
        agent_run_id=s.agent_run.id,
        subject_artifact_id=s.artifact.id,
        evaluation_definition_version_id=version.id,
    )
    svc.execute(run.id, worker_id="test-worker")

    from app.api.routers.evaluation_runs import _detail_read

    detail = get_evaluation_run_detail(db, run.id)
    blob = _detail_read(detail).model_dump_json().lower()
    forbidden_terms = ["score", "percentage", "percent", "rank", "winner", "grade", "recommend"]
    for term in forbidden_terms:
        assert term not in blob, f"forbidden term {term!r} found in evaluation run detail response"
