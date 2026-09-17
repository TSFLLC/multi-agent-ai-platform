"""MA3 operator smoke test — a real, live OpenRouter execution.

THIS SCRIPT MAKES A REAL EXTERNAL NETWORK REQUEST TO OPENROUTER.AI AND MAY
CONSUME A REAL PROVIDER QUOTA. It exists to give an operator a way to prove,
on demand, that the full AgentExecutionService pipeline (model resolution ->
budget governor -> prompt assembly -> real provider invocation -> usage/cost
recording -> artifact creation -> terminal state transitions) works against
the live OpenRouter API, not just against the deterministic pytest suite's
fake ProviderAdapter (see tests/test_execution_service.py).

This is explicit-operator-only tooling:
  - it is never collected by pytest (pytest's testpaths is "tests"; this
    file also defines no test_* functions, so it would not be collected
    even if that changed);
  - nothing in app/ imports or calls it — it never runs at API startup;
  - it is not wired into any CI workflow (none exists in this repo yet;
    if one is added later, it must not invoke this script by default);
  - it only runs when an operator explicitly executes it and supplies
    OPENROUTER_API_KEY.

Data safety:
  - uses a throwaway temporary SQLite database and a throwaway temporary
    artifacts directory, created fresh per run and removed afterwards —
    never data/multi_agent_platform.db or data/artifacts/;
  - stores the API key through the real platform secret boundary
    (app.services.secret_service.SecretService -> whatever
    app.secrets_store.get_secret_store() resolves to at runtime, the same
    OS-native credential store a real Provider credential would use), then
    deletes that secret reference again in a ``finally`` block so nothing
    from this smoke run lingers in the OS credential store;
  - the raw key is never printed, logged, or written to disk by this
    script at any point.

Model selection:
  - always re-fetches the current OpenRouter catalog (never a hard-coded
    model id);
  - FREE/PAID/UNKNOWN classification always comes from
    app.domain.pricing.classify_pricing over the provider's own pricing
    metadata for that model, right now — never guessed;
  - among models the catalog currently classifies as FREE, this script
    prefers one that also carries OpenRouter's documented ":free" naming
    convention (a named tier of a known, stable model) over an untagged/
    "stealth" entry, purely as a smoke-test preference for a more
    reproducible choice — pricing metadata remains the sole source of
    truth for the FREE classification itself, ":free" naming is never
    used to classify a model as free on its own.

Usage:
    OPENROUTER_API_KEY=sk-or-... python scripts/smoke_openrouter_execution.py

Exit code is 0 only if the run reached AgentRun=COMPLETED, TaskRun=COMPLETED,
ModelCall=SUCCESS, and produced an artifact. Any other outcome, or any
unhandled error, exits nonzero.
"""

import os
import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from typing import List, Optional

from sqlalchemy.orm import sessionmaker

from app.bootstrap import ensure_local_bootstrap
from app.config import settings
from app.db.base import Base
from app.db.enums import (
    ExecutionMode,
    ModelCallStatus,
    ModelSelectionMode,
    ModelStatus,
    ProviderType,
    TaskRunStatus,
    TaskStatus,
)
from app.db.session import build_engine
from app.domain.pricing import classify_pricing
from app.models.agents import Agent, AgentVersion
from app.models.artifacts_eval import Artifact
from app.models.execution import ModelCall
from app.models.providers import Model, Provider, ProviderModel
from app.models.tasks import AgentRun, Task, TaskRun
from app.providers.openrouter import ModelDescriptor, OpenRouterAdapter
from app.services.execution_service import AgentExecutionService
from app.services.secret_service import SecretService


def select_free_model(descriptors: List[ModelDescriptor]) -> Optional[ModelDescriptor]:
    """Pricing metadata (classify_pricing) is the only thing that decides
    FREE eligibility; ":free" naming only breaks ties among already-FREE
    candidates, never substitutes for a real pricing check."""
    free_descriptors = [
        d
        for d in descriptors
        if classify_pricing(d.cost_input_per_mtok, d.cost_output_per_mtok).value == "free"
    ]
    if not free_descriptors:
        return None
    named_free = [d for d in free_descriptors if d.provider_model_id.endswith(":free")]
    return named_free[0] if named_free else free_descriptors[0]


def main() -> int:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is not set in the environment. Aborting.")
        return 1

    smoke_dir = Path(tempfile.mkdtemp(prefix="ma3_smoke_"))
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

        free_descriptor = select_free_model(descriptors)
        if free_descriptor is None:
            print("No currently FREE model is available in the live OpenRouter catalog. Aborting.")
            return 1
        print(f"Selected model: {free_descriptor.provider_model_id!r} (classification=FREE)")

        provider = Provider(type=ProviderType.OPENROUTER, name="OpenRouter (smoke test)")
        db.add(provider)
        db.flush()

        model = Model(
            canonical_model_id=free_descriptor.provider_model_id,
            context_window=free_descriptor.context_window,
            status=ModelStatus.ACTIVE,
        )
        db.add(model)
        db.flush()

        provider_model = ProviderModel(
            model_id=model.id,
            provider_id=provider.id,
            provider_model_id=free_descriptor.provider_model_id,
            cost_input_per_mtok=Decimal(0),
            cost_output_per_mtok=Decimal(0),
        )
        db.add(provider_model)
        db.flush()

        # Real secret boundary: whatever app.secrets_store.get_secret_store()
        # resolves to at runtime (KeyringSecretStore in a normal install) —
        # removed again in the finally block below.
        secret_row = SecretService(db).store_secret(
            project_id=project.id,
            provider_id=provider.id,
            name="openrouter-smoke-test-key",
            value=api_key,
        )
        secret_ref_id = secret_row.id

        agent = Agent(project_id=project.id, name="Smoke Test Agent", role="engineer")
        db.add(agent)
        db.flush()

        agent_version = AgentVersion(
            agent_id=agent.id,
            version=1,
            name=agent.name,
            role=agent.role,
            model_policy={
                "mode": ModelSelectionMode.MANUAL.value,
                "manual_provider_model_id": provider_model.id,
            },
        )
        db.add(agent_version)
        db.flush()

        task = Task(
            project_id=project.id,
            title="MA3 live smoke test: reply with the single word OK.",
            execution_mode=ExecutionMode.SINGLE_AGENT,
            status=TaskStatus.READY,
        )
        db.add(task)
        db.flush()

        task_run = TaskRun(task_id=task.id, status=TaskRunStatus.CREATED)
        db.add(task_run)
        db.flush()

        agent_run = AgentRun(task_run_id=task_run.id, agent_version_id=agent_version.id)
        db.add(agent_run)
        db.commit()
        db.refresh(agent_run)

        print("Invoking AgentExecutionService against live OpenRouter (real request)...")
        service = AgentExecutionService(db)
        service.execute(agent_run.id, worker_id="smoke-test-operator")

        db.expire_all()
        refetched_agent_run = db.get(AgentRun, agent_run.id)
        refetched_task_run = db.get(TaskRun, task_run.id)
        assert refetched_agent_run is not None
        assert refetched_task_run is not None
        agent_run, task_run = refetched_agent_run, refetched_task_run
        model_calls = (
            db.query(ModelCall)
            .filter(ModelCall.agent_run_id == agent_run.id)
            .order_by(ModelCall.started_at)
            .all()
        )
        artifacts = db.query(Artifact).filter(Artifact.agent_run_id == agent_run.id).all()

        model_call = model_calls[-1] if model_calls else None
        cost_status = "n/a"
        if model_call is not None:
            cost_status = "verified" if model_call.cost_is_estimated is False else "estimated"

        print("\n--- MA3 live smoke test report ---")
        print(f"model used:           {free_descriptor.provider_model_id}")
        print("pricing classification: FREE")
        print(f"TaskRun status:        {task_run.status.value}")
        print(f"AgentRun status:       {agent_run.status.value}")
        print(f"ModelCall status:      {model_call.status.value if model_call else 'none'}")
        print(
            f"tokens_in / tokens_out: {model_call.tokens_in if model_call else None} / "
            f"{model_call.tokens_out if model_call else None}"
        )
        print(f"latency_ms:            {model_call.latency_ms if model_call else None}")
        print(f"cost_amount:           {model_call.cost_amount if model_call else None} ({cost_status})")
        if artifacts:
            content = Path(artifacts[0].storage_ref).read_text(encoding="utf-8")
            print(
                f"artifact:              type={artifacts[0].type.value} "
                f"size_bytes={artifacts[0].size_bytes} content={content[:200]!r}"
            )
        else:
            print("artifact:              none")

        ok = (
            agent_run.status.value == "completed"
            and task_run.status.value == "completed"
            and model_call is not None
            and model_call.status == ModelCallStatus.SUCCESS
            and bool(artifacts)
        )
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
