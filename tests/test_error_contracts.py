"""Consistent API error contracts — Section K.

Every error response is ``{"error": {"code", "message", "detail"?}}``.
Unhandled exceptions never leak stack traces or internals to the client.
"""

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.errors import (
    ConflictError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    LeaseFencingConflictError,
    NotFoundError,
    register_exception_handlers,
)


def _make_test_app() -> TestClient:
    """An isolated app with the same error handlers as app.main, plus
    routes that deliberately raise each error type — avoids depending on
    a real database/route existing for each error class."""
    app = FastAPI()
    register_exception_handlers(app)
    router = APIRouter()

    @router.get("/boom/not-found")
    def _not_found():
        raise NotFoundError("Widget 42 not found.")

    @router.get("/boom/conflict")
    def _conflict():
        raise ConflictError("Widget 42 already exists.")

    @router.get("/boom/invalid-state")
    def _invalid_state():
        raise InvalidStateTransitionError("Cannot cancel a COMPLETED run.")

    @router.get("/boom/idempotency")
    def _idempotency():
        raise IdempotencyConflictError("Key already in flight.", detail={"key": "abc"})

    @router.get("/boom/lease")
    def _lease():
        raise LeaseFencingConflictError("Stale fencing token.")

    @router.get("/boom/unhandled")
    def _unhandled():
        raise RuntimeError("something with a /secret/path/leaked and api_key=sk-shouldnotappear123456")

    @router.get("/boom/validation")
    def _validation(required_int: int):
        return {"required_int": required_int}

    app.include_router(router)
    # raise_server_exceptions=False: the unhandled-exception test needs the
    # actual HTTP response our handler produced, not TestClient's default
    # debugging behavior of re-raising the original exception into the
    # test itself.
    return TestClient(app, raise_server_exceptions=False)


def test_not_found_contract():
    client = _make_test_app()
    resp = client.get("/boom/not-found")
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "Widget 42 not found."}}


def test_conflict_contract():
    client = _make_test_app()
    resp = client.get("/boom/conflict")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_invalid_state_transition_contract():
    client = _make_test_app()
    resp = client.get("/boom/invalid-state")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "invalid_state_transition"


def test_idempotency_conflict_contract_includes_detail():
    client = _make_test_app()
    resp = client.get("/boom/idempotency")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "idempotency_conflict"
    assert body["error"]["detail"] == {"key": "abc"}


def test_lease_fencing_conflict_contract():
    client = _make_test_app()
    resp = client.get("/boom/lease")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "lease_fencing_conflict"


def test_validation_error_contract():
    client = _make_test_app()
    resp = client.get("/boom/validation", params={"required_int": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "errors" in body["error"]["detail"]


def test_unhandled_exception_never_leaks_internals():
    client = _make_test_app()
    resp = client.get("/boom/unhandled")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "internal_error"
    text = resp.text
    assert "/secret/path" not in text
    assert "sk-shouldnotappear123456" not in text
    assert "RuntimeError" not in text
    assert "Traceback" not in text
