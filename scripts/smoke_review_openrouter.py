"""MA4 operator smoke test — a real, live Agent-to-Agent Review cycle.

THIS SCRIPT MAKES REAL EXTERNAL NETWORK REQUESTS TO OPENROUTER.AI AND MAY
CONSUME REAL PROVIDER QUOTA. It exists to give an operator a way to prove,
on demand, that the full Agent-to-Agent Review pipeline (primary Agent ->
candidate artifact -> reviewer Agent -> structured ACCEPT/REPAIR_REQUIRED
decision -> optional bounded repair -> final artifact) works against the
live OpenRouter API, not just against the deterministic pytest suite's fake
ProviderAdapter (see tests/test_review_orchestration_service.py).

Same explicit-operator-only, data-safety, and secret-handling posture as
scripts/smoke_openrouter_execution.py (MA3's live smoke test) -- see that
script's module docstring for the full rationale, summarized here:
  - never collected by pytest, never run at API startup, never wired into
    any CI workflow;
  - only runs when an operator explicitly executes it and supplies
    OPENROUTER_API_KEY (never printed/logged);
  - throwaway temp SQLite DB + temp artifacts dir, never data/;
  - the API key is stored through the real platform secret boundary
    (SecretService -> whatever secrets_store.get_secret_store() resolves
    to) and the secret reference is deleted again in a ``finally`` block.

Model selection: always re-fetches the current OpenRouter catalog and
picks currently-FREE models (pricing metadata is authoritative, ":free"
naming is only a smoke-test preference among already-FREE candidates --
see scripts/smoke_openrouter_execution.py's select_free_model, reused
here). Primary and reviewer are given independent Agent Versions and,
where the live catalog offers more than one FREE model, independent
models too -- proving Agent != Model live, not just in the deterministic
suite.

This script does not force a repair cycle -- it reports whatever the live
reviewer actually decided. Forcing REPAIR_REQUIRED deterministically (to
prove that specific path) is covered offline in
tests/test_review_orchestration_service.py with a fake adapter, per the
Owner instruction not to deliberately waste live inference just to
exercise repair.

Usage:
    OPENROUTER_API_KEY=sk-or-... python scripts/smoke_review_openrouter.py
"""

import os
import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.bootstrap import ensure_local_bootstrap
from app.config import settings
from app.db.base import Base
from app.db.enums import (
    AgentRunRole,
    AgentRunStatus,
    ExecutionMode,
    ModelSelectionMode,
    ModelStatus,
    ProviderType,
    TaskRunStatus,
    TaskStatus,
)
from app.db.session import build_engine
from app.domain.pricing import classify_pricing
from app.models.agents import Agent, AgentVersion
from app.models.execution import ModelCall
from app.models.providers import Model, Provider, ProviderModel
from app.models.tasks import AgentRun, Task, TaskRun
from app.providers.openrouter import ModelDescriptor, OpenRouterAdapter
from app.services.execution_service import AgentExecutionService
from app.services.review_orchestration_service import get_review_summary
from app.services.secret_service import SecretService

TASK_TITLE = (
    "Write a Python function named is_even(n) that returns True when an integer is even "
    "and False otherwise. Return only the code."
)
REVIEW_INSTRUCTIONS = (
    "Check: (1) correctness -- does it actually classify even/odd numbers correctly; "
    "(2) the function is named exactly is_even; (3) it does what was asked, nothing more; "
    "(4) no unnecessary content (explanations, extra functions, tests) was included."
)


def select_free_models(descriptors: List[ModelDescriptor], count: int) -> List[ModelDescriptor]:
    """Same pricing-metadata-is-authoritative rule as MA3's smoke test:
    ':free' naming only orders *among* candidates classify_pricing already
    verified as FREE right now."""
    free_descriptors = [
        d
        for d in descriptors
        if classify_pricing(d.cost_input_per_mtok, d.cost_output_per_mtok).value == "free"
    ]
    named_free = [d for d in free_descriptors if d.provider_model_id.endswith(":free")]
    ordered = named_free + [d for d in free_descriptors if d not in named_free]
    return ordered[:count]


def main() -> int:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is not set in the environment. Aborting.")
        return 1

    smoke_dir = Path(tempfile.mkdtemp(prefix="ma4_smoke_"))
    db_path = smoke_dir / "smoke.db"
    original_artifacts_dir = settings.artifacts_dir
    settings.artifacts_dir = smoke_dir / "artifacts"

    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = session_factory()

    secret_ref_id: Optional[str] = None
    exit_code = 1
    try:
        identities = ensure_local_bootstrap(db)
        db.commit()
        project = identities.project

        print("Fetching the current OpenRouter model catalog (live request)...")
        adapter = OpenRouterAdapter(api_key)
        descriptors = adapter.list_models()
        chosen = select_free_models(descriptors, 2)
        if not chosen:
            print("No currently FREE model is available in the live OpenRouter catalog. Aborting.")
            return 1
        primary_descriptor = chosen[0]
        reviewer_descriptor = chosen[1] if len(chosen) > 1 else chosen[0]
        print(f"Primary model:  {primary_descriptor.provider_model_id!r} (classification=FREE)")
        print(f"Reviewer model: {reviewer_descriptor.provider_model_id!r} (classification=FREE)")

        provider = Provider(type=ProviderType.OPENROUTER, name="OpenRouter (MA4 smoke test)")
        db.add(provider)
        db.flush()

        def _register(descriptor: ModelDescriptor) -> ProviderModel:
            model = Model(
                canonical_model_id=descriptor.provider_model_id,
                context_window=descriptor.context_window,
                status=ModelStatus.ACTIVE,
            )
            db.add(model)
            db.flush()
            pm = ProviderModel(
                model_id=model.id,
                provider_id=provider.id,
                provider_model_id=descriptor.provider_model_id,
                cost_input_per_mtok=Decimal(0),
                cost_output_per_mtok=Decimal(0),
            )
            db.add(pm)
            db.flush()
            return pm

        primary_pm = _register(primary_descriptor)
        reviewer_pm = (
            _register(reviewer_descriptor) if reviewer_descriptor is not primary_descriptor else primary_pm
        )

        secret_row = SecretService(db).store_secret(
            project_id=project.id,
            provider_id=provider.id,
            name="openrouter-ma4-smoke-test-key",
            value=api_key,
        )
        secret_ref_id = secret_row.id

        primary_agent = Agent(project_id=project.id, name="Software Engineer (smoke)", role="engineer")
        reviewer_agent = Agent(project_id=project.id, name="Code Reviewer (smoke)", role="reviewer")
        db.add_all([primary_agent, reviewer_agent])
        db.flush()

        primary_version = AgentVersion(
            agent_id=primary_agent.id,
            version=1,
            name=primary_agent.name,
            role=primary_agent.role,
            model_policy={
                "mode": ModelSelectionMode.MANUAL.value,
                "manual_provider_model_id": primary_pm.id,
            },
        )
        reviewer_version = AgentVersion(
            agent_id=reviewer_agent.id,
            version=1,
            name=reviewer_agent.name,
            role=reviewer_agent.role,
            model_policy={
                "mode": ModelSelectionMode.MANUAL.value,
                "manual_provider_model_id": reviewer_pm.id,
            },
        )
        db.add_all([primary_version, reviewer_version])
        db.flush()

        task = Task(
            project_id=project.id,
            title=TASK_TITLE,
            execution_mode=ExecutionMode.BUILD_REVIEW,
            status=TaskStatus.READY,
        )
        db.add(task)
        db.flush()

        task_run = TaskRun(
            task_id=task.id,
            status=TaskRunStatus.CREATED,
            config_snapshot={
                "primary_agent_version_id": primary_version.id,
                "reviewer_agent_version_id": reviewer_version.id,
                "max_repair_iterations": settings.default_max_repair_iterations,
                "review_instructions": REVIEW_INSTRUCTIONS,
            },
        )
        db.add(task_run)
        db.flush()

        primary_run = AgentRun(
            task_run_id=task_run.id, agent_version_id=primary_version.id, role=AgentRunRole.PRIMARY
        )
        db.add(primary_run)
        db.commit()

        print("Running the live Agent-to-Agent Review cycle (real OpenRouter requests)...")
        service = AgentExecutionService(db)
        stages_run = 0
        for _ in range(10):
            stmt = (
                select(AgentRun)
                .where(AgentRun.task_run_id == task_run.id, AgentRun.status == AgentRunStatus.CREATED)
                .order_by(AgentRun.created_at)
            )
            run = db.execute(stmt).scalars().first()
            if run is None:
                break
            assert run.role is not None  # every BUILD_REVIEW AgentRun created above is tagged
            print(f"  -> executing {run.role.value} agent_run {run.id} ...")
            service.execute(run.id, worker_id="ma4-smoke-test-operator")
            stages_run += 1

        db.expire_all()
        refetched_task_run = db.get(TaskRun, task_run.id)
        assert refetched_task_run is not None
        task_run = refetched_task_run
        summary = get_review_summary(db, task_run.id)
        assert summary is not None  # the task_run we just created is guaranteed to exist

        print("\n--- MA4 live review smoke test report ---")
        print(f"primary Agent:    {primary_agent.name} ({primary_agent.id})")
        print(f"reviewer Agent:   {reviewer_agent.name} ({reviewer_agent.id})")
        print(f"primary model:    {primary_descriptor.provider_model_id}")
        print(f"reviewer model:   {reviewer_descriptor.provider_model_id}")
        print("pricing classifications: primary=FREE reviewer=FREE")
        print(f"stages executed:  {stages_run}")

        repair_count = sum(1 for r in summary.agent_runs if r.role == AgentRunRole.REPAIR)
        print(f"repair count:     {repair_count}")

        for run in summary.agent_runs:
            calls = db.query(ModelCall).filter_by(agent_run_id=run.id).all()
            role_label = run.role.value if run.role else "?"
            for call in calls:
                print(
                    f"  [{role_label}] tokens_in={call.tokens_in} tokens_out={call.tokens_out} "
                    f"latency_ms={call.latency_ms} cost={call.cost_amount} "
                    f"({'verified' if call.cost_is_estimated is False else 'estimated'})"
                )

        reviews = summary.reviews
        if reviews:
            last_review = reviews[-1]
            print(f"final review decision: {last_review.decision.value}")
        else:
            print("final review decision: none recorded")

        if summary.final_artifact is not None:
            content = Path(summary.final_artifact.storage_ref).read_text(encoding="utf-8")
            print(f"final artifact:   size_bytes={summary.final_artifact.size_bytes} content={content!r}")
        else:
            print("final artifact:   none")

        print(f"TaskRun state:    {task_run.status.value} (outcome={summary.outcome})")
        print(f"total cost:       {summary.total_cost}")

        ok = summary.outcome in ("accepted", "repair_limit_exhausted") and stages_run > 0
        print("\nSMOKE TEST " + ("PASSED" if ok else "FAILED"))
        exit_code = 0 if ok else 1
        return exit_code
    finally:
        try:
            if secret_ref_id is not None:
                SecretService(db).delete_secret(secret_ref_id)
        finally:
            db.close()
            engine.dispose()
            settings.artifacts_dir = original_artifacts_dir
            shutil.rmtree(smoke_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
