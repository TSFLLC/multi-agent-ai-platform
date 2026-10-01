"""AIL5D.6 browser-UAT harness (LOCAL / DEV ONLY — never deployed, never run against a real database).

Starts the real application against a DISPOSABLE database in a temp directory, with:

* the real migration chain applied (``alembic upgrade head``);
* the real Worker running in a thread, so a practice run is a genuine queued Task Run / Agent Run processed by the real
  execution service;
* ONLY the model provider replaced by a deterministic local stand-in (``StandInAdapter``) — there is no model API key in a
  UAT sandbox, and a canned reply is what makes the UAT repeatable. Lab runs get a short canned answer; the AI Professor gets
  a canned structured reply. Everything between the browser and the adapter is the production code path;
* Level 1 provisioned, and the reference Days authored as structured Days.

    python scripts/ail5d6_uat_server.py --port 8765 [--days 1,4,5,9]

It prints the URL. Ctrl+C stops it and the temp database is discarded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--days", default="1,4,5,9", help="comma-separated Days to author as structured Days")
    args = parser.parse_args()

    data_root = Path(tempfile.mkdtemp(prefix="ail5d6-uat-"))
    os.environ["MAP_DATA_ROOT"] = str(data_root)
    os.environ.setdefault("MAP_BACKUP_ON_STARTUP", "false")

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")

    from app.config import settings
    from app.db.enums import ProviderType
    from app.db.session import SessionLocal
    from app.models.providers import Model, Provider, ProviderModel
    from app.providers.base import InvokeResponse
    from app.services.agent_registry_service import AgentRegistryService  # noqa: F401  (import side effects: models registered)

    class StandInAdapter:
        """Deterministic local stand-in for the model provider (the only replaced component)."""

        def invoke(self, request):
            prompt = request.user_prompt or ""
            if request.response_format is not None or "Professor" in (request.system_prompt or ""):
                body = {
                    "intent": "EXPLAIN_THIS", "direct_answer": "Start from what this step is asking, then check it against what you can already see on the page.",
                    "explanation": "(Stand-in Professor for UAT.) Look for the rule that decides the answer before choosing.",
                    "evidence": [], "grounded_assertions": [{"assertion_kind": "ai_explanation", "text": "Start from what this step is asking.", "references": []}],
                    "uncertainties": [], "suggested_next_actions": [], "attachment_references": [],
                }
                return InvokeResponse(text=json.dumps(body), tokens_in=120, tokens_out=60, tokens_total=180, latency_ms=5, provider_http_status=200, finish_reason="stop")
            if "ONLY the information in this document" in prompt:
                text = "I cannot answer this from the provided document." if "parking" in prompt else "The museum opened in 1968, according to the document."
            elif "(No document is provided.)" in prompt:
                text = "I believe it opened around 1970, but I'm not certain."
            elif "Review:" in prompt:
                text = "mixed"
            else:
                text = "Stand-in answer: a short, deterministic reply used for UAT."
            time.sleep(1.2)
            return InvokeResponse(text=text, tokens_in=40, tokens_out=20, tokens_total=60, latency_ms=1200, provider_http_status=200, finish_reason="stop")

    from app.services import execution_service

    execution_service.AgentExecutionService.__init__.__defaults__ = (lambda _db, _provider: StandInAdapter(),)

    # The disposable database needs one reachable (free) model for AUTO routing.
    db = SessionLocal()
    try:
        provider = Provider(type=ProviderType.OPENROUTER, name="UAT stand-in")
        db.add(provider)
        db.flush()
        model = Model(canonical_model_id="uat/stand-in", structured_output_support=True)
        db.add(model)
        db.flush()
        db.add(ProviderModel(model_id=model.id, provider_id=provider.id, provider_model_id="uat/stand-in", cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0)))
        db.commit()
    finally:
        db.close()

    from app.main import app
    import uvicorn

    from fastapi.testclient import TestClient  # noqa: F401

    def seed() -> None:
        """Provision Level 1 and author the requested Days, through the same services the API uses."""
        from app.db.session import SessionLocal as S
        from app.services.academy_level1_service import AcademyLevel1Service
        from app.services.academy_structured_authoring import author_day_structure
        from app.bootstrap import ensure_local_bootstrap

        s = S()
        try:
            identities = ensure_local_bootstrap(s)
            service = AcademyLevel1Service(s)
            from app.services.practical_ai_foundations_provisioning import provision_practical_ai_foundations

            provision_practical_ai_foundations(s)
            service.provision(identities.user)
            for day in [int(x) for x in args.days.split(",") if x.strip()]:
                author_day_structure(s, day)
        finally:
            s.close()

    from app.worker import Worker

    worker = Worker(concurrency=1)
    threading.Thread(target=worker.run_forever, daemon=True).start()
    print(f"\nAIL5D.6 UAT server (disposable DB at {settings.database_path})  ->  http://127.0.0.1:{args.port}/\n", flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=lambda: (time.sleep(3), seed()), daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
