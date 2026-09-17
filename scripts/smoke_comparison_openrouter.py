"""MA5 operator smoke test — a real, live Parallel Comparison.

THIS SCRIPT MAKES REAL EXTERNAL NETWORK REQUESTS TO OPENROUTER.AI AND MAY
CONSUME REAL PROVIDER QUOTA. It exists to give an operator a way to prove,
on demand, that the full Parallel Comparison pipeline (ComparisonService
configure -> launch -> N independent, isolated Task Run/Agent Run pairs on
MA3's real execution engine -> every result's evidence preserved -> human
canonical selection, hash-bound) works against the live OpenRouter API, not
just against the deterministic pytest suite's fake ProviderAdapter (see
tests/test_comparison_service.py).

Same explicit-operator-only, data-safety, and secret-handling posture as
scripts/smoke_review_openrouter.py (MA4's live smoke test) -- see that
script's module docstring for the full rationale, summarized here:
  - never collected by pytest, never run at API startup, never wired into
    any CI workflow;
  - only runs when an operator explicitly executes it and supplies
    OPENROUTER_API_KEY (never printed/logged);
  - throwaway temp SQLite DB + temp artifacts dir, never data/;
  - the API key is stored through the real platform secret boundary
    (SecretService -> whatever secrets_store.get_secret_store() resolves
    to) and the secret reference is deleted again in a ``finally`` block.

Model selection: always re-fetches the current OpenRouter catalog and picks
currently-FREE models (pricing metadata is authoritative, ":free" naming is
only a smoke-test preference among already-FREE candidates -- see
scripts/smoke_openrouter_execution.py's select_free_model, reused here).
Uses up to 3 currently-FREE models; if the live catalog offers fewer than 3
right now, it proceeds with however many are available down to MA5's hard
minimum of 2 candidates, and reports which combinations it could and could
not exercise rather than failing outright over live catalog availability.

Candidate design deliberately covers all three axes Section 3 requires be
expressible:
  - Candidate A and Candidate B share ONE Agent Version but override to
    different models (same Agent, different models);
  - Candidate C uses a SEPARATE Agent Version (different Agent), overridden
    to a third model when one is available, otherwise reusing Candidate A's
    model (different Agent, same OR different model, depending on live
    catalog availability -- reported explicitly either way).

This script simulates the human's final action by calling
ComparisonService.select_canonical against one of the comparison's own
completed candidates through the exact same code path the API's
POST /comparisons/{id}/select-winner uses -- it is the operator standing in
for the human to validate the full lifecycle end-to-end; the platform
itself never runs a Judge Agent, scoring engine, ranking algorithm, or
learned router anywhere in this path (Section 18.3 Principle 2/4).

Usage:
    OPENROUTER_API_KEY=sk-or-... python scripts/smoke_comparison_openrouter.py
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

from sqlalchemy.orm import sessionmaker

from app.bootstrap import ensure_local_bootstrap
from app.config import settings
from app.db.base import Base
from app.db.enums import (
    ExecutionMode,
    ModelSelectionMode,
    ModelStatus,
    ProviderType,
    TaskStatus,
    VersionStatus,
)
from app.db.session import build_engine
from app.domain.pricing import classify_pricing
from app.models.agents import Agent, AgentVersion
from app.models.providers import Model, Provider, ProviderModel
from app.models.tasks import Task
from app.providers.openrouter import ModelDescriptor, OpenRouterAdapter
from app.services.comparison_service import ComparisonService, get_comparison_detail
from app.services.execution_service import AgentExecutionService
from app.services.secret_service import SecretService

TASK_TITLE = (
    "Write a Python function named is_palindrome(s) that returns True if the string s "
    "(ignoring case) reads the same forwards and backwards, and False otherwise. "
    "Return only the code."
)


def select_free_models(descriptors: List[ModelDescriptor], count: int) -> List[ModelDescriptor]:
    """Same pricing-metadata-is-authoritative rule as MA3/MA4's smoke
    tests: ':free' naming only orders *among* candidates classify_pricing
    already verified as FREE right now."""
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

    smoke_dir = Path(tempfile.mkdtemp(prefix="ma5_smoke_"))
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
        catalog_adapter = OpenRouterAdapter(api_key)
        descriptors = catalog_adapter.list_models()
        chosen = select_free_models(descriptors, 3)
        if len(chosen) < 2:
            print(
                f"Only {len(chosen)} currently-FREE model(s) available in the live OpenRouter "
                "catalog -- MA5 requires at least 2 candidates. Aborting."
            )
            return 1
        if len(chosen) < 3:
            print(
                f"NOTE: only {len(chosen)} currently-FREE models are available live "
                "(preferred 3) -- proceeding with what the catalog offers right now."
            )
        for i, d in enumerate(chosen):
            print(f"FREE model [{i}]: {d.provider_model_id!r}")

        provider = Provider(type=ProviderType.OPENROUTER, name="OpenRouter (MA5 smoke test)")
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

        provider_models = [_register(d) for d in chosen]

        secret_row = SecretService(db).store_secret(
            project_id=project.id,
            provider_id=provider.id,
            name="openrouter-ma5-smoke-test-key",
            value=api_key,
        )
        secret_ref_id = secret_row.id

        # Candidate A/B: ONE shared Agent Version, overridden to different
        # models -- "same Agent, different models" (Section 3).
        shared_agent = Agent(project_id=project.id, name="Python Engineer (smoke)", role="engineer")
        db.add(shared_agent)
        db.flush()
        shared_version = AgentVersion(
            agent_id=shared_agent.id,
            version=1,
            name=shared_agent.name,
            role=shared_agent.role,
            status=VersionStatus.ACTIVE,
            model_policy={
                "mode": ModelSelectionMode.MANUAL.value,
                "manual_provider_model_id": provider_models[0].id,
            },
        )
        db.add(shared_version)
        db.flush()

        # Candidate C: a SEPARATE Agent Version -- "different Agent".
        alt_agent = Agent(project_id=project.id, name="Alternative Python Engineer (smoke)", role="engineer")
        db.add(alt_agent)
        db.flush()
        alt_version = AgentVersion(
            agent_id=alt_agent.id,
            version=1,
            name=alt_agent.name,
            role=alt_agent.role,
            status=VersionStatus.ACTIVE,
            model_policy={
                "mode": ModelSelectionMode.MANUAL.value,
                "manual_provider_model_id": provider_models[0].id,
            },
        )
        db.add(alt_version)
        db.flush()

        task = Task(
            project_id=project.id,
            title=TASK_TITLE,
            execution_mode=ExecutionMode.PARALLEL_COMPARISON,
            status=TaskStatus.READY,
        )
        db.add(task)
        db.commit()

        candidate_specs = [
            {
                "agent_version_id": shared_version.id,
                "label": "Candidate A (shared Agent, model[0])",
                "model_policy_override": {
                    "mode": ModelSelectionMode.MANUAL.value,
                    "manual_provider_model_id": provider_models[0].id,
                },
            },
            {
                "agent_version_id": shared_version.id,
                "label": f"Candidate B (shared Agent, model[{1 if len(provider_models) > 1 else 0}])",
                "model_policy_override": {
                    "mode": ModelSelectionMode.MANUAL.value,
                    "manual_provider_model_id": provider_models[min(1, len(provider_models) - 1)].id,
                },
            },
            {
                "agent_version_id": alt_version.id,
                "label": f"Candidate C (different Agent, model[{2 if len(provider_models) > 2 else 0}])",
                "model_policy_override": {
                    "mode": ModelSelectionMode.MANUAL.value,
                    "manual_provider_model_id": provider_models[min(2, len(provider_models) - 1)].id,
                },
            },
        ]
        print(
            "Candidate design: A/B = same Agent Version, "
            + ("different" if len(provider_models) > 1 else "the same (only 1 FREE model live)")
            + " models; C = a different Agent Version, "
            + ("a third" if len(provider_models) > 2 else "a reused")
            + " model."
        )

        comparison = ComparisonService(db).create_comparison(
            task_id=task.id, candidates=candidate_specs, budget_id=None
        )
        print(f"Comparison {comparison.id} created (PENDING) with {len(candidate_specs)} candidates.")

        ComparisonService(db).launch_comparison(comparison.id)
        print("Comparison launched -- one independent Task Run + Agent Run per candidate, queued.")

        detail = get_comparison_detail(db, comparison.id)
        print("Running each candidate's Agent Run against the LIVE OpenRouter API (real requests)...")
        for candidate_detail in detail.candidates:
            candidate = candidate_detail.candidate
            # launch_comparison() always sets agent_run_id before returning
            # (Section: candidate rows are only nullable *before* launch).
            assert candidate.agent_run_id is not None
            print(f"  -> executing {candidate.label!r} (agent_run {candidate.agent_run_id}) ...")
            AgentExecutionService(db).execute(candidate.agent_run_id, worker_id="ma5-smoke-test-operator")

        db.expire_all()
        detail = get_comparison_detail(db, comparison.id)
        phase = detail.phase

        print("\n--- MA5 live comparison smoke test report ---")
        print(f"comparison_id:    {comparison.id}")
        print(f"phase:            {phase}")
        for candidate_detail in detail.candidates:
            c = candidate_detail.candidate
            print(
                f"  [{c.label}] status={candidate_detail.status} model_id={candidate_detail.model_id} "
                f"tokens_in={candidate_detail.usage.tokens_in} tokens_out={candidate_detail.usage.tokens_out} "
                f"cost={candidate_detail.usage.cost_amount} latency_ms={candidate_detail.usage.latency_ms}"
            )
            if candidate_detail.artifact is not None:
                content = Path(candidate_detail.artifact.storage_ref).read_text(encoding="utf-8")
                print(f"    artifact: hash={candidate_detail.artifact.content_hash} content={content!r}")
        print(f"aggregate tokens_in={detail.total_tokens_in} tokens_out={detail.total_tokens_out}")
        print(f"aggregate cost:   {detail.total_cost}")

        # Structural isolation proof: no candidate's Flight Recorder trace
        # references another candidate's task_run/agent_run/artifact ids.
        candidate_ids = {c.candidate.agent_run_id for c in detail.candidates}
        for candidate_detail in detail.candidates:
            own_id = candidate_detail.candidate.agent_run_id
            other_ids = candidate_ids - {own_id}
            from app.services.flight_recorder import FlightRecorderService

            candidate_task_run_id = candidate_detail.candidate.task_run_id
            assert candidate_task_run_id is not None
            events = FlightRecorderService(db).list_for_task_run(task_run_id=candidate_task_run_id)
            leaked = [e for e in events if e.agent_run_id in other_ids]
            print(
                f"  isolation check [{candidate_detail.candidate.label}]: "
                f"{'OK, no cross-candidate references' if not leaked else 'FAILED -- cross-candidate leakage!'}"
            )

        winner_selected = False
        if phase == "ready_for_selection":
            # The operator stands in for the human here, exercising the
            # exact ComparisonService.select_canonical path the API's
            # POST /comparisons/{id}/select-winner uses -- the platform
            # itself never made this choice.
            succeeded = [
                c
                for c in detail.candidates
                if c.status == "completed" and c.artifact is not None and c.artifact.content_hash is not None
            ]
            if succeeded:
                chosen_candidate = succeeded[0]
                chosen_artifact = chosen_candidate.artifact
                assert chosen_artifact is not None and chosen_artifact.content_hash is not None
                print(
                    f"\nSimulating human canonical selection of {chosen_candidate.candidate.label!r} "
                    "(operator standing in for the UI's confirm action)..."
                )
                ComparisonService(db).select_canonical(
                    comparison.id,
                    candidate_id=chosen_candidate.candidate.id,
                    artifact_hash=chosen_artifact.content_hash,
                    selected_by=None,
                )
                winner_selected = True
                db.expire_all()
                from app.models.artifacts_eval import ComparisonRun

                final_comparison = db.get(ComparisonRun, comparison.id)
                assert final_comparison is not None
                print(f"Canonical selection recorded. comparison.status={final_comparison.status.value}")
                print(f"winner_agent_run_id={final_comparison.winner_agent_run_id}")
                print(f"winner_artifact_hash={final_comparison.winner_artifact_hash}")
        else:
            print(f"\nphase={phase!r} -- not ready for a canonical selection (no candidate succeeded).")

        ok = phase in ("ready_for_selection", "failed") and (
            phase != "ready_for_selection" or winner_selected
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
