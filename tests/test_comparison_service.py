"""ComparisonService — MA5 core orchestration scenarios.

Every test drives AgentExecutionService directly with a fake ProviderAdapter
(no real network), exactly like tests/test_execution_service.py and
tests/test_review_orchestration_service.py — the deterministic suite must
never require Internet. The one live OpenRouter comparison is a separate,
explicit operator smoke test (scripts/smoke_comparison_openrouter.py).

Covers: candidate configuration/validation, launch onto MA3's queue/worker
plane, candidate failure isolation, same-Agent/different-model and
different-Agent/same-model resolution, concurrent budget-reservation
correctness, human canonical selection (hash-bound, one-shot, never
automatic), cooperative cancellation, the derived comparison phase, and the
Flight Recorder event lifecycle.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, List

import pytest
from sqlalchemy import select

from app.db.enums import ComparisonRunStatus, ExecutionMode, JobType, TaskRunStatus, VersionStatus
from app.errors import ArtifactHashMismatchError, ConflictError, InvalidStateTransitionError
from app.models.artifacts_eval import Artifact, ComparisonCandidate, ComparisonRun
from app.models.execution import JobQueue
from app.models.observability import AuditEvent, ExecutionEvent
from app.models.tasks import AgentRun, TaskRun
from app.providers.base import InvokeRequest, InvokeResponse
from app.services.comparison_service import ComparisonService, compute_comparison_phase, get_comparison_detail
from app.services.execution_service import AgentExecutionService
from tests.conftest import (
    make_agent,
    make_agent_version,
    make_budget,
    make_model,
    make_project,
    make_provider,
    make_provider_model,
    make_task,
)

# -- helpers ------------------------------------------------------------------


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


def _run_candidate(db, agent_run_id: str, *, text: str = "OK", tokens_in=10, tokens_out=2) -> None:
    adapter = FakeAdapter(responses=[InvokeResponse(text=text, tokens_in=tokens_in, tokens_out=tokens_out)])
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
        agent_run_id, worker_id="test-worker"
    )


def _run_candidate_failing(db, agent_run_id: str, exc: Exception) -> None:
    adapter = FakeAdapter(responses=[exc])
    AgentExecutionService(db, adapter_factory=_factory_for(adapter)).execute(
        agent_run_id, worker_id="test-worker"
    )


def _comparison_task(db, *, project=None):
    project = project or make_project(db)
    task = make_task(db, project=project, execution_mode=ExecutionMode.PARALLEL_COMPARISON)
    return project, task


def _agent_version(db, project, name="Engineer", agent=None):
    """Published Agent Version with a working default FREE manual model
    policy already attached, so a plain ``_run_candidate`` call succeeds
    without every test having to wire up its own provider/model -- tests
    that care about a *specific* model just overwrite ``.model_policy``
    (or use a per-candidate ``model_policy_override``) afterward."""
    agent = agent or make_agent(db, project=project, name=name)
    version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    pm = _pm(
        db,
        canonical_model_id=f"model/{name}",
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    version.model_policy = _manual(pm)
    return version


def _pm(db, provider=None, canonical_model_id="model/x", **kwargs):
    provider = provider or make_provider(db)
    model = make_model(db, canonical_model_id=canonical_model_id)
    return make_provider_model(db, model=model, provider=provider, **kwargs)


def _make_user(db, project) -> str:
    from app.models.identity import User

    user = User(org_id=project.org_id, email=f"{project.id}@example.com")
    db.add(user)
    db.flush()
    return user.id


def _candidates_of(db, comparison_id) -> List[ComparisonCandidate]:
    return list(
        db.execute(
            select(ComparisonCandidate)
            .where(ComparisonCandidate.comparison_run_id == comparison_id)
            .order_by(ComparisonCandidate.created_at)
        )
        .scalars()
        .all()
    )


def _events(db, task_run_id) -> List[str]:
    stmt = (
        select(ExecutionEvent.event_type)
        .where(ExecutionEvent.task_run_id == task_run_id)
        .order_by(ExecutionEvent.sequence_number)
    )
    return [row[0] for row in db.execute(stmt).all()]


# -- create_comparison: validation ---------------------------------------------


def test_create_comparison_requires_parallel_comparison_execution_mode(db):
    project = make_project(db)
    task = make_task(db, project=project, execution_mode=ExecutionMode.SINGLE_AGENT)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()

    with pytest.raises(ConflictError):
        ComparisonService(db).create_comparison(
            task_id=task.id,
            candidates=[
                {"agent_version_id": av1.id, "label": "A"},
                {"agent_version_id": av2.id, "label": "B"},
            ],
        )


def test_create_comparison_requires_at_least_two_candidates(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    db.commit()

    with pytest.raises(ConflictError):
        ComparisonService(db).create_comparison(
            task_id=task.id, candidates=[{"agent_version_id": av1.id, "label": "A"}]
        )


def test_create_comparison_rejects_duplicate_labels(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()

    with pytest.raises(ConflictError):
        ComparisonService(db).create_comparison(
            task_id=task.id,
            candidates=[
                {"agent_version_id": av1.id, "label": "Same"},
                {"agent_version_id": av2.id, "label": "Same"},
            ],
        )


def test_create_comparison_rejects_unpublished_agent_version(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    draft_agent = make_agent(db, project=project, name="Draft")
    draft_version = make_agent_version(db, agent=draft_agent, status=VersionStatus.DRAFT)
    db.commit()

    with pytest.raises(ConflictError):
        ComparisonService(db).create_comparison(
            task_id=task.id,
            candidates=[
                {"agent_version_id": av1.id, "label": "A"},
                {"agent_version_id": draft_version.id, "label": "B"},
            ],
        )


def test_create_comparison_configures_candidates_without_launching(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()

    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[
            {"agent_version_id": av1.id, "label": "Candidate A"},
            {"agent_version_id": av2.id, "label": "Candidate B", "model_policy_override": {"mode": "manual"}},
        ],
    )

    assert comparison.status == ComparisonRunStatus.PENDING
    candidates = _candidates_of(db, comparison.id)
    assert len(candidates) == 2
    assert all(c.task_run_id is None and c.agent_run_id is None for c in candidates)
    assert compute_comparison_phase(db, comparison) == "pending"

    # No job was enqueued yet -- configuration alone never dispatches work.
    assert db.execute(select(JobQueue)).scalars().all() == []


# -- launch_comparison ----------------------------------------------------------


def test_launch_creates_independent_task_runs_and_enqueues_jobs(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()

    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[
            {"agent_version_id": av1.id, "label": "A"},
            {"agent_version_id": av2.id, "label": "B"},
        ],
    )
    ComparisonService(db).launch_comparison(comparison.id)
    db.refresh(comparison)

    assert comparison.status == ComparisonRunStatus.RUNNING
    candidates = _candidates_of(db, comparison.id)
    task_run_ids = {c.task_run_id for c in candidates}
    agent_run_ids = {c.agent_run_id for c in candidates}
    assert len(task_run_ids) == 2  # every candidate gets its OWN Task Run
    assert len(agent_run_ids) == 2
    assert comparison.task_run_id not in task_run_ids  # never the bookkeeping run itself

    jobs = list(db.execute(select(JobQueue)).scalars().all())
    assert len(jobs) == 2
    assert {j.job_type for j in jobs} == {JobType.AGENT_RUN}
    assert {j.payload_ref for j in jobs} == agent_run_ids

    bookkeeping_run = db.get(TaskRun, comparison.task_run_id)
    assert bookkeeping_run.status == TaskRunStatus.RUNNING


def test_launch_twice_is_rejected(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )
    ComparisonService(db).launch_comparison(comparison.id)

    with pytest.raises(ConflictError):
        ComparisonService(db).launch_comparison(comparison.id)


def test_same_agent_different_model_candidates_resolve_independently(db):
    """Section 3: 'same Agent, different models' must be expressible."""
    project, task = _comparison_task(db)
    shared_agent = make_agent(db, project=project, name="Coder")
    shared_version = make_agent_version(db, agent=shared_agent, status=VersionStatus.ACTIVE)
    provider = make_provider(db)
    pm_x = _pm(db, provider=provider, canonical_model_id="model/x")
    pm_y = _pm(db, provider=provider, canonical_model_id="model/y")
    db.commit()

    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[
            {
                "agent_version_id": shared_version.id,
                "label": "X",
                "model_policy_override": _manual(pm_x),
            },
            {
                "agent_version_id": shared_version.id,
                "label": "Y",
                "model_policy_override": _manual(pm_y),
            },
        ],
    )
    ComparisonService(db).launch_comparison(comparison.id)

    candidates = {c.label: c for c in _candidates_of(db, comparison.id)}
    _run_candidate(db, candidates["X"].agent_run_id, text="X_RESULT")
    _run_candidate(db, candidates["Y"].agent_run_id, text="Y_RESULT")

    db.expire_all()
    run_x = db.get(AgentRun, candidates["X"].agent_run_id)
    run_y = db.get(AgentRun, candidates["Y"].agent_run_id)
    assert run_x.agent_version_id == run_y.agent_version_id  # same Agent Version
    assert run_x.model_id != run_y.model_id  # different resolved models
    assert run_x.status == run_y.status == "completed"


def test_different_agent_same_model_candidates(db):
    project, task = _comparison_task(db)
    provider = make_provider(db)
    pm = _pm(db, provider=provider, canonical_model_id="shared/model")
    av1 = _agent_version(db, project, "Engineer A")
    av2 = _agent_version(db, project, "Engineer B")
    av1.model_policy = _manual(pm)
    av2.model_policy = _manual(pm)
    db.commit()

    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )
    ComparisonService(db).launch_comparison(comparison.id)

    candidates = {c.label: c for c in _candidates_of(db, comparison.id)}
    _run_candidate(db, candidates["A"].agent_run_id)
    _run_candidate(db, candidates["B"].agent_run_id)

    db.expire_all()
    run_a = db.get(AgentRun, candidates["A"].agent_run_id)
    run_b = db.get(AgentRun, candidates["B"].agent_run_id)
    assert run_a.agent_version_id != run_b.agent_version_id  # different Agents
    assert run_a.model_id == run_b.model_id  # same resolved model


# -- candidate failure isolation -------------------------------------------------


def test_one_candidate_failure_does_not_affect_sibling_success(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )
    ComparisonService(db).launch_comparison(comparison.id)
    candidates = {c.label: c for c in _candidates_of(db, comparison.id)}

    from app.providers.base import ProviderConnectionError

    _run_candidate_failing(db, candidates["A"].agent_run_id, ProviderConnectionError("boom"))
    # retries are bounded (settings.max_agent_run_attempts) -- exhaust them
    # deterministically rather than relying on count.
    from app.config import settings

    for _ in range(settings.max_agent_run_attempts - 1):
        db.expire_all()
        run = db.get(AgentRun, candidates["A"].agent_run_id)
        if run.status.value == "failed":
            break
        _run_candidate_failing(db, candidates["A"].agent_run_id, ProviderConnectionError("boom"))

    _run_candidate(db, candidates["B"].agent_run_id, text="B_SUCCEEDED")

    db.expire_all()
    task_run_a = db.get(TaskRun, candidates["A"].task_run_id)
    task_run_b = db.get(TaskRun, candidates["B"].task_run_id)
    assert task_run_a.status == TaskRunStatus.FAILED
    assert task_run_b.status == TaskRunStatus.COMPLETED

    comparison = db.get(ComparisonRun, comparison.id)
    # The comparison is NOT stored FAILED just because one candidate failed
    # -- the successful sibling's result is fully preserved and selectable.
    assert comparison.status == ComparisonRunStatus.RUNNING
    assert compute_comparison_phase(db, comparison) == "ready_for_selection"

    artifact_b = db.execute(
        select(Artifact).where(Artifact.agent_run_id == candidates["B"].agent_run_id)
    ).scalar_one()
    assert Path(artifact_b.storage_ref).read_text(encoding="utf-8") == "B_SUCCEEDED"


def test_all_candidates_failing_stores_comparison_failed(db):
    from app.providers.base import ProviderAuthenticationError

    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )
    ComparisonService(db).launch_comparison(comparison.id)
    candidates = {c.label: c for c in _candidates_of(db, comparison.id)}

    # Authentication errors are never retried -- one call each is terminal.
    _run_candidate_failing(db, candidates["A"].agent_run_id, ProviderAuthenticationError("bad key"))
    _run_candidate_failing(db, candidates["B"].agent_run_id, ProviderAuthenticationError("bad key"))

    db.expire_all()
    comparison = db.get(ComparisonRun, comparison.id)
    assert comparison.status == ComparisonRunStatus.FAILED
    assert compute_comparison_phase(db, comparison) == "failed"


# -- budget / concurrent reservation correctness ---------------------------------


def test_concurrent_candidates_share_budget_reservation_correctly(db):
    """Section 24.4 #13: N parallel candidates sharing one budget must not
    each check a stale total and all proceed. Reusing the exact
    BudgetGovernor/BudgetReservation machinery MA3 already built -- no new
    budget code for MA5, only proof it composes correctly across candidates."""
    project, task = _comparison_task(db)
    provider = make_provider(db)
    pm = _pm(
        db,
        provider=provider,
        canonical_model_id="paid/model",
        cost_input_per_mtok=Decimal("100.00"),
        cost_output_per_mtok=Decimal("100.00"),
    )
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    av1.model_policy = _manual(pm)
    av2.model_policy = _manual(pm)
    budget = make_budget(db, project=project, limit_amount="0.001")  # first reservation alone exceeds this
    db.commit()

    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
        budget_id=budget.id,
    )
    ComparisonService(db).launch_comparison(comparison.id)
    candidates = {c.label: c for c in _candidates_of(db, comparison.id)}

    for candidate in candidates.values():
        task_run = db.get(TaskRun, candidate.task_run_id)
        assert task_run.budget_id == budget.id  # the shared budget, not a copy

    # Both candidates are rejected by the hard budget threshold before any
    # provider call is made -- proving the reservation check runs, and runs
    # independently per candidate (neither silently bypasses it).
    _run_candidate_failing(db, candidates["A"].agent_run_id, AssertionError("should not be invoked"))
    db.expire_all()
    run_a = db.get(AgentRun, candidates["A"].agent_run_id)
    assert run_a.status.value == "failed"

    _run_candidate_failing(db, candidates["B"].agent_run_id, AssertionError("should not be invoked"))
    db.expire_all()
    run_b = db.get(AgentRun, candidates["B"].agent_run_id)
    assert run_b.status.value == "failed"

    comparison = db.get(ComparisonRun, comparison.id)
    assert comparison.status == ComparisonRunStatus.FAILED  # neither candidate ever ran


# -- human canonical selection ---------------------------------------------------


def _ready_comparison(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )
    ComparisonService(db).launch_comparison(comparison.id)
    candidates = {c.label: c for c in _candidates_of(db, comparison.id)}
    _run_candidate(db, candidates["A"].agent_run_id, text="RESULT_A")
    _run_candidate(db, candidates["B"].agent_run_id, text="RESULT_B")
    db.expire_all()
    comparison = db.get(ComparisonRun, comparison.id)
    return project, task, comparison, {c.label: c for c in _candidates_of(db, comparison.id)}


def test_select_canonical_rejected_before_ready(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )

    with pytest.raises(InvalidStateTransitionError):
        ComparisonService(db).select_canonical(
            comparison.id, candidate_id="whatever", artifact_hash="deadbeef", selected_by=None
        )


def test_select_canonical_rejects_artifact_hash_mismatch(db):
    _, _, comparison, candidates = _ready_comparison(db)
    candidate_a = candidates["A"]

    with pytest.raises(ArtifactHashMismatchError):
        ComparisonService(db).select_canonical(
            comparison.id, candidate_id=candidate_a.id, artifact_hash="not-the-real-hash", selected_by=None
        )

    db.expire_all()
    comparison = db.get(ComparisonRun, comparison.id)
    assert comparison.status == ComparisonRunStatus.RUNNING  # rejected selection never mutates state


def test_select_canonical_succeeds_and_is_final(db):
    project, _task, comparison, candidates = _ready_comparison(db)
    candidate_a = candidates["A"]
    artifact_a = db.execute(
        select(Artifact).where(Artifact.agent_run_id == candidate_a.agent_run_id)
    ).scalar_one()
    user_id = _make_user(db, project)
    db.commit()

    result = ComparisonService(db).select_canonical(
        comparison.id, candidate_id=candidate_a.id, artifact_hash=artifact_a.content_hash, selected_by=user_id
    )

    assert result.status == ComparisonRunStatus.COMPLETED
    assert result.winner_agent_run_id == candidate_a.agent_run_id
    assert result.winner_artifact_id == artifact_a.id
    assert result.winner_artifact_hash == artifact_a.content_hash

    db.refresh(candidate_a)
    assert candidate_a.is_winner is True

    bookkeeping_run = db.get(TaskRun, comparison.task_run_id)
    assert bookkeeping_run.status == TaskRunStatus.COMPLETED
    assert bookkeeping_run.final_artifact_id == artifact_a.id

    # Audit trail for the human decision (Section 20.5) -- structurally
    # separate from the Flight Recorder.
    audit_rows = list(db.execute(select(AuditEvent)).scalars().all())
    matching = [a for a in audit_rows if a.event_type == "comparison.canonical_candidate_selected"]
    assert len(matching) == 1
    assert matching[0].actor_user_id == user_id
    assert matching[0].detail["artifact_hash"] == artifact_a.content_hash

    # Never automatic, never re-selectable: a second selection attempt is
    # refused even against the same candidate/hash.
    with pytest.raises(ConflictError):
        ComparisonService(db).select_canonical(
            comparison.id,
            candidate_id=candidate_a.id,
            artifact_hash=artifact_a.content_hash,
            selected_by=user_id,
        )


def test_select_canonical_rejects_a_failed_candidate(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )
    ComparisonService(db).launch_comparison(comparison.id)
    candidates = {c.label: c for c in _candidates_of(db, comparison.id)}

    from app.providers.base import ProviderAuthenticationError

    _run_candidate_failing(db, candidates["A"].agent_run_id, ProviderAuthenticationError("bad key"))
    _run_candidate(db, candidates["B"].agent_run_id, text="OK")
    db.expire_all()
    comparison = db.get(ComparisonRun, comparison.id)

    with pytest.raises(ConflictError):
        ComparisonService(db).select_canonical(
            comparison.id, candidate_id=candidates["A"].id, artifact_hash="anything", selected_by=None
        )


# -- cancellation -----------------------------------------------------------------


def test_cancel_pending_comparison_is_immediate(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )

    result = ComparisonService(db).cancel_comparison(comparison.id, requested_by="user-1")
    assert result.status == ComparisonRunStatus.CANCELLED
    assert compute_comparison_phase(db, result) == "cancelled"


def test_cancel_running_comparison_cascades_and_completes_cooperatively(db):
    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )
    ComparisonService(db).launch_comparison(comparison.id)
    candidates = {c.label: c for c in _candidates_of(db, comparison.id)}

    user_id = _make_user(db, project)
    db.commit()

    result = ComparisonService(db).cancel_comparison(comparison.id, requested_by=user_id)
    assert result.cancellation_requested_at is not None
    assert result.status == ComparisonRunStatus.RUNNING  # cooperative -- not instant

    for candidate in candidates.values():
        task_run = db.get(TaskRun, candidate.task_run_id)
        assert task_run.cancellation_requested_at is not None

    # A second cancel request is refused.
    with pytest.raises(InvalidStateTransitionError):
        ComparisonService(db).cancel_comparison(comparison.id, requested_by=user_id)

    # Workers observe cancellation at their next checkpoint (Section 26.7) --
    # simulate that by actually running each candidate's Agent Run, which
    # checks cancellation before invoking the provider.
    _run_candidate_failing(db, candidates["A"].agent_run_id, AssertionError("must not invoke after cancel"))
    _run_candidate_failing(db, candidates["B"].agent_run_id, AssertionError("must not invoke after cancel"))

    db.expire_all()
    comparison = db.get(ComparisonRun, comparison.id)
    assert comparison.status == ComparisonRunStatus.CANCELLED
    assert compute_comparison_phase(db, comparison) == "cancelled"


def test_cancel_already_terminal_comparison_rejected(db):
    _project, _task, comparison, _candidates = _ready_comparison(db)
    ComparisonService(db).select_canonical(
        comparison.id,
        candidate_id=_candidates["A"].id,
        artifact_hash=db.execute(
            select(Artifact).where(Artifact.agent_run_id == _candidates["A"].agent_run_id)
        )
        .scalar_one()
        .content_hash,
        selected_by=None,
    )
    with pytest.raises(InvalidStateTransitionError):
        ComparisonService(db).cancel_comparison(comparison.id, requested_by="user-1")


# -- consolidated detail / aggregate usage ---------------------------------------


def test_comparison_detail_aggregates_per_candidate_and_total_usage(db):
    _project, _task, comparison, _candidates = _ready_comparison(db)

    detail = get_comparison_detail(db, comparison.id)
    assert detail.phase == "ready_for_selection"
    by_label = {c.candidate.label: c for c in detail.candidates}
    assert by_label["A"].status == "completed"
    assert by_label["A"].usage.tokens_in == 10
    assert by_label["A"].artifact is not None
    assert detail.total_tokens_in == by_label["A"].usage.tokens_in + by_label["B"].usage.tokens_in
    assert detail.total_cost == by_label["A"].usage.cost_amount + by_label["B"].usage.cost_amount


# -- Flight Recorder lifecycle ----------------------------------------------------


def test_flight_recorder_event_lifecycle(db):
    _project, _task, comparison, candidates = _ready_comparison(db)
    bookkeeping_events = _events(db, comparison.task_run_id)

    assert "comparison.created" in bookkeeping_events
    assert "comparison.launched" in bookkeeping_events
    assert bookkeeping_events.count("comparison.candidate_queued") == 2
    assert "comparison.candidates_terminal" in bookkeeping_events
    assert "comparison.ready_for_selection" in bookkeeping_events

    candidate_a = candidates["A"]
    candidate_events = _events(db, candidate_a.task_run_id)
    assert "task_run.created" in candidate_events
    assert "task_run.queued" in candidate_events
    assert "task_run.completed" in candidate_events

    artifact_a = db.execute(
        select(Artifact).where(Artifact.agent_run_id == candidate_a.agent_run_id)
    ).scalar_one()
    ComparisonService(db).select_canonical(
        comparison.id, candidate_id=candidate_a.id, artifact_hash=artifact_a.content_hash, selected_by=None
    )
    assert "comparison.canonical_selected" in _events(db, comparison.task_run_id)


# -- parallel-capable queue execution / recovery / fencing -----------------------
#
# ComparisonService.launch_comparison enqueues each candidate as an ordinary
# JobType.AGENT_RUN row via the exact same JobQueueRepository MA3 already
# built -- no second queue, no new lease/fencing mechanism. These two tests
# prove that inheritance is real, not just architectural intent: any number
# of workers may claim the independent candidate jobs in any order (no
# ordering coupling between candidates), and a worker that dies mid-candidate
# is recovered by fencing exactly like any other Agent Run.


def test_comparison_candidate_jobs_are_independent_and_worker_order_agnostic(db, fast_worker, monkeypatch):
    import app.services.execution_service as execution_service_module

    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    av3 = _agent_version(db, project, "C")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[
            {"agent_version_id": av1.id, "label": "A"},
            {"agent_version_id": av2.id, "label": "B"},
            {"agent_version_id": av3.id, "label": "C"},
        ],
    )
    ComparisonService(db).launch_comparison(comparison.id)
    candidates = _candidates_of(db, comparison.id)
    assert len(candidates) == 3

    jobs_before = list(db.execute(select(JobQueue)).scalars().all())
    assert len(jobs_before) == 3  # one independent job_queue row per candidate

    executed = []

    def fake_execute(self, agent_run_id, *, worker_id):
        executed.append(agent_run_id)

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "execute", fake_execute)

    for _ in range(3):
        processed = fast_worker.run_once()
        assert processed is True

    # Every candidate's job was claimed and processed exactly once, with no
    # coupling to a particular order -- a real deployment could run this
    # with N worker processes claiming these rows concurrently.
    assert sorted(executed) == sorted(c.agent_run_id for c in candidates)
    db.expire_all()
    assert all(j.status.value == "done" for j in db.execute(select(JobQueue)).scalars().all())


def test_dead_worker_on_comparison_candidate_is_reclaimed_by_fencing(db, session_factory):
    """Mirrors tests/test_worker_agent_run.py's
    test_dead_worker_lease_is_reclaimed_and_stale_completion_rejected
    exactly, seeded from a comparison-launched candidate instead of a plain
    MA3 Agent Run -- proving MA5 introduces no new recovery mechanism."""
    from app.db.enums import JobQueueStatus
    from app.repositories.job_queue_repository import JobQueueRepository

    repo = JobQueueRepository()

    project, task = _comparison_task(db)
    av1 = _agent_version(db, project, "A")
    av2 = _agent_version(db, project, "B")
    db.commit()
    comparison = ComparisonService(db).create_comparison(
        task_id=task.id,
        candidates=[{"agent_version_id": av1.id, "label": "A"}, {"agent_version_id": av2.id, "label": "B"}],
    )
    ComparisonService(db).launch_comparison(comparison.id)
    candidate_a = _candidates_of(db, comparison.id)[0]

    job = db.execute(select(JobQueue).where(JobQueue.payload_ref == candidate_a.agent_run_id)).scalar_one()

    from datetime import datetime, timedelta, timezone

    claim_db = session_factory()
    try:
        claimed = repo.claim_one(claim_db, worker_id="worker-a", lease_seconds=30)
        assert claimed is not None
        zombie_fencing_token = claimed.fencing_token
        claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        claim_db.commit()
    finally:
        claim_db.close()

    reclaim_db = session_factory()
    try:
        reclaimed = repo.claim_one(reclaim_db, worker_id="worker-b", lease_seconds=30)
    finally:
        reclaim_db.close()

    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.fencing_token == zombie_fencing_token + 1

    worker_b_completed = repo.complete(
        db,
        job_id=job.id,
        worker_id="worker-b",
        fencing_token=reclaimed.fencing_token,
        status=JobQueueStatus.DONE,
    )
    assert worker_b_completed is True

    zombie_completed = repo.complete(
        db,
        job_id=job.id,
        worker_id="worker-a",
        fencing_token=zombie_fencing_token,
        status=JobQueueStatus.FAILED,
    )
    assert zombie_completed is False  # the zombie's stale write must never be honored

    db.expire_all()
    assert db.get(JobQueue, job.id).status == JobQueueStatus.DONE
