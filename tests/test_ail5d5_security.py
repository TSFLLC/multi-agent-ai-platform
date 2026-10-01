"""AIL.5D.5 — the security and integrity boundary of Educational Lab Kit practice.

Each test names the attack. The shared principle: a learner can supply their own words and the permitted variable values, and
nothing else - not a model, a cap, a budget, a status, a result, an assistance level, a return URL or another learner's record."""

import copy
import json
from decimal import Decimal

import pytest

from app import academy_lab_kit as K
from app import academy_lab_kits as kits
from app.auth import get_current_user
from app.config import settings
from app.db.enums import AssistanceLevel, BudgetReservationStatus, EvidenceType
from app.main import app as fastapi_app
from app.models.academy import AcademyPracticeInstance, AcademyPracticeRun, AcademyProfessorHelp, AcademyStepProgress
from app.models.governance import Budget, BudgetReservation
from app.models.identity import User
from app.models.learner import LearningEvidence
from app.models.tasks import AgentRun, TaskRun
from app.services.independence_policy import demonstration_floor
from app.services.learner_state_service import LearnerStateService
from tests.ail1a_factories import make_user
from tests.ail5d1_factories import new_version
from tests.ail5d5_factories import (
    GUIDED, INDEPENDENT, KIT, LAB_STEP, PRACTICE_STEP, finish_run, kit_with, lab_steps, make_lab_day,
)
from tests.conftest import make_model, make_provider, make_provider_model
from tests.test_ail5d5_practice import NOTE, P, PREDICTIONS, REVEAL_FRAGMENT, _create, _guided_to_completion, _last_run_id, _predict, _run


@pytest.fixture()
def day(db, bootstrap):
    return make_lab_day(db)


@pytest.fixture(autouse=True)
def _clean(db):
    yield
    fastapi_app.dependency_overrides.pop(get_current_user, None)
    for key in [k for k in kits.KITS if k.startswith("sec-")]:
        kits.KITS.pop(key)


def _as(db, user):
    fastapi_app.dependency_overrides[get_current_user] = lambda: db.get(User, user.id)


def _started(client, headers, day):
    iid = _create(client, headers, day).json()["instance_id"]
    _predict(client, headers, iid)
    return iid


# -- cross-user access ---------------------------------------------------------------------------------------------------------------


def test_another_learner_cannot_see_run_answer_hand_off_or_abandon_my_practice(client, auth_headers, db, tmp_path, day, bootstrap):
    iid = _started(client, auth_headers, day)
    first = _run(client, auth_headers, iid, source="none", question="q-opened").json()
    finish_run(db, tmp_path, _last_run_id(first), "mine")
    other = make_user(db, org=None, email="second@example.com")
    db.commit()
    _as(db, other)
    for response in (
        client.get(f"{P}/{iid}", headers=auth_headers),
        client.get(f"{P}/{iid}/handoff", headers=auth_headers),
        _run(client, auth_headers, iid, source="document", question="q-opened"),
        client.post(f"{P}/{iid}/abandon", headers=auth_headers),
        client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "observation", "text": NOTE, "run_ids": [_last_run_id(first)]}),
    ):
        assert response.status_code == 404
    mine = _create(client, auth_headers, day)                 # the other learner gets their OWN instance of the same step
    assert mine.status_code == 201 and mine.json()["instance_id"] != iid and mine.json()["runs"] == []
    assert db.get(AcademyPracticeInstance, iid).status.value in ("observing", "running", "predicted", "created")
    assert db.query(AcademyPracticeRun).filter_by(instance_id=iid).count() == 1


# -- the master kit is immutable to learners ---------------------------------------------------------------------------------------------


def test_the_master_kit_cannot_be_mutated_and_an_instance_keeps_the_scenario_it_was_created_from(client, auth_headers, db, day, monkeypatch):
    iid = _create(client, auth_headers, day).json()["instance_id"]
    row = db.get(AcademyPracticeInstance, iid)
    assert row.scenario_sha256 == K.scenario_fingerprint(K.scenario_of(kits.KITS[KIT], GUIDED))
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)(f"{P}/{iid}", headers=auth_headers).status_code == 405
    for forged in ({"scenario": {"key": "x"}}, {"kit": {"key": KIT}}, {"prompt_template": "{source}"}, {"max_runs": 99}, {"scenario_key": "other"}):
        assert client.post(f"{P}/{iid}/runs", headers=auth_headers, json={"variables": {}, **forged}).status_code == 422
        assert client.post(P, headers=auth_headers, json={"learning_item_id": day.id, "step_key": LAB_STEP, **forged}).status_code == 422
    live = K.scenario_of(kits.KITS[KIT], GUIDED)
    original = live["instructions_md"]
    monkeypatch.setitem(live, "instructions_md", "REVISED AFTER THE LEARNER STARTED")
    assert client.get(f"{P}/{iid}", headers=auth_headers).json()["scenario"]["instructions_md"] == original
    assert db.get(AcademyPracticeInstance, iid).scenario["instructions_md"] == original


def test_every_authored_kit_validates_and_a_learner_has_no_route_that_writes_one():
    for spec in kits.KITS.values():
        K.validate_lab_kit(spec)
    routes = {(m, r.path) for r in fastapi_app.routes for m in getattr(r, "methods", ()) if "/academy/practice" in getattr(r, "path", "")}
    assert not any(m in ("PUT", "PATCH", "DELETE") for m, _ in routes)
    assert not any("kit" in p.lower() and m == "POST" for m, p in routes)


# -- forged completion / forged results -------------------------------------------------------------------------------------------------


def test_completion_cannot_be_asserted_by_any_request(client, auth_headers, db, day, bootstrap):
    iid = _started(client, auth_headers, day)
    for body in ({"status": "completed"}, {"completed": True}, {"evidence": {"passed": True}}, {"state": "completed"}):
        assert client.post(f"{P}/{iid}/runs", headers=auth_headers, json={"variables": {}, **body}).status_code == 422
        assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "reflection", "text": "I did everything.", **body}).status_code == 422
    assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "completion", "text": "I did everything."}).status_code == 422
    assert client.post(f"/academy/level-1/items/{day.id}/steps/{LAB_STEP}/complete", headers=auth_headers).status_code == 409
    assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "reflection", "text": "I did everything I was asked to."}).status_code == 409
    assert db.get(AcademyPracticeInstance, iid).status.value == "predicted"
    assert db.query(AcademyStepProgress).filter_by(user_id=bootstrap.user.id, step_key=LAB_STEP, status="completed").count() == 0
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LAB).count() == 0


def test_a_forged_experiment_result_or_run_reference_is_refused(client, auth_headers, db, tmp_path, day, bootstrap):
    iid = _started(client, auth_headers, day)
    running = _run(client, auth_headers, iid, source="none", question="q-opened").json()
    rid = _last_run_id(running)
    obs = lambda ids, **extra: client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "observation", "text": NOTE, "run_ids": ids, **extra})  # noqa: E731
    assert obs([rid]).status_code == 422                          # still running: nothing to observe
    assert obs(["made-up-run"]).status_code == 422
    assert obs([]).status_code == 422
    assert obs([rid], output="the model said 1968").status_code == 422     # a client cannot supply a result
    assert obs([rid], experiment_id="exp-1").status_code == 422
    finish_run(db, tmp_path, rid, "real output")
    other = make_user(db, org=None, email="second@example.com")
    db.commit()
    _as(db, other)
    theirs = _create(client, auth_headers, day).json()["instance_id"]
    other_runs = client.post(f"{P}/{theirs}/responses", headers=auth_headers, json={"kind": "observation", "text": NOTE, "run_ids": [rid]})
    assert other_runs.status_code == 422                          # a run id of someone else's practice is not mine
    fastapi_app.dependency_overrides.pop(get_current_user, None)
    assert obs([rid]).status_code == 200


# -- model, run-limit and budget controls ---------------------------------------------------------------------------------------------------


def test_a_learner_cannot_select_an_arbitrary_model(client, auth_headers, db, day):
    iid = _started(client, auth_headers, day)
    for body in ({"variables": {"source": "none", "question": "q-opened"}, "model": "openai/gpt-5"},
                 {"variables": {"source": "none", "question": "q-opened"}, "model_policy": {"mode": "manual"}},
                 {"variables": {"source": "none", "question": "q-opened", "model": "openai/gpt-5"}},
                 {"variables": {"source": "none", "question": "q-opened"}, "budget_id": "x"},
                 {"variables": {"source": "none", "question": "q-opened"}, "agent_version_id": "x"}):
        assert client.post(f"{P}/{iid}/runs", headers=auth_headers, json=body).status_code == 422
    assert db.query(TaskRun).count() == 0


def test_an_authored_model_choice_is_restricted_to_the_authored_allow_list_and_pins_only_that_model(client, auth_headers, db, bootstrap):
    def add_model(kit):
        sc = next(s for s in kit["scenarios"] if s["key"] == GUIDED)
        sc["variables"].append({"key": "model", "label": "Model", "kind": "model", "options": ["test/model-a", "test/model-b"], "default": "test/model-a"})
        sc["allowed_models"] = ["test/model-a", "test/model-b"]

    kit_with(add_model, key="sec-model-kit")
    pm = make_provider_model(db, make_model(db, "test/model-a"), make_provider(db))
    db.commit()
    day = make_lab_day(db, slug="sec-model-day", steps=lab_steps(kit="sec-model-kit"))
    iid = _create(client, auth_headers, day).json()["instance_id"]
    _predict(client, auth_headers, iid)
    assert _run(client, auth_headers, iid, source="none", question="q-opened", model="openai/gpt-5").status_code == 422
    ok = _run(client, auth_headers, iid, source="none", question="q-opened", model="test/model-a")
    assert ok.status_code == 201 and ok.json()["runs"][0]["model"] == "test/model-a"
    agent_run = db.query(AgentRun).one()
    assert agent_run.model_policy_override_json == {"mode": "manual", "manual_provider_model_id": pm.id}
    # an authored model that the platform registry does not offer fails closed rather than falling back to anything else
    day2 = make_lab_day(db, slug="sec-model-day2", day=6, steps=lab_steps(kit="sec-model-kit"))
    other = _create(client, auth_headers, day2).json()["instance_id"]
    _predict(client, auth_headers, other)
    miss = _run(client, auth_headers, other, source="none", question="q-opened", model="test/model-b")
    assert miss.status_code == 409 and miss.json()["error"]["detail"]["code"] == "practice_model_unavailable"


def test_the_run_limit_cannot_be_bypassed_and_failed_runs_count(client, auth_headers, db, tmp_path, bootstrap):
    def cap(kit):
        sc = next(s for s in kit["scenarios"] if s["key"] == GUIDED)
        sc["limits"]["max_runs"] = 2

    kit_with(cap, key="sec-cap-kit")
    day = make_lab_day(db, slug="sec-cap-day", steps=lab_steps(kit="sec-cap-kit"))
    made = _create(client, auth_headers, day)
    assert made.status_code == 201, made.text
    iid = made.json()["instance_id"]
    assert db.get(AcademyPracticeInstance, iid).max_runs == 2
    _predict(client, auth_headers, iid)
    from tests.ail5d5_factories import fail_run

    first = _run(client, auth_headers, iid, source="none", question="q-opened").json()
    fail_run(db, _last_run_id(first))
    second = _run(client, auth_headers, iid, source="document", question="q-opened").json()
    finish_run(db, tmp_path, _last_run_id(second))
    third = _run(client, auth_headers, iid, source="document", question="q-closed")
    assert third.status_code == 409 and third.json()["error"]["detail"]["code"] == "practice_run_limit"
    assert db.query(AcademyPracticeRun).filter_by(instance_id=iid).count() == 2 and db.query(TaskRun).count() == 2
    db.get(AcademyPracticeInstance, iid).max_runs = 2     # the cap was copied at creation; a changed kit cannot raise it
    assert client.get(f"{P}/{iid}", headers=auth_headers).json()["limits"]["runs_left"] == 0


def test_the_daily_run_limit_applies_across_instances(client, auth_headers, db, tmp_path, day, monkeypatch):
    monkeypatch.setattr(settings, "academy_practice_daily_run_cap", 1)
    iid = _started(client, auth_headers, day)
    first = _run(client, auth_headers, iid, source="none", question="q-opened").json()
    finish_run(db, tmp_path, _last_run_id(first))
    stop = _run(client, auth_headers, iid, source="document", question="q-opened")
    assert stop.status_code == 409 and stop.json()["error"]["detail"]["code"] == "practice_daily_limit"
    assert db.query(TaskRun).count() == 1


def test_an_exhausted_practice_budget_stops_a_run_before_any_model_is_called(client, auth_headers, db, day, monkeypatch, bootstrap):
    monkeypatch.setattr(settings, "academy_practice_budget_usd", 1.0)
    iid = _started(client, auth_headers, day)
    budget = db.get(Budget, db.get(AcademyPracticeInstance, iid).budget_id)
    assert budget.limit_amount == Decimal("1") and budget.scope.value == "user" and budget.scope_ref_id == bootstrap.user.id
    db.add(BudgetReservation(budget_id=budget.id, reserved_amount=Decimal("1.0"), committed_amount=Decimal("1.0"), status=BudgetReservationStatus.COMMITTED))
    db.commit()
    seen = client.get(f"{P}/{iid}", headers=auth_headers).json()["limits"]
    assert seen["budget_exhausted"] is True
    stop = _run(client, auth_headers, iid, source="none", question="q-opened")
    assert stop.status_code == 409 and stop.json()["error"]["detail"]["code"] == "practice_budget_exhausted"
    assert db.query(TaskRun).count() == 0 and db.query(AcademyPracticeRun).count() == 0
    assert client.post(f"{P}/{iid}/runs", headers=auth_headers, json={"variables": {}, "budget_id": "other"}).status_code == 422


def test_a_started_run_is_charged_to_the_learners_own_practice_budget(client, auth_headers, db, day):
    iid = _started(client, auth_headers, day)
    run = _run(client, auth_headers, iid, source="none", question="q-opened")
    assert run.status_code == 201
    budget_id = db.get(AcademyPracticeInstance, iid).budget_id
    assert db.query(TaskRun).one().budget_id == budget_id
    assert db.query(TaskRun).one().config_snapshot["frozen_task_snapshot"]["practice"] is True


# -- return URL, stale versions, hidden answers -------------------------------------------------------------------------------------------------


def test_a_client_supplied_return_url_is_never_accepted_or_echoed(client, auth_headers, db, day):
    evil = "https://evil.example/phish"
    assert client.post(P, headers=auth_headers, json={"learning_item_id": day.id, "step_key": LAB_STEP, "return_to": evil}).status_code == 422
    iid = _create(client, auth_headers, day).json()["instance_id"]
    for url in (f"{P}/{iid}?return_to={evil}", f"{P}/{iid}/handoff?return_to={evil}&return={evil}"):
        body = client.get(url, headers=auth_headers).json()
        assert evil not in json.dumps(body)
        assert body["return"]["path"] == f"#/academy/level-1/5?step={LAB_STEP}&practice={iid}"
    assert client.post(f"{P}/{iid}/runs", headers=auth_headers, json={"variables": {}, "return_to": evil}).status_code == 422


def test_a_stale_learning_item_version_cannot_start_or_continue_practice(client, auth_headers, db, tmp_path, day):
    iid = _started(client, auth_headers, day)
    v2 = new_version(db, day, spec=copy.deepcopy(day.spec))
    stale_create = _create(client, auth_headers, day)
    assert stale_create.status_code == 409 and stale_create.json()["error"]["detail"]["code"] == "stale_item_version"
    assert stale_create.json()["error"]["detail"]["current_item_id"] == v2.id
    stale_run = _run(client, auth_headers, iid, source="none", question="q-opened")
    assert stale_run.status_code == 409 and stale_run.json()["error"]["detail"]["code"] == "stale_item_version"
    assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "prediction", "prediction_id": "accuracy", "text": PREDICTIONS["accuracy"]}).status_code == 409
    assert client.get(f"{P}/{iid}", headers=auth_headers).status_code == 200      # reading what you did is still allowed
    assert _create(client, auth_headers, v2).status_code == 201                     # the current version works, with its own instance
    assert db.query(TaskRun).count() == 0


def test_the_authored_lab_answer_is_not_served_until_the_practice_is_completed(client, auth_headers, db, tmp_path, day, bootstrap):
    iid = _started(client, auth_headers, day)
    surfaces = [client.get(f"{P}/{iid}", headers=auth_headers), client.get(f"{P}/{iid}/handoff", headers=auth_headers),
                client.get(f"/academy/level-1/items/{day.id}/steps", headers=auth_headers),
                client.get(f"/academy/level-1/items/{day.id}/steps/{LAB_STEP}/professor", headers=auth_headers),
                client.get("/professor/context-preview", headers=auth_headers, params={
                    "intent": "EXPLAIN_THIS", "target_type": "academy_step", "target_id": day.id, "step_key": LAB_STEP, "help": "hint"})]
    for response in surfaces:
        assert response.status_code == 200, response.text
        assert REVEAL_FRAGMENT not in response.text and "reveal_md" not in response.text
    first = _run(client, auth_headers, iid, source="none", question="q-opened").json()
    finish_run(db, tmp_path, _last_run_id(first), "x")
    again = client.get(f"{P}/{iid}", headers=auth_headers)
    assert REVEAL_FRAGMENT not in again.text
    done_iid, done = _guided_to_completion(client, auth_headers, db, tmp_path, make_lab_day(db, slug="sec-reveal-day", day=7))
    assert REVEAL_FRAGMENT in done["lab_answer_md"]
    preview = client.get("/professor/context-preview", headers=auth_headers, params={
        "intent": "EXPLAIN_THIS", "target_type": "academy_step", "target_id": db.get(AcademyPracticeInstance, done_iid).learning_item_id,
        "step_key": LAB_STEP, "help": "hint"})
    assert preview.status_code == 200 and REVEAL_FRAGMENT not in preview.text       # never to the Professor, even after completion


# -- Professor assistance ----------------------------------------------------------------------------------------------------------------------


def _help(db, user, item, step_key, *, level, request="hint", at=None, interaction):
    from app.academy_professor import HelpLevel, HelpRequest, ProfessorMode
    from app.academy_steps import public_steps

    step = next(s for s in public_steps(item.spec) if s["key"] == step_key)
    row = AcademyProfessorHelp(user_id=user.id, learning_item_id=item.id, lineage_id=item.lineage_id, step_key=step_key,
                               step_fingerprint=step["fingerprint"], mode=ProfessorMode.GUIDED, request_kind=HelpRequest(request),
                               help_level=HelpLevel.HINT, assistance_level=AssistanceLevel(level), revealed_solution=False,
                               interaction_id=interaction, context_sha256="0" * 64)
    db.add(row)
    db.flush()
    if at is not None:
        row.created_at = at
    db.commit()
    return row


def test_assistance_is_derived_from_canonical_professor_help_and_cannot_be_supplied(client, auth_headers, db, tmp_path, day, bootstrap):
    from datetime import timedelta

    iid = _create(client, auth_headers, day).json()["instance_id"]
    created = db.get(AcademyPracticeInstance, iid).created_at
    _help(db, bootstrap.user, day, LAB_STEP, level="h5", at=created - timedelta(hours=1), interaction="1" * 36)   # before this practice: not counted
    other = make_user(db, org=None, email="second@example.com")
    db.commit()
    _help(db, other, day, LAB_STEP, level="h5", interaction="2" * 36)                                                 # someone else's: not counted
    _help(db, bootstrap.user, day, PRACTICE_STEP, level="h5", interaction="3" * 36)                                    # another step: not counted
    _help(db, bootstrap.user, day, LAB_STEP, level="h3", interaction="4" * 36)                                        # this practice's help
    for spoof in ({"assistance_level": "h0"}, {"assistance": {"max_level": "h0"}}, {"hints_used": 0}):
        assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "prediction", "prediction_id": "accuracy", "text": PREDICTIONS["accuracy"], **spoof}).status_code == 422
        assert client.post(f"{P}/{iid}/runs", headers=auth_headers, json={"variables": {}, **spoof}).status_code == 422
    view = client.get(f"{P}/{iid}", headers=auth_headers).json()["assistance"]
    assert view == {"tracked": True, "hints_used": 1, "help_count": 1, "max_level": "h3", "revealed_solution": False}
    _predict(client, auth_headers, iid)
    runs = []
    for variables in ({"source": "none", "question": "q-opened"}, {"source": "document", "question": "q-opened"}):
        body = _run(client, auth_headers, iid, **variables).json()
        finish_run(db, tmp_path, _last_run_id(body), "answer")
    ids = [r["run_id"] for r in client.get(f"{P}/{iid}", headers=auth_headers).json()["runs"]]
    client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "observation", "text": NOTE, "run_ids": ids})
    client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "comparison", "text": NOTE + " Why.", "run_ids": ids})
    done = client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "reflection", "text": "Grounding ties the answer to the text."}).json()
    assert done["status"] == "completed" and done["assistance"]["max_level"] == "h3"
    evidence = db.query(LearningEvidence).filter_by(user_id=bootstrap.user.id, evidence_type=EvidenceType.LAB).one()
    assert evidence.assistance_level == AssistanceLevel.H3 and evidence.score["assistance_tracked"] is True
    assert db.get(AcademyPracticeInstance, iid).assistance["max_level"] == "h3"


def test_unrecorded_help_on_an_untracked_lab_is_never_claimed_as_independent(db, bootstrap, tmp_path):
    """A legacy (non-structured) lab has no step-scoped Professor ledger: its practice evidence is not claimed as H0."""
    from app.db.enums import LearningItemType
    from app.services.academy_practice_service import AcademyPracticeService
    from app.services.concept_graph_service import ConceptGraphService
    from tests.ail1a_factories import make_concept, make_published_version

    concept = make_concept(db, slug="hallucination-grounding", name="Hallucination")
    make_published_version(db, concept=concept)
    spec = {"academy_key": "level1-v2-day-5", "curriculum": "ail5-level1-practical-ai-foundations-v2", "day": 5, "week": 1, "kind": "lab",
            "objectives": ["Run the grounding lab."], "engine_binding": "experiment"}
    item = ConceptGraphService(db).create_learning_item(concept_id=concept.id, item_type=LearningItemType.LAB, title="Day 5", body_md="legacy",
                                                         spec=spec, reviewed=True, est_minutes=60)
    service = AcademyPracticeService(db)
    view = service.create(bootstrap.user, item.id, "lab")
    assert view["assistance"] == {"tracked": False, "hints_used": 0, "help_count": 0, "max_level": None, "revealed_solution": False}


# -- practice is never demonstration ---------------------------------------------------------------------------------------------------------------


def test_completed_practice_can_make_a_concept_practiced_but_never_demonstrated(client, auth_headers, db, tmp_path, day, bootstrap):
    _guided_to_completion(client, auth_headers, db, tmp_path, day)
    evidence = db.query(LearningEvidence).filter_by(user_id=bootstrap.user.id, evidence_type=EvidenceType.LAB).one()
    assert demonstration_floor(evidence) == (False, "practice")
    state = LearnerStateService(db).state(bootstrap.user.id, day.concept_id)
    assert state.ladder in ("practiced", "exposed", "understood") and state.ladder != "demonstrated" and "demonstrated" not in json.dumps(getattr(state, "overlays", ""), default=str).lower()
    assert evidence.assistance_level == AssistanceLevel.H0
    with pytest.raises(ValueError):
        from app.services.learning_evidence_service import LearningEvidenceService
        from app.services.concept_graph_service import ConceptGraphService

        LearningEvidenceService(db).record_evidence(
            user_id=bootstrap.user.id, concept_id=day.concept_id, concept_version_id=ConceptGraphService(db).get_current_version(day.concept_id).id,
            evidence_type=EvidenceType.EXPLAIN_BACK, grader=evidence.grader, score={"practice": True})
