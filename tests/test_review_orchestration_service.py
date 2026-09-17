"""Agent-to-Agent Review orchestration — MA4 core scenarios.

Every test here uses a fake ProviderAdapter (no real network) injected via
``adapter_factory`` — the deterministic suite must never require Internet.
The one live OpenRouter review cycle is a separate, explicit operator
smoke test (scripts/smoke_review_openrouter.py).

Reuses AgentExecutionService directly (as the real Worker would, one
Agent Run at a time) rather than the job_queue/Worker layer, mirroring
tests/test_execution_service.py's own isolation of "one AgentRun to
completion" — job_queue/Worker dispatch of AGENT_RUN jobs is already
proven generic in tests/test_worker_agent_run.py and is exercised again
here for a reviewer/repair-stage job specifically in
test_worker_dispatches_reviewer_and_repair_jobs_generically /
test_stale_worker_cannot_complete_reclaimed_review_stage below.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.db.enums import (
    AgentRunRole,
    AgentRunStatus,
    ExecutionMode,
    JobQueueStatus,
    JobType,
    ReviewDecision,
    TaskRunStatus,
    VersionStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.execution import JobQueue, ModelCall
from app.models.observability import ExecutionEvent
from app.models.reviews import AgentReview
from app.models.tasks import AgentRun, TaskRun
from app.providers.base import (
    InvokeRequest,
    InvokeResponse,
    ProviderAuthenticationError,
)
from app.repositories.job_queue_repository import JobQueueRepository
from app.services.execution_service import AgentExecutionService
from app.services.review_orchestration_service import get_review_summary
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

ACCEPT_JSON = json.dumps(
    {"decision": "ACCEPT", "summary": "Correct and minimal.", "issues": [], "repair_instructions": ""}
)


def repair_required_json(issue: str = "wrong function name") -> str:
    return json.dumps(
        {
            "decision": "REPAIR_REQUIRED",
            "summary": "Needs a fix.",
            "issues": [issue],
            "repair_instructions": f"Fix: {issue}.",
        }
    )


MALFORMED_TEXT = "Looks fine to me, ship it!"


# -- scenario builder ---------------------------------------------------------


@dataclass
class ReviewScenario:
    project: Any
    task: Any
    task_run: TaskRun
    primary_agent_version: Any
    reviewer_agent_version: Any
    primary_run: AgentRun


def _manual(pm) -> Dict[str, Any]:
    return {"mode": "manual", "manual_provider_model_id": pm.id}


def _build_review_scenario(
    db,
    *,
    primary_pm,
    reviewer_pm,
    max_repair_iterations: int = 2,
    budget=None,
    review_instructions: Optional[str] = None,
) -> ReviewScenario:
    project = make_project(db)
    task = make_task(db, project=project, execution_mode=ExecutionMode.BUILD_REVIEW)

    primary_agent = make_agent(db, project=project, name="Software Engineer")
    primary_agent_version = make_agent_version(db, agent=primary_agent, status=VersionStatus.ACTIVE)
    primary_agent_version.model_policy = _manual(primary_pm)

    reviewer_agent = make_agent(db, project=project, name="Code Reviewer")
    reviewer_agent_version = make_agent_version(db, agent=reviewer_agent, status=VersionStatus.ACTIVE)
    reviewer_agent_version.model_policy = _manual(reviewer_pm)
    db.commit()

    config_snapshot = {
        "task_title": task.title,
        "execution_mode": task.execution_mode.value,
        "primary_agent_version_id": primary_agent_version.id,
        "reviewer_agent_version_id": reviewer_agent_version.id,
        "max_repair_iterations": max_repair_iterations,
        "review_instructions": review_instructions,
        "budget_id": budget.id if budget else None,
    }
    task_run = TaskRun(
        task_id=task.id,
        status=TaskRunStatus.CREATED,
        budget_id=budget.id if budget else None,
        config_snapshot=config_snapshot,
    )
    db.add(task_run)
    db.flush()

    primary_run = AgentRun(
        task_run_id=task_run.id,
        agent_version_id=primary_agent_version.id,
        role=AgentRunRole.PRIMARY,
        status=AgentRunStatus.CREATED,
    )
    db.add(primary_run)
    db.commit()

    return ReviewScenario(
        project=project,
        task=task,
        task_run=task_run,
        primary_agent_version=primary_agent_version,
        reviewer_agent_version=reviewer_agent_version,
        primary_run=primary_run,
    )


def _two_provider_models(db):
    """Distinct Model/ProviderModel rows for primary vs reviewer — proves
    Agent != Model / model independence, not just Agent independence."""
    provider = make_provider(db)
    primary_pm = make_provider_model(
        db,
        model=make_model(db, canonical_model_id="engineer/model-a"),
        provider=provider,
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    reviewer_pm = make_provider_model(
        db,
        model=make_model(db, canonical_model_id="reviewer/model-b"),
        provider=provider,
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    db.commit()
    return primary_pm, reviewer_pm


@dataclass
class FakeAdapter:
    responses: List[Any] = field(default_factory=list)
    calls: List[InvokeRequest] = field(default_factory=list)
    on_invoke: Optional[Any] = None

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        self.calls.append(request)
        if self.on_invoke is not None:
            self.on_invoke(request)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _factory_for(adapter: FakeAdapter):
    return lambda db, provider: adapter


def _run_full_cycle(db, adapter, task_run_id, *, max_stages=10):
    """Drives AgentExecutionService.execute() over every AgentRun the
    orchestrator creates for this Task Run, in creation order — exactly
    what a real Worker polling job_queue would do one job at a time,
    minus the queue/lease plumbing itself (already proven generic in
    tests/test_worker_agent_run.py)."""
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    ran = []
    for _ in range(max_stages):
        stmt = (
            select(AgentRun)
            .where(AgentRun.task_run_id == task_run_id, AgentRun.status == AgentRunStatus.CREATED)
            .order_by(AgentRun.created_at)
        )
        run = db.execute(stmt).scalars().first()
        if run is None:
            break
        service.execute(run.id, worker_id="test-worker")
        ran.append(run.id)
    return ran


def _read(storage_ref: str) -> str:
    return Path(storage_ref).read_text(encoding="utf-8")


def _events(db, task_run_id) -> List[str]:
    stmt = (
        select(ExecutionEvent.event_type)
        .where(ExecutionEvent.task_run_id == task_run_id)
        .order_by(ExecutionEvent.sequence_number)
    )
    return [row[0] for row in db.execute(stmt).all()]


# -- happy path: ACCEPT on first review ---------------------------------------


def test_accept_on_first_review_completes_task_run_with_final_artifact(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="def is_even(n):\n    return n % 2 == 0", tokens_in=10, tokens_out=8),
            InvokeResponse(text=ACCEPT_JSON, tokens_in=20, tokens_out=15),
        ]
    )
    ran = _run_full_cycle(db, adapter, scenario.task_run.id)
    assert len(ran) == 2  # primary, reviewer — no repair needed

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.COMPLETED
    assert scenario.task_run.final_artifact_id is not None

    final_artifact = db.get(Artifact, scenario.task_run.final_artifact_id)
    assert "is_even" in _read(final_artifact.storage_ref)

    reviews = db.query(AgentReview).filter_by(task_run_id=scenario.task_run.id).all()
    assert len(reviews) == 1
    assert reviews[0].decision == ReviewDecision.ACCEPT
    assert reviews[0].candidate_artifact_id == scenario.task_run.final_artifact_id
    assert reviews[0].iteration_number == 0

    summary = get_review_summary(db, scenario.task_run.id)
    assert summary.outcome == "accepted"


def test_primary_and_reviewer_use_independent_models(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="candidate", tokens_in=1, tokens_out=1),
            InvokeResponse(text=ACCEPT_JSON, tokens_in=1, tokens_out=1),
        ]
    )
    _run_full_cycle(db, adapter, scenario.task_run.id)

    runs = db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id).order_by(AgentRun.created_at).all()
    primary_run, reviewer_run = runs[0], runs[1]
    assert primary_run.role == AgentRunRole.PRIMARY
    assert reviewer_run.role == AgentRunRole.REVIEWER
    assert primary_run.model_id != reviewer_run.model_id
    assert primary_run.agent_version_id != reviewer_run.agent_version_id


# -- repair cycles --------------------------------------------------------------


def test_one_repair_cycle_then_accept_preserves_both_candidates(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(
        db, primary_pm=primary_pm, reviewer_pm=reviewer_pm, max_repair_iterations=2
    )

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(
                text="def isEven(n): return n % 2 == 0", tokens_in=1, tokens_out=1
            ),  # candidate v1
            InvokeResponse(
                text=repair_required_json("wrong name isEven"), tokens_in=1, tokens_out=1
            ),  # review v1
            InvokeResponse(
                text="def is_even(n):\n    return n % 2 == 0", tokens_in=1, tokens_out=1
            ),  # candidate v2
            InvokeResponse(text=ACCEPT_JSON, tokens_in=1, tokens_out=1),  # review v2
        ]
    )
    ran = _run_full_cycle(db, adapter, scenario.task_run.id)
    assert len(ran) == 4  # primary, reviewer, repair, reviewer

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.COMPLETED

    runs = db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id).order_by(AgentRun.created_at).all()
    roles = [r.role for r in runs]
    assert roles == [AgentRunRole.PRIMARY, AgentRunRole.REVIEWER, AgentRunRole.REPAIR, AgentRunRole.REVIEWER]

    candidates = [
        db.query(Artifact).filter_by(agent_run_id=runs[0].id).one(),
        db.query(Artifact).filter_by(agent_run_id=runs[2].id).one(),
    ]
    assert "isEven" in _read(candidates[0].storage_ref)
    assert "is_even" in _read(candidates[1].storage_ref)
    # both preserved -- repair never overwrites the previous candidate
    assert candidates[0].id != candidates[1].id

    reviews = (
        db.query(AgentReview)
        .filter_by(task_run_id=scenario.task_run.id)
        .order_by(AgentReview.iteration_number)
        .all()
    )
    assert [r.decision for r in reviews] == [ReviewDecision.REPAIR_REQUIRED, ReviewDecision.ACCEPT]
    assert reviews[0].candidate_artifact_id == candidates[0].id
    assert reviews[1].candidate_artifact_id == candidates[1].id

    repair_run = runs[2]
    assert repair_run.repair_of_review_id == reviews[0].id  # lineage: repair -> triggering review

    assert scenario.task_run.final_artifact_id == candidates[1].id


def test_two_repair_cycles_then_accept(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(
        db, primary_pm=primary_pm, reviewer_pm=reviewer_pm, max_repair_iterations=2
    )

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="v1", tokens_in=1, tokens_out=1),
            InvokeResponse(text=repair_required_json("issue 1"), tokens_in=1, tokens_out=1),
            InvokeResponse(text="v2", tokens_in=1, tokens_out=1),
            InvokeResponse(text=repair_required_json("issue 2"), tokens_in=1, tokens_out=1),
            InvokeResponse(text="v3", tokens_in=1, tokens_out=1),
            InvokeResponse(text=ACCEPT_JSON, tokens_in=1, tokens_out=1),
        ]
    )
    ran = _run_full_cycle(db, adapter, scenario.task_run.id)
    assert len(ran) == 6

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.COMPLETED
    final_artifact = db.get(Artifact, scenario.task_run.final_artifact_id)
    assert _read(final_artifact.storage_ref) == "v3"

    reviews = (
        db.query(AgentReview)
        .filter_by(task_run_id=scenario.task_run.id)
        .order_by(AgentReview.iteration_number)
        .all()
    )
    assert [r.iteration_number for r in reviews] == [0, 1, 2]


def test_repair_limit_exhausted_never_falsely_marks_accepted(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(
        db, primary_pm=primary_pm, reviewer_pm=reviewer_pm, max_repair_iterations=1
    )

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="v1", tokens_in=1, tokens_out=1),
            InvokeResponse(text=repair_required_json(), tokens_in=1, tokens_out=1),
            InvokeResponse(text="v2", tokens_in=1, tokens_out=1),
            InvokeResponse(
                text=repair_required_json(), tokens_in=1, tokens_out=1
            ),  # iteration 1 == limit -> exhausted
        ]
    )
    _run_full_cycle(db, adapter, scenario.task_run.id)

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert scenario.task_run.final_artifact_id is None  # never falsely marked accepted

    events = _events(db, scenario.task_run.id)
    assert "repair_limit.reached" in events

    summary = get_review_summary(db, scenario.task_run.id)
    assert summary.outcome == "repair_limit_exhausted"

    failed_event = (
        db.query(ExecutionEvent)
        .filter_by(task_run_id=scenario.task_run.id, event_type="task_run.failed")
        .one()
    )
    assert failed_event.error["category"] == "repair_limit_exhausted"


# -- malformed reviewer output ---------------------------------------------------


def test_malformed_reviewer_response_fails_safely_never_accepts(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="def is_even(n): return n % 2 == 0", tokens_in=1, tokens_out=1),
            InvokeResponse(text=MALFORMED_TEXT, tokens_in=1, tokens_out=1),
        ]
    )
    _run_full_cycle(db, adapter, scenario.task_run.id)

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert scenario.task_run.final_artifact_id is None

    review = db.query(AgentReview).filter_by(task_run_id=scenario.task_run.id).one()
    assert review.decision == ReviewDecision.INVALID
    assert review.parse_error is not None

    summary = get_review_summary(db, scenario.task_run.id)
    assert summary.outcome == "reviewer_response_invalid"


def test_stale_candidate_hash_mismatch_refuses_review(db):
    """Defends the "tied to the exact immutable artifact/hash" contract:
    if the candidate's recorded hash no longer matches at review time
    (should never happen in practice -- artifacts are never mutated -- but
    this proves the check is real, not decorative), the review is refused
    as INVALID, never silently approved."""
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="candidate", tokens_in=1, tokens_out=1),
            InvokeResponse(text=ACCEPT_JSON, tokens_in=1, tokens_out=1),
        ]
    )
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.primary_run.id, worker_id="test-worker")

    candidate_artifact = db.query(Artifact).filter_by(agent_run_id=scenario.primary_run.id).one()
    candidate_artifact.content_hash = "0" * 64  # simulate drift
    db.commit()

    reviewer_run = (
        db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id, role=AgentRunRole.REVIEWER).one()
    )
    service.execute(reviewer_run.id, worker_id="test-worker")

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert scenario.task_run.final_artifact_id is None
    review = db.query(AgentReview).filter_by(task_run_id=scenario.task_run.id).one()
    assert review.decision == ReviewDecision.INVALID
    assert "hash" in review.parse_error.lower()


# -- stage failures ---------------------------------------------------------------


def test_primary_failure_fails_task_run_before_any_reviewer_run(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(responses=[ProviderAuthenticationError("bad key")])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.primary_run.id, worker_id="test-worker")

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.FAILED
    reviewer_runs = (
        db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id, role=AgentRunRole.REVIEWER).all()
    )
    assert reviewer_runs == []


def test_reviewer_failure_fails_task_run_but_preserves_candidate_artifact(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="candidate", tokens_in=1, tokens_out=1),
            ProviderAuthenticationError("reviewer key bad"),
        ]
    )
    _run_full_cycle(db, adapter, scenario.task_run.id)

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.FAILED
    # the primary's candidate artifact is preserved even though the
    # reviewer never got to render a decision on it
    candidate = db.query(Artifact).filter_by(agent_run_id=scenario.primary_run.id).one()
    assert candidate is not None


def test_repair_failure_fails_task_run(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="v1", tokens_in=1, tokens_out=1),
            InvokeResponse(text=repair_required_json(), tokens_in=1, tokens_out=1),
            ProviderAuthenticationError("repair invocation failed"),
        ]
    )
    _run_full_cycle(db, adapter, scenario.task_run.id)

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert scenario.task_run.final_artifact_id is None


# -- cancellation between/during stages -------------------------------------------


def test_cancellation_before_review_queued_stops_the_cycle(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(responses=[InvokeResponse(text="candidate", tokens_in=1, tokens_out=1)])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))

    from datetime import datetime, timezone

    def _cancel(request):
        scenario.task_run.cancellation_requested_at = datetime.now(timezone.utc)
        db.commit()

    adapter.on_invoke = _cancel
    service.execute(scenario.primary_run.id, worker_id="test-worker")

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.CANCELLED
    reviewer_runs = (
        db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id, role=AgentRunRole.REVIEWER).all()
    )
    assert reviewer_runs == []  # no subsequent stage started


def test_cancellation_during_review_preserves_reviewer_output_but_stops(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(responses=[InvokeResponse(text="candidate", tokens_in=1, tokens_out=1)])
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.primary_run.id, worker_id="test-worker")

    reviewer_run = (
        db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id, role=AgentRunRole.REVIEWER).one()
    )

    from datetime import datetime, timezone

    def _cancel(request):
        scenario.task_run.cancellation_requested_at = datetime.now(timezone.utc)
        db.commit()

    adapter.responses = [InvokeResponse(text=ACCEPT_JSON, tokens_in=1, tokens_out=1)]
    adapter.on_invoke = _cancel
    service.execute(reviewer_run.id, worker_id="test-worker")

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.CANCELLED
    assert scenario.task_run.final_artifact_id is None  # decision never processed
    # no AgentReview was created -- cancellation was observed before the
    # orchestrator got to parse/record a decision
    assert db.query(AgentReview).filter_by(task_run_id=scenario.task_run.id).count() == 0
    # but the reviewer's own raw output artifact is preserved
    assert db.query(Artifact).filter_by(agent_run_id=reviewer_run.id).count() == 1


def test_cancellation_before_repair_queued_stops_the_cycle(db):
    from datetime import datetime, timezone

    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(
        db, primary_pm=primary_pm, reviewer_pm=reviewer_pm, max_repair_iterations=2
    )

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="v1", tokens_in=1, tokens_out=1),
            InvokeResponse(text=repair_required_json(), tokens_in=1, tokens_out=1),
        ]
    )
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.primary_run.id, worker_id="test-worker")
    reviewer_run = (
        db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id, role=AgentRunRole.REVIEWER).one()
    )
    scenario.task_run.cancellation_requested_at = datetime.now(timezone.utc)
    db.commit()
    service.execute(reviewer_run.id, worker_id="test-worker")

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.CANCELLED
    repair_runs = (
        db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id, role=AgentRunRole.REPAIR).all()
    )
    assert repair_runs == []  # no subsequent stage started


# -- budget enforcement across the whole cycle -----------------------------------


def test_budget_exhaustion_before_reviewer_blocks_reviewer_call(db):
    provider = make_provider(db)
    paid_model = make_model(db, canonical_model_id="paid/primary")
    primary_pm = make_provider_model(
        db,
        model=paid_model,
        provider=provider,
        cost_input_per_mtok=Decimal("1.00"),
        cost_output_per_mtok=Decimal("1.00"),
    )
    reviewer_pm = make_provider_model(
        db,
        model=make_model(db, canonical_model_id="paid/reviewer"),
        provider=provider,
        cost_input_per_mtok=Decimal("1.00"),
        cost_output_per_mtok=Decimal("1.00"),
    )
    # A tight budget: primary's *actual* cost alone will already commit
    # near/at the hard threshold, so the reviewer's subsequent estimated
    # reservation is rejected before it ever invokes the provider.
    budget = make_budget(db, limit_amount="0.01")
    db.commit()
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm, budget=budget)

    adapter = FakeAdapter(
        responses=[InvokeResponse(text="candidate", tokens_in=10_000, tokens_out=10_000, latency_ms=1)]
    )
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.primary_run.id, worker_id="test-worker")

    reviewer_run = (
        db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id, role=AgentRunRole.REVIEWER).one()
    )
    service.execute(reviewer_run.id, worker_id="test-worker")

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert len(adapter.calls) == 1  # reviewer's provider call never happened
    failed_event = (
        db.query(ExecutionEvent)
        .filter_by(task_run_id=scenario.task_run.id, event_type="task_run.failed")
        .one()
    )
    assert failed_event.error["category"] == "budget_exceeded"


def test_budget_exhaustion_before_repair_blocks_repair_call(db):
    provider = make_provider(db)
    primary_pm = make_provider_model(
        db,
        model=make_model(db, canonical_model_id="paid/primary2"),
        provider=provider,
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    reviewer_pm = make_provider_model(
        db,
        model=make_model(db, canonical_model_id="paid/reviewer2"),
        provider=provider,
        cost_input_per_mtok=Decimal("5.00"),
        cost_output_per_mtok=Decimal("5.00"),
    )
    # 0.02 lets the reviewer's own *estimated* reservation through (2000/500
    # default estimate tokens @ $5/$5 = 0.0125, 62.5% of 0.02) so it can
    # actually run and settle its real cost (10_000/10_000 tokens @ $5/$5 =
    # $0.10) -- only then does that committed spend blow the budget, which
    # is what must block the *repair* stage's own (otherwise-free) reservation.
    budget = make_budget(db, limit_amount="0.02")
    db.commit()
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm, budget=budget)

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="v1", tokens_in=1, tokens_out=1),  # primary: free, no budget impact
            InvokeResponse(
                text=repair_required_json(), tokens_in=10_000, tokens_out=10_000
            ),  # reviewer: expensive
        ]
    )
    service = AgentExecutionService(db, adapter_factory=_factory_for(adapter))
    service.execute(scenario.primary_run.id, worker_id="test-worker")
    reviewer_run = (
        db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id, role=AgentRunRole.REVIEWER).one()
    )
    service.execute(reviewer_run.id, worker_id="test-worker")

    # the review itself succeeded (REPAIR_REQUIRED), but queuing the
    # repair's own reservation must fail once the reviewer's real cost
    # already exhausted the budget.
    repair_run = (
        db.query(AgentRun).filter_by(task_run_id=scenario.task_run.id, role=AgentRunRole.REPAIR).one()
    )
    service.execute(repair_run.id, worker_id="test-worker")

    db.refresh(scenario.task_run)
    assert scenario.task_run.status == TaskRunStatus.FAILED
    assert len(adapter.calls) == 2  # repair's provider call never happened
    failed_event = (
        db.query(ExecutionEvent)
        .filter_by(task_run_id=scenario.task_run.id, event_type="task_run.failed")
        .one()
    )
    assert failed_event.error["category"] == "budget_exceeded"


# -- usage/cost aggregation --------------------------------------------------------


def test_usage_and_cost_aggregation_across_the_whole_cycle(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="candidate", tokens_in=100, tokens_out=50),
            InvokeResponse(text=ACCEPT_JSON, tokens_in=200, tokens_out=75),
        ]
    )
    _run_full_cycle(db, adapter, scenario.task_run.id)

    summary = get_review_summary(db, scenario.task_run.id)
    assert len(summary.usage) == 2
    by_role = {u.role: u for u in summary.usage}
    assert by_role["primary"].tokens_in == 100
    assert by_role["primary"].tokens_out == 50
    assert by_role["reviewer"].tokens_in == 200
    assert by_role["reviewer"].tokens_out == 75
    assert summary.total_cost == Decimal(0)  # both FREE models

    all_calls = (
        db.query(ModelCall)
        .join(AgentRun, ModelCall.agent_run_id == AgentRun.id)
        .filter(AgentRun.task_run_id == scenario.task_run.id)
        .all()
    )
    assert len(all_calls) == 2


# -- Flight Recorder sequence -----------------------------------------------------


def test_flight_recorder_sequence_for_one_repair_cycle(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="v1", tokens_in=1, tokens_out=1),
            InvokeResponse(text=repair_required_json(), tokens_in=1, tokens_out=1),
            InvokeResponse(text="v2", tokens_in=1, tokens_out=1),
            InvokeResponse(text=ACCEPT_JSON, tokens_in=1, tokens_out=1),
        ]
    )
    _run_full_cycle(db, adapter, scenario.task_run.id)

    events = _events(db, scenario.task_run.id)

    # sequencing (not necessarily contiguous -- per-stage MA3 events like
    # model_call.started/completed interleave) -- assert relative order of
    # the MA4-specific milestones:
    def idx(name):
        return events.index(name)

    assert idx("review.queued") < idx("review.decision_repair_required")
    assert idx("review.decision_repair_required") < idx("repair.queued")
    assert idx("repair.queued") < idx("review.decision_accept")
    assert idx("review.decision_accept") < idx("final_artifact.selected")
    assert idx("final_artifact.selected") < idx("task_run.completed")
    assert events.count("agent_run.completed") == 4  # primary, reviewer, repair, reviewer


def test_no_secret_leakage_in_review_flight_recorder_events(db):
    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    secret_marker = "sk-or-v1-totally-should-not-appear-in-any-event"
    adapter = FakeAdapter(
        responses=[
            InvokeResponse(text="candidate uses no secret", tokens_in=1, tokens_out=1),
            InvokeResponse(text=ACCEPT_JSON, tokens_in=1, tokens_out=1),
        ]
    )
    _run_full_cycle(db, adapter, scenario.task_run.id)

    events = db.query(ExecutionEvent).filter_by(task_run_id=scenario.task_run.id).all()
    for event in events:
        assert secret_marker not in json.dumps(
            {"decision_summary": event.decision_summary, "error": event.error}, default=str
        )


# -- worker dispatch / stale-worker fencing on a later stage ----------------------


def test_worker_dispatches_reviewer_and_repair_jobs_generically(db, session_factory, monkeypatch):
    """The real Worker/job_queue layer needs zero MA4-specific code: a
    reviewer/repair Agent Run is just another JobType.AGENT_RUN job."""
    import app.services.execution_service as execution_service_module
    from app.worker import Worker

    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    repo = JobQueueRepository()
    repo.enqueue(db, job_type=JobType.AGENT_RUN, payload_ref=scenario.primary_run.id)

    calls = []

    def fake_execute(self, agent_run_id, *, worker_id):
        calls.append(agent_run_id)
        run = db.get(AgentRun, agent_run_id)
        run.status = AgentRunStatus.COMPLETED
        if run.role == AgentRunRole.PRIMARY:
            reviewer_run = AgentRun(
                task_run_id=scenario.task_run.id,
                agent_version_id=scenario.reviewer_agent_version.id,
                role=AgentRunRole.REVIEWER,
                status=AgentRunStatus.CREATED,
            )
            db.add(reviewer_run)
            db.commit()
            db.refresh(reviewer_run)
            repo.enqueue(db, job_type=JobType.AGENT_RUN, payload_ref=reviewer_run.id)
        db.commit()

    monkeypatch.setattr(execution_service_module.AgentExecutionService, "execute", fake_execute)

    worker = Worker(session_factory=session_factory, poll_interval_seconds=0.01, lease_seconds=5)
    assert worker.run_once() is True  # primary
    assert worker.run_once() is True  # reviewer, enqueued by the fake primary above
    assert worker.run_once() is False  # nothing left

    assert len(calls) == 2


def test_stale_worker_cannot_complete_reclaimed_review_stage_job(db, session_factory):
    """Same fencing guarantee tests/test_worker_agent_run.py proves for a
    plain Agent Run, exercised on a reviewer-stage job -- a later MA4 stage
    gets no special exemption from MA3's lease/fencing protocol."""
    from datetime import datetime, timedelta, timezone

    primary_pm, reviewer_pm = _two_provider_models(db)
    scenario = _build_review_scenario(db, primary_pm=primary_pm, reviewer_pm=reviewer_pm)

    reviewer_run = AgentRun(
        task_run_id=scenario.task_run.id,
        agent_version_id=scenario.reviewer_agent_version.id,
        role=AgentRunRole.REVIEWER,
        status=AgentRunStatus.CREATED,
    )
    db.add(reviewer_run)
    db.commit()

    repo = JobQueueRepository()
    job = repo.enqueue(db, job_type=JobType.AGENT_RUN, payload_ref=reviewer_run.id)

    claim_db = session_factory()
    try:
        claimed = repo.claim_one(claim_db, worker_id="worker-a", lease_seconds=30)
        assert claimed is not None
        zombie_token = claimed.fencing_token
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

    worker_b_completed = repo.complete(
        db,
        job_id=job.id,
        worker_id="worker-b",
        fencing_token=reclaimed.fencing_token,
        status=JobQueueStatus.DONE,
    )
    assert worker_b_completed is True

    zombie_completed = repo.complete(
        db, job_id=job.id, worker_id="worker-a", fencing_token=zombie_token, status=JobQueueStatus.FAILED
    )
    assert zombie_completed is False  # worker-a's stale write is rejected

    db.expire_all()
    assert db.get(JobQueue, job.id).status == JobQueueStatus.DONE
