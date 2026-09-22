"""Starter Agent v2 responsibility upgrade script — MA7.7D.

Publishes v2 of the six original starter Agents' substantive
responsibilities (``app.agent_responsibility_upgrades.AGENT_V2_UPGRADES``)
against a running instance (local or hosted), through the application's
own HTTP API only -- never by writing database rows directly (same Owner
instruction scripts/provision_staging.py already follows). Agent Version
content is immutable once published (Section 12.3): this never touches
v1, it only ever creates and publishes a new v2.

Idempotent: safe to re-run. If an Agent already has a version >= 2, that
Agent is skipped -- v2 is created and published at most once per Agent,
never duplicated on repeated runs.

Deliberately NOT wired into application startup (see
app.starter_agents' module docstring for why) -- publishing a new Agent
Version is an explicit, operator-triggered upgrade, never an implicit
side effect of every restart.

Usage::

    MAP_API_BASE_URL=http://127.0.0.1:8000 \\
    MAP_AUTH_TOKEN=<the bearer token> \\
    python -m scripts.provision_agent_responsibilities

Against Railway staging, set MAP_API_BASE_URL to the staging URL and
MAP_AUTH_TOKEN to that environment's MAP_AUTH_TOKEN value (the same
token used for scripts.provision_staging).
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

from app.agent_responsibility_upgrades import AGENT_V2_UPGRADES, AgentV2Spec

logger = logging.getLogger("scripts.provision_agent_responsibilities")

# app.bootstrap.LOCAL_PROJECT_ID -- V1 is single-project; Agents are
# project-scoped (agents.project_id), so listing them needs this fixed,
# well-known id, the same one app.bootstrap seeds on every startup.
LOCAL_PROJECT_ID = "00000000-0000-0000-0000-000000000003"


class UpgradeError(RuntimeError):
    pass


def _client(base_url: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0)


def _find_agents_by_role(client: httpx.Client) -> Dict[str, Dict[str, Any]]:
    resp = client.get("/agents", params={"project_id": LOCAL_PROJECT_ID})
    resp.raise_for_status()
    agents: List[Dict[str, Any]] = resp.json()
    return {a["role"]: a for a in agents}


def _latest_version_number(client: httpx.Client, agent_id: str) -> int:
    resp = client.get(f"/agents/{agent_id}/versions")
    resp.raise_for_status()
    versions: List[Dict[str, Any]] = resp.json()
    return max((v["version"] for v in versions), default=0)


def _publish_v2(client: httpx.Client, *, agent_id: str, spec: AgentV2Spec) -> Dict[str, Any]:
    prompt_resp = client.post(f"/agents/{agent_id}/prompt-versions", json={"content": spec.prompt})
    if prompt_resp.status_code != 201:
        raise UpgradeError(
            f"POST prompt-versions failed for role={spec.role!r}: {prompt_resp.status_code} {prompt_resp.text}"
        )
    prompt_version_id = prompt_resp.json()["id"]

    version_resp = client.post(
        f"/agents/{agent_id}/versions",
        json={
            "name": spec.name,
            "role": spec.role,
            "description": spec.description,
            "prompt_version_id": prompt_version_id,
        },
    )
    if version_resp.status_code != 201:
        raise UpgradeError(
            f"POST versions failed for role={spec.role!r}: {version_resp.status_code} {version_resp.text}"
        )
    version_number = version_resp.json()["version"]

    publish_resp = client.post(f"/agents/{agent_id}/versions/{version_number}/publish")
    if publish_resp.status_code != 200:
        raise UpgradeError(
            f"POST publish failed for role={spec.role!r}: {publish_resp.status_code} {publish_resp.text}"
        )
    return publish_resp.json()


def provision(base_url: str, token: str) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    with _client(base_url, token) as client:
        agents_by_role = _find_agents_by_role(client)
        for spec in AGENT_V2_UPGRADES:
            agent = agents_by_role.get(spec.role)
            if agent is None:
                raise UpgradeError(
                    f"No existing Agent found with role={spec.role!r} -- starter Agents must "
                    "already be bootstrapped (a normal app startup) before running this upgrade."
                )
            agent_id = agent["id"]
            latest = _latest_version_number(client, agent_id)
            if latest >= 2:
                logger.info(
                    "agent_already_upgraded role=%s agent_id=%s latest_version=%s",
                    spec.role,
                    agent_id,
                    latest,
                )
                results[spec.role] = {"agent_id": agent_id, "skipped": True, "version": latest}
                continue
            published = _publish_v2(client, agent_id=agent_id, spec=spec)
            logger.info(
                "agent_v2_published role=%s agent_id=%s version=%s",
                spec.role,
                agent_id,
                published["version"],
            )
            results[spec.role] = {"agent_id": agent_id, "skipped": False, "version": published["version"]}
    return results


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    base_url = os.environ.get("MAP_API_BASE_URL")
    token = os.environ.get("MAP_AUTH_TOKEN")
    if not base_url or not token:
        print(
            "MAP_API_BASE_URL and MAP_AUTH_TOKEN must both be set in the environment.\n"
            "See the module docstring (python -m pydoc scripts.provision_agent_responsibilities) "
            "for usage.",
            file=sys.stderr,
        )
        return 2

    try:
        provision(base_url, token)
    except (UpgradeError, httpx.HTTPError) as exc:
        logger.error("upgrade_failed error=%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
