"""Staging provisioning script — MA7.7B.

Bootstraps a freshly-deployed, freshly-migrated hosted instance's
Provider/Model catalog through the application's own HTTP API — never by
writing rows directly (Owner instruction: "prepare/configure using normal
application/service/API contracts, never direct database manipulation").
Idempotent: safe to re-run against an instance that has already been
provisioned.

What this script does:
  1. Confirms the instance is reachable and ready (GET /health, /ready).
  2. Ensures exactly one OpenRouter Provider row exists, creating it (with
     the API key, if this is the first run) via POST /providers.
  3. Refreshes that provider's model catalog via POST /models/refresh.

What this script deliberately does NOT do (left as guided manual/API
steps — see docs/deployment/railway-staging.md): create an Evaluation
Definition or a flagship Workflow. Those need per-environment judgment
about which starter Agents/models to wire together (exactly the choice
the MA7.6B UAT-prep task made by hand for the local instance) — scripting
that blindly would silently pick the same models for staging with no
review, which is a worse outcome than a short guided checklist.

Usage::

    STAGING_API_BASE_URL=https://<railway-url> \\
    MAP_AUTH_TOKEN=<the hosted bearer token> \\
    STAGING_OPENROUTER_API_KEY=<an OpenRouter key, only on first run> \\
    python -m scripts.provision_staging

The OpenRouter key is read once from the environment and sent only in the
POST /providers request body over HTTPS — never logged, never printed,
never written to a file by this script.
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("scripts.provision_staging")

_OPENROUTER_PROVIDER_TYPE = "openrouter"
_OPENROUTER_PROVIDER_NAME = "OpenRouter"


class ProvisioningError(RuntimeError):
    pass


def _client(base_url: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0)


def _require_ready(client: httpx.Client) -> None:
    health = client.get("/health")
    if health.status_code != 200:
        raise ProvisioningError(f"/health returned {health.status_code}, expected 200 -- is the instance up?")

    ready = client.get("/ready")
    body = ready.json() if ready.headers.get("content-type", "").startswith("application/json") else {}
    if ready.status_code != 200 or not body.get("ready"):
        raise ProvisioningError(
            f"/ready reports not-ready (status={ready.status_code}, body={body}) -- "
            "run the migration step before provisioning."
        )
    logger.info("instance_ready revision=%s", body.get("current_revision"))


def _find_openrouter_provider(client: httpx.Client) -> Optional[Dict[str, Any]]:
    resp = client.get("/providers")
    resp.raise_for_status()
    providers: List[Dict[str, Any]] = resp.json()
    for provider in providers:
        if provider.get("type") == _OPENROUTER_PROVIDER_TYPE:
            return provider
    return None


def _create_openrouter_provider(client: httpx.Client, *, api_key: Optional[str]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"type": _OPENROUTER_PROVIDER_TYPE, "name": _OPENROUTER_PROVIDER_NAME}
    if api_key:
        body["api_key"] = api_key
    resp = client.post("/providers", json=body)
    if resp.status_code != 201:
        raise ProvisioningError(f"POST /providers failed: {resp.status_code} {resp.text}")
    return resp.json()


def _refresh_catalog(client: httpx.Client, *, provider_id: str) -> Dict[str, Any]:
    resp = client.post("/models/refresh", params={"provider_id": provider_id})
    if resp.status_code != 202:
        raise ProvisioningError(f"POST /models/refresh failed: {resp.status_code} {resp.text}")
    return resp.json()


def provision(base_url: str, token: str, *, openrouter_api_key: Optional[str] = None) -> Dict[str, Any]:
    with _client(base_url, token) as client:
        _require_ready(client)

        provider = _find_openrouter_provider(client)
        if provider is None:
            if not openrouter_api_key:
                raise ProvisioningError(
                    "No OpenRouter provider exists yet and STAGING_OPENROUTER_API_KEY was not set -- "
                    "an API key is required on first provisioning."
                )
            provider = _create_openrouter_provider(client, api_key=openrouter_api_key)
            logger.info("provider_created id=%s", provider["id"])
        else:
            logger.info("provider_already_exists id=%s -- skipping creation", provider["id"])
            if openrouter_api_key:
                logger.warning(
                    "openrouter_api_key_ignored provider_id=%s -- an OpenRouter provider already exists "
                    "and this script has no rotate-key step (POST /providers only creates); "
                    "rotate a key by hand via the application's provider management, not this script.",
                    provider["id"],
                )

        refresh = _refresh_catalog(client, provider_id=provider["id"])
        logger.info(
            "catalog_refresh_triggered provider_id=%s refresh_id=%s status=%s",
            provider["id"],
            refresh.get("id"),
            refresh.get("status"),
        )
        return {"provider": provider, "refresh": refresh}


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    base_url = os.environ.get("STAGING_API_BASE_URL")
    token = os.environ.get("MAP_AUTH_TOKEN")
    api_key = os.environ.get("STAGING_OPENROUTER_API_KEY")

    if not base_url or not token:
        print(
            "STAGING_API_BASE_URL and MAP_AUTH_TOKEN must both be set in the environment.\n"
            "See the module docstring (python -m pydoc scripts.provision_staging) for usage.",
            file=sys.stderr,
        )
        return 2

    try:
        provision(base_url, token, openrouter_api_key=api_key)
    except (ProvisioningError, httpx.HTTPError) as exc:
        logger.error("provisioning_failed error=%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
