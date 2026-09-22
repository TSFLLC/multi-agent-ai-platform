"""scripts.provision_staging -- existing-provider credential rotation path,
MA7.7C. Uses httpx.MockTransport (same pattern as test_provider_adapter.py)
so these run offline against a fake API, never a live Railway instance.
"""

import httpx
import pytest

from scripts import provision_staging


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="https://staging.example")


def test_provision_rotates_credential_when_provider_exists_and_key_supplied(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ready":
            return httpx.Response(200, json={"ready": True, "current_revision": "abc"})
        if request.url.path == "/providers" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "prov-1", "type": "openrouter", "name": "OpenRouter"}])
        if request.url.path == "/providers/prov-1/rotate-credential":
            assert request.content
            import json

            body = json.loads(request.content)
            assert body == {"api_key": "sk-new-staging-key"}
            return httpx.Response(200, json={"id": "prov-1", "type": "openrouter", "name": "OpenRouter"})
        if request.url.path == "/models/refresh":
            return httpx.Response(202, json={"id": "refresh-1", "status": "success"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr(provision_staging, "_client", lambda base_url, token: _mock_client(handler))

    result = provision_staging.provision(
        "https://staging.example", "token", openrouter_api_key="sk-new-staging-key"
    )

    assert result["provider"]["id"] == "prov-1"
    rotate_calls = [c for c in calls if c[1] == "/providers/prov-1/rotate-credential"]
    assert len(rotate_calls) == 1
    create_calls = [c for c in calls if c[1] == "/providers" and c[0] == "POST"]
    assert create_calls == []


def test_provision_skips_rotation_when_no_key_supplied(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ready":
            return httpx.Response(200, json={"ready": True})
        if request.url.path == "/providers" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "prov-1", "type": "openrouter", "name": "OpenRouter"}])
        if request.url.path == "/models/refresh":
            return httpx.Response(202, json={"id": "refresh-1", "status": "success"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr(provision_staging, "_client", lambda base_url, token: _mock_client(handler))

    result = provision_staging.provision("https://staging.example", "token", openrouter_api_key=None)

    assert result["provider"]["id"] == "prov-1"
    assert all(path != "/providers/prov-1/rotate-credential" for _, path in calls)


def test_provision_raises_on_rotation_failure(monkeypatch):
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ready":
            return httpx.Response(200, json={"ready": True})
        if request.url.path == "/providers" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "prov-1", "type": "openrouter", "name": "OpenRouter"}])
        if request.url.path == "/providers/prov-1/rotate-credential":
            return httpx.Response(401, text="OpenRouter rejected the configured API key (401).")
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr(provision_staging, "_client", lambda base_url, token: _mock_client(handler))

    with pytest.raises(provision_staging.ProvisioningError):
        provision_staging.provision("https://staging.example", "token", openrouter_api_key="sk-bad-key")


def test_provision_still_creates_provider_on_first_run(monkeypatch):
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ready":
            return httpx.Response(200, json={"ready": True})
        if request.url.path == "/providers" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/providers" and request.method == "POST":
            return httpx.Response(201, json={"id": "prov-new", "type": "openrouter", "name": "OpenRouter"})
        if request.url.path == "/models/refresh":
            return httpx.Response(202, json={"id": "refresh-1", "status": "success"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr(provision_staging, "_client", lambda base_url, token: _mock_client(handler))

    result = provision_staging.provision(
        "https://staging.example", "token", openrouter_api_key="sk-first-run-key"
    )
    assert result["provider"]["id"] == "prov-new"
