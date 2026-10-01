"""AIL.5D.5 — the step contract for kit-bound labs and practice, the Professor on lab/practice steps, and the real Day 4/5/9 labs."""

import copy
import json
from decimal import Decimal

import pytest

from app import academy_lab_kit as K
from app import academy_lab_kits as kits
from app.academy_professor import HelpLevel, ProfessorMode, mode_for_step, solution_eligible
from app.academy_steps import StepContractError, validate_structured_spec
from app.db.enums import AssistanceLevel, EvidenceType, LearningItemType
from app.models.academy import AcademyPracticeInstance, AcademyProfessorHelp
from app.models.learner import LearningEvidence
from app.models.lab import Experiment
from app.schemas.professor import ProfessorContextRequest, ProfessorIntent, ProfessorTarget, ProfessorTargetType
from app.services.concept_graph_service import ConceptGraphService
from app.services.professor_execution_service import ProfessorExecutionService
from tests.ail1a_factories import make_concept, make_published_version
from tests.ail5d1_factories import sample_spec
from tests.ail5d5_factories import GUIDED, INDEPENDENT, KIT, LAB_STEP, PRACTICE_STEP, finish_run, lab_steps, make_lab_day
from tests.conftest import make_model, make_provider, make_provider_model
from tests.test_ail5d4_step_professor import CapturingAdapter, _prompt
from tests.test_ail5d5_practice import NOTE, P, REVEAL_FRAGMENT, _create, _last_run_id, _predict, _run

L1 = "/academy/level-1"


# -- the step contract ----------------------------------------------------------------------------------------------------------------


def _spec(steps):
    spec = sample_spec(day=5, kind="lab", steps=steps)
    spec.pop("knowledge_check", None)
    return spec


def test_a_kit_bound_lab_and_a_practice_step_are_valid_and_a_plain_lab_still_needs_predictions():
    steps = validate_structured_spec(_spec(lab_steps()))
    assert [s["type"] for s in steps] == ["teach", "lab", "practice"]
    plain = lab_steps()
    plain[1] = {**plain[1], "binding": {"kind": "personal_lab", "engine": "personal_lab_experiment"}}
    with pytest.raises(StepContractError, match="predictions"):
        validate_structured_spec(_spec(plain))                    # unchanged v1 rule for a lab that has no kit


@pytest.mark.parametrize("mutate,fragment", [
    (lambda s: s[1]["content"].update(predictions=[{"id": "p1", "text": "x"}]), "takes its predictions from the Lab Kit"),
    (lambda s: s[1]["binding"].update(engine="agent_version"), "Personal Lab engine"),
    (lambda s: s[1]["binding"]["kit"].update(extra=1), "unknown field"),
    (lambda s: s[2].pop("binding"), "must bind to an Educational Lab Kit"),
    (lambda s: s[2]["binding"].update(kind="personal_lab"), "academy_lab_kit"),
    (lambda s: s[2]["binding"].update(scenario_keys=[]), "scenario_keys"),
    (lambda s: s[2]["binding"].update(scenario_keys=["a-b", "a-b"]), "must not repeat"),
    (lambda s: s[2].update(private={"authored_check_md": "x"}), "must not carry private"),
    (lambda s: s[2]["content"].update(extra="x"), "unknown field"),
])
def test_malformed_kit_and_practice_steps_are_refused(mutate, fragment):
    steps = copy.deepcopy(lab_steps())
    mutate(steps)
    with pytest.raises(StepContractError, match=fragment):
        validate_structured_spec(_spec(steps))


def test_a_practice_step_is_independent_and_a_lab_step_never_reaches_a_full_explanation():
    step = {"type": "practice", "key": PRACTICE_STEP}
    assert mode_for_step(step) == ProfessorMode.INDEPENDENT and mode_for_step({"type": "lab", "key": LAB_STEP}) == ProfessorMode.GUIDED
    assert solution_eligible(step, {}) is False and solution_eligible({"type": "lab", "key": LAB_STEP}, {}) is False


# -- the Professor on lab and practice steps -----------------------------------------------------------------------------------------------------


@pytest.fixture()
def day(db, bootstrap):
    return make_lab_day(db)


@pytest.fixture()
def professor(db, bootstrap):
    model = make_model(db, canonical_model_id="free/step-professor", structured_output_support=True)
    make_provider_model(db, model=model, provider=make_provider(db), cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()
    adapter = CapturingAdapter()
    return adapter, (lambda user, request: ProfessorExecutionService(db, adapter_factory=lambda _d, _p: adapter).create_and_execute(user, request))


def _ask(item, key, **kw):
    return ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, target=ProfessorTarget(type=ProfessorTargetType.ACADEMY_STEP, id=item.id, step_key=key), **kw)


def test_the_professor_coaches_a_guided_lab_with_the_learners_own_work_and_never_the_lab_answer(client, auth_headers, db, tmp_path, day, bootstrap, professor):
    adapter, ask = professor
    iid = _create(client, auth_headers, day).json()["instance_id"]
    _predict(client, auth_headers, iid)
    run = _run(client, auth_headers, iid, source="none", question="q-opened").json()
    finish_run(db, tmp_path, _last_run_id(run), "My ungrounded output mentioned 1970.")
    result = ask(bootstrap.user, _ask(day, LAB_STEP, help="hint", question="What should I change next?"))
    assert result.status == "complete" and result.assistance["mode"] == "guided" and result.assistance["level"] == "hint"
    prompt = _prompt(adapter)
    assert "FALSE - models often guess instead of admitting it." in prompt and "My ungrounded output mentioned 1970." in prompt
    assert REVEAL_FRAGMENT not in prompt and "reveal_md" not in prompt and "\"private\"" not in prompt
    levels = [ask(bootstrap.user, _ask(day, LAB_STEP, help="hint")).assistance["level"] for _ in range(3)]
    assert levels == ["stronger_hint", "stronger_hint", "stronger_hint"]          # a lab never climbs to a full explanation
    rows = db.query(AcademyProfessorHelp).filter_by(step_key=LAB_STEP).order_by(AcademyProfessorHelp.created_at).all()
    assert [r.assistance_level.value for r in rows] == ["h2", "h3", "h3", "h3"] and not any(r.revealed_solution for r in rows)


def test_independent_practice_gets_progressive_hints_and_the_assistance_is_recorded_for_the_instance(client, auth_headers, db, tmp_path, day, bootstrap, professor):
    adapter, ask = professor
    body = _create(client, auth_headers, day, PRACTICE_STEP).json()
    iid = body["instance_id"]
    assert body["assistance"] == {"tracked": True, "hints_used": 0, "help_count": 0, "max_level": "h0", "revealed_solution": False}
    first = ask(bootstrap.user, _ask(day, PRACTICE_STEP, help="hint"))
    assert first.assistance["mode"] == "independent" and first.assistance["level"] == "hint"
    second = ask(bootstrap.user, _ask(day, PRACTICE_STEP, help="hint"))
    assert second.assistance["level"] == "stronger_hint"
    seen = client.get(f"{P}/{iid}", headers=auth_headers).json()["assistance"]
    assert seen["hints_used"] == 2 and seen["max_level"] == "h3" and seen["revealed_solution"] is False
    for variables in ({"source": "none", "question": "q-admission"}, {"source": "document-edited", "question": "q-admission"}):
        r = _run(client, auth_headers, iid, **variables).json()
        finish_run(db, tmp_path, _last_run_id(r), "Unknown.")
    done = client.get(f"{P}/{iid}", headers=auth_headers).json()
    assert done["status"] == "completed" and done["assistance"]["max_level"] == "h3"
    evidence = db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LAB).one()
    assert evidence.assistance_level == AssistanceLevel.H3 and evidence.score["practice"] is True


def test_a_second_practice_does_not_inherit_the_help_of_the_first(client, auth_headers, db, tmp_path, day, bootstrap, professor):
    adapter, ask = professor
    first = _create(client, auth_headers, day, PRACTICE_STEP).json()["instance_id"]
    ask(bootstrap.user, _ask(day, PRACTICE_STEP, help="hint"))
    client.post(f"{P}/{first}/abandon", headers=auth_headers)
    second = _create(client, auth_headers, day, PRACTICE_STEP).json()
    assert second["assistance"]["help_count"] == 0 and second["assistance"]["max_level"] == "h0"


# -- the real Day 4 / 5 / 9 labs --------------------------------------------------------------------------------------------------------------------------


AUTHORED = {
    4: ("day4-ai-behavior-lab", ["Who won the Pulitzer Prize for Fiction in 1987?", "In exactly three sentences, describe what makes a good teacher.",
                                 "What was the exact sales revenue of Widgets Corp in Q3 2024?", "Attention Is All You Need"]),
    5: ("day5-grounding-lab", ["Answer my question using ONLY the information in this document.", "I cannot answer this from the provided document.", "parking"]),
    9: ("day9-prompt-structure-lab", ["Variant 1 - zero-shot", "Variant 2 - structured", "Variant 3 - few-shot"]),
}


@pytest.mark.parametrize("day_number", [4, 5, 9])
def test_the_authored_kits_keep_the_authored_lab_material_and_offer_guided_and_independent_practice(day_number):
    kit_key, fragments = AUTHORED[day_number]
    assert kits.DAY_KITS[day_number][0] == kit_key
    kit = kits.KITS[kit_key]["lab_kit"]
    blob = json.dumps(kit)
    for fragment in fragments:
        assert fragment in blob, fragment
    modes = {s["mode"] for s in kit["scenarios"]}
    assert modes == {"guided", "independent"} and kits.DAY_KITS[day_number][1] in {s["key"] for s in kit["scenarios"] if s["mode"] == "guided"}
    guided = next(s for s in kit["scenarios"] if s["mode"] == "guided")
    assert guided["predictions"] and guided["requirements"]["require_comparison"] and guided["requirements"]["require_reflection"] and guided["limits"]["max_runs"] <= 10
    assert "reveal_md" not in K.public_scenario(guided)


def _legacy_lab(db, day_number):
    slug = {4: "what-ai-is-and-isnt", 5: "hallucination-grounding", 9: "prompt-structure"}[day_number]
    concept = make_concept(db, slug=slug, name=slug)
    make_published_version(db, concept=concept)
    spec = {"academy_key": f"level1-v2-day-{day_number}", "curriculum": "ail5-level1-practical-ai-foundations-v2", "day": day_number,
            "week": (day_number - 1) // 5 + 1, "kind": "lab", "objectives": ["Run the lab."], "engine_binding": "experiment",
            "capability_boundary": None, "assessment_definition_key": f"level1-day-0{day_number}-reflection"}
    return ConceptGraphService(db).create_learning_item(concept_id=concept.id, item_type=LearningItemType.LAB, title=f"Day {day_number}: lab",
                                                         body_md="legacy", spec=spec, reviewed=True, est_minutes=75)


@pytest.mark.parametrize("day_number", [4, 5, 9])
def test_start_lab_for_days_4_5_and_9_now_opens_a_runnable_kit_practice_not_an_experiment_that_cannot_launch(client, auth_headers, db, tmp_path, bootstrap, day_number):
    item = _legacy_lab(db, day_number)
    response = client.post(f"{L1}/days/{day_number}/start-lab", headers=auth_headers, json={})
    assert response.status_code == 200, response.text
    lab = response.json()
    assert lab["engine"] == "academy_lab_kit" and lab["experiment_id"] is None and lab["practice_mode"] == "guided" and lab["step_key"] == "lab"
    assert lab["return_to"] == f"#/academy/level-1/{day_number}?step=lab&practice={lab['practice_instance_id']}"
    assert db.query(Experiment).count() == 0
    inst = db.get(AcademyPracticeInstance, lab["practice_instance_id"])
    assert inst.kit_key == kits.DAY_KITS[day_number][0] and inst.scenario_key == kits.DAY_KITS[day_number][1] and inst.learning_item_id == item.id
    again = client.post(f"{L1}/days/{day_number}/start-lab", headers=auth_headers, json={}).json()
    assert again["practice_instance_id"] == lab["practice_instance_id"]                  # resuming restores the same instance


def test_a_legacy_lab_runs_a_guided_practice_end_to_end_and_offers_independent_practice(client, auth_headers, db, tmp_path, bootstrap):
    item = _legacy_lab(db, 9)
    lab = client.post(f"{L1}/days/9/start-lab", headers=auth_headers, json={}).json()
    iid = lab["practice_instance_id"]
    view = client.get(f"{P}/{iid}", headers=auth_headers).json()
    for pred in view["scenario"]["predictions"]:
        assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "prediction", "prediction_id": pred["id"], "text": "My prediction is variant three."}).status_code == 200
    for variant in ("v1", "v3"):
        body = _run(client, auth_headers, iid, variant=variant, review="r5").json()
        finish_run(db, tmp_path, _last_run_id(body), "negative")
    ids = [r["run_id"] for r in client.get(f"{P}/{iid}", headers=auth_headers).json()["runs"]]
    for kind, text in (("observation", NOTE), ("comparison", NOTE + " The few-shot examples fixed the format.")):
        assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": kind, "text": text, "run_ids": ids}).status_code == 200
    done = client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "reflection", "text": "Structure and examples change what the model returns."}).json()
    assert done["status"] == "completed" and done["assistance"]["tracked"] is False
    evidence = db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LAB).one()
    assert evidence.assistance_level == AssistanceLevel.H3 and evidence.score["practice"] is True and evidence.score["assistance_tracked"] is False
    practice = client.post(P, headers=auth_headers, json={"learning_item_id": item.id, "step_key": "practice"}).json()
    assert practice["mode"] == "independent" and practice["scenario"]["key"] == "practice-prompt-variants"
    assert client.post(P, headers=auth_headers, json={"learning_item_id": item.id, "step_key": "no-such-step"}).status_code == 404
    other_day = make_lab_day(db, slug="not-a-kit-day", day=11, steps=[s for s in lab_steps() if s["type"] == "teach"])
    assert client.post(P, headers=auth_headers, json={"learning_item_id": other_day.id, "step_key": "intro"}).status_code == 409
