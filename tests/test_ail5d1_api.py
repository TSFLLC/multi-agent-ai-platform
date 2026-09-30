"""AIL.5D.1 HTTP surface: learner-scoped step progress, no private leakage, stale/forged requests refused."""

import json

import pytest

from app.api.routers.academy_level1 import _public_item
from app.auth import get_current_user
from app.db.enums import LearningItemType
from app.main import app as fastapi_app
from app.models.academy import AcademyStepProgress
from app.models.identity import User
from app.services.concept_graph_service import ConceptGraphService
from tests.ail1a_factories import make_concept, make_published_version, make_user
from tests.ail5d1_factories import CLAIM_WHY, REVEAL, make_structured_item, new_version, sample_spec

BASE = "/academy/level-1/items"


def _as(db, user):
    fastapi_app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)


@pytest.fixture()
def item(db, bootstrap):
    return make_structured_item(db)


def test_steps_endpoint_returns_ordered_learner_safe_steps_without_private_content(client, auth_headers, item):
    response = client.get(f"{BASE}/{item.id}/steps", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [s["position"] for s in body["steps"]] == list(range(1, 10))
    assert body["version"] == 1 and body["step_schema_version"] == 1
    blob = json.dumps(body)
    assert REVEAL not in blob and CLAIM_WHY not in blob and "Correct answer: X." not in blob
    assert all("private" not in s for s in body["steps"])
    assert body["progress"]["required_total"] == 6 and body["progress"]["learning_complete"] is False


def test_the_existing_day_endpoint_shape_strips_private_and_keeps_legacy_stripping(item):
    read = _public_item(item)
    assert all("private" not in s for s in read.spec["steps"])
    assert REVEAL not in json.dumps(read.spec)
    assert "answer" not in read.spec["knowledge_check"][0]  # the pre-existing knowledge-check stripping is unchanged
    assert "private" in item.spec["steps"][3]  # the stored, authored content is not modified by reading


def test_open_never_completes_and_complete_is_limited_to_self_completable_steps(client, auth_headers, item):
    opened = client.post(f"{BASE}/{item.id}/steps/what-is-ai/open", headers=auth_headers).json()
    assert opened["step"]["status"] == "opened" and opened["day"]["required_completed"] == 0
    done = client.post(f"{BASE}/{item.id}/steps/what-is-ai/complete", headers=auth_headers).json()
    assert done["step"]["status"] == "completed" and done["day"]["required_completed"] == 1
    refused = client.post(f"{BASE}/{item.id}/steps/ai-or-not/complete", headers=auth_headers)
    assert refused.status_code == 409 and refused.json()["error"]["detail"]["code"] == "interaction_required"
    refused = client.post(f"{BASE}/{item.id}/steps/bp-reminder/complete", headers=auth_headers)
    assert refused.status_code == 409


def test_skip_is_allowed_only_for_optional_steps(client, auth_headers, item):
    assert client.post(f"{BASE}/{item.id}/steps/what-is-ai/skip", headers=auth_headers).status_code == 409
    ok = client.post(f"{BASE}/{item.id}/steps/baseline/skip", headers=auth_headers)
    assert ok.status_code == 200 and ok.json()["step"]["status"] == "skipped"


def test_a_client_cannot_name_another_learner_or_smuggle_completion_in_the_body(client, auth_headers, item, db):
    forged = client.post(
        f"{BASE}/{item.id}/steps/bp-reminder/complete",
        json={"user_id": "someone-else", "status": "completed", "verified": {"kind": "committed_answer"}, "completion_basis": {"kind": "x"}},
        headers=auth_headers,
    )
    assert forged.status_code == 409
    assert db.query(AcademyStepProgress).filter(AcademyStepProgress.step_key == "bp-reminder").count() == 0


def test_progress_is_isolated_between_learners_over_http(client, auth_headers, item, db, bootstrap):
    client.post(f"{BASE}/{item.id}/steps/what-is-ai/complete", headers=auth_headers)
    owner_view = client.get(f"{BASE}/{item.id}/steps", headers=auth_headers).json()["progress"]
    assert owner_view["required_completed"] == 1
    other = make_user(db, org=None, email="second@example.com")
    db.commit()
    _as(db, other)
    other_view = client.get(f"{BASE}/{item.id}/steps", headers=auth_headers).json()["progress"]
    assert other_view["required_completed"] == 0 and {s["status"] for s in other_view["steps"]} == {"not_started"}
    client.post(f"{BASE}/{item.id}/steps/what-is-ai/open", headers=auth_headers)
    fastapi_app.dependency_overrides.pop(get_current_user, None)
    assert client.get(f"{BASE}/{item.id}/steps", headers=auth_headers).json()["progress"]["required_completed"] == 1
    rows = db.query(AcademyStepProgress).filter_by(step_key="what-is-ai").all()
    assert len(rows) == 2 and len({r.user_id for r in rows}) == 2


def test_requests_without_the_local_token_are_rejected(client, item):
    assert client.get(f"{BASE}/{item.id}/steps").status_code == 401
    assert client.post(f"{BASE}/{item.id}/steps/what-is-ai/complete").status_code == 401


def test_a_stale_item_id_gets_a_409_naming_the_current_version(client, auth_headers, item, db):
    v2 = new_version(db, item, spec=sample_spec())
    stale = client.post(f"{BASE}/{item.id}/steps/what-is-ai/open", headers=auth_headers)
    assert stale.status_code == 409
    assert stale.json()["error"]["detail"] == {"code": "stale_item_version", "current_item_id": v2.id, "current_version": 2}
    assert client.get(f"{BASE}/{item.id}/steps", headers=auth_headers).status_code == 409
    assert client.get(f"{BASE}/{v2.id}/steps", headers=auth_headers).status_code == 200


def test_unknown_items_steps_and_non_structured_items_are_404(client, auth_headers, item, db):
    assert client.get(f"{BASE}/no-such-item/steps", headers=auth_headers).status_code == 404
    assert client.post(f"{BASE}/{item.id}/steps/nope/open", headers=auth_headers).status_code == 404
    concept = make_concept(db, slug="legacy-api", name="Legacy")
    make_published_version(db, concept=concept)
    legacy = ConceptGraphService(db).create_learning_item(
        concept_id=concept.id, item_type=LearningItemType.RESOURCE, title="Legacy",
        spec={"academy_key": "level1-v2-day-2", "day": 2, "week": 1, "kind": "lecture", "knowledge_check": []}, reviewed=True,
    )
    assert client.get(f"{BASE}/{legacy.id}/steps", headers=auth_headers).status_code == 404
    assert client.post(f"{BASE}/{legacy.id}/steps/x/open", headers=auth_headers).status_code == 404


def test_existing_level1_and_knowledge_check_routes_are_unchanged_for_legacy_items():
    routes = {(m, r.path) for r in fastapi_app.routes if getattr(r, "methods", None) for m in r.methods}
    for expected in (("GET", "/academy/level-1/days"), ("GET", "/academy/level-1/days/{day}"), ("POST", "/academy/level-1/items/{item_id}/open"),
                     ("POST", "/academy/level-1/items/{item_id}/knowledge-check"), ("POST", "/academy/level-1/days/{day}/start-lab")):
        assert expected in routes


def test_the_professor_context_for_a_structured_day_carries_no_private_or_step_content(db, bootstrap):
    """Professor context is built from explicit Learning Item fields. It must never include ``spec`` (where the
    steps, reveals and answer keys live), so hidden answers cannot reach the Professor through the item."""
    from app.schemas.professor import ProfessorContextRequest, ProfessorTarget
    from app.schemas.professor import ProfessorIntent, ProfessorTargetType
    from app.services.professor_context_service import ProfessorContextAssembler

    item = make_structured_item(db, slug="professor-blind")
    context = ProfessorContextAssembler(db).assemble(
        bootstrap.user.id,
        ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=item.concept_id)),
    )
    blob = json.dumps([r.model_dump(mode="json") for r in context.records], default=str) + json.dumps(context.deterministic_facts, default=str)
    assert item.id in blob  # the item is in context...
    for secret in (REVEAL, CLAIM_WHY, "Correct answer: X.", "private", "reveal_md", "steps"):
        assert secret not in blob, secret  # ...but none of its structured or private content is
