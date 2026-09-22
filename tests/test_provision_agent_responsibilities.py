"""scripts.provision_agent_responsibilities -- idempotent v2 upgrade path,
MA7.7D. Uses httpx.MockTransport (same pattern as
test_provision_staging.py) so these run offline, never against a live
instance.
"""

import json

import httpx
import pytest

from app.agent_responsibility_upgrades import AGENT_V2_UPGRADES
from scripts import provision_agent_responsibilities as provision_mod


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="https://instance.example")


_AGENTS = [
    {"id": f"agent-{spec.role}", "role": spec.role, "name": spec.name, "current_status": "active"}
    for spec in AGENT_V2_UPGRADES
]


def test_provision_publishes_v2_for_every_agent_when_only_v1_exists(monkeypatch):
    published_versions = []

    def handler(request):
        path = request.url.path
        if path == "/agents":
            return httpx.Response(200, json=_AGENTS)
        if path.endswith("/versions") and request.method == "GET":
            return httpx.Response(200, json=[{"version": 1}])
        if path.endswith("/prompt-versions") and request.method == "POST":
            return httpx.Response(201, json={"id": f"prompt-{path}"})
        if path.endswith("/versions") and request.method == "POST":
            body = json.loads(request.content)
            assert body["prompt_version_id"].startswith("prompt-")
            return httpx.Response(201, json={"version": 2, "id": "v2-id"})
        if path.endswith("/publish"):
            published_versions.append(path)
            return httpx.Response(200, json={"version": 2, "status": "active"})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(handler))

    results = provision_mod.provision("https://instance.example", "token")

    assert len(results) == 6
    assert all(not r["skipped"] for r in results.values())
    assert all(r["version"] == 2 for r in results.values())
    assert len(published_versions) == 6


def test_provision_skips_agents_already_upgraded(monkeypatch):
    calls = []

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/agents":
            return httpx.Response(200, json=_AGENTS)
        if path.endswith("/versions") and request.method == "GET":
            return httpx.Response(200, json=[{"version": 1}, {"version": 2}])
        raise AssertionError(f"unexpected request: {request.method} {path}")

    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(handler))

    results = provision_mod.provision("https://instance.example", "token")

    assert len(results) == 6
    assert all(r["skipped"] for r in results.values())
    assert all(r["version"] == 2 for r in results.values())
    prompt_or_publish_calls = [c for c in calls if "prompt-versions" in c[1] or "publish" in c[1]]
    assert prompt_or_publish_calls == []


def test_provision_is_idempotent_across_repeated_runs(monkeypatch):
    """First run publishes v2; a second run against the now-upgraded
    state must not publish v3 or duplicate anything."""
    state = {"upgraded": set()}

    def handler(request):
        path = request.url.path
        if path == "/agents":
            return httpx.Response(200, json=_AGENTS)
        if path.endswith("/versions") and request.method == "GET":
            agent_id = path.split("/")[2]
            versions = [{"version": 1}]
            if agent_id in state["upgraded"]:
                versions.append({"version": 2})
            return httpx.Response(200, json=versions)
        if path.endswith("/prompt-versions") and request.method == "POST":
            return httpx.Response(201, json={"id": "prompt-id"})
        if path.endswith("/versions") and request.method == "POST":
            agent_id = path.split("/")[2]
            state["upgraded"].add(agent_id)
            return httpx.Response(201, json={"version": 2, "id": "v2-id"})
        if path.endswith("/publish"):
            return httpx.Response(200, json={"version": 2, "status": "active"})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(handler))

    first = provision_mod.provision("https://instance.example", "token")
    assert all(not r["skipped"] for r in first.values())

    second = provision_mod.provision("https://instance.example", "token")
    assert all(r["skipped"] for r in second.values())
    assert all(r["version"] == 2 for r in second.values())


def test_provision_raises_when_starter_agent_missing(monkeypatch):
    def handler(request):
        if request.url.path == "/agents":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr(provision_mod, "_client", lambda base_url, token: _mock_client(handler))

    with pytest.raises(provision_mod.UpgradeError):
        provision_mod.provision("https://instance.example", "token")
