"""AIL.5D.1 — the structured Day/Step authored contract (pure validation, ordering, privacy, ORM guard)."""

import copy
import json

import pytest

from app.academy_steps import (
    MAX_STEPS, StepContractError, StepType, fingerprint, is_structured, public_steps, strip_private, validate_structured_spec,
)
from app.models.concepts import LearningItem
from tests.ail5d1_factories import CLAIM_WHY, REVEAL, sample_spec, sample_steps


def _spec(**kw):
    return sample_spec(**kw)


def _bad(mutate, match):
    spec = _spec()
    mutate(spec)
    with pytest.raises(StepContractError, match=match):
        validate_structured_spec(spec)


def _step(spec, key):
    return next(s for s in spec["steps"] if s["key"] == key)


# -- the valid contract ---------------------------------------------------------------------------------------------------


def test_a_valid_structured_day_covers_every_step_type_and_keeps_authored_order():
    steps = validate_structured_spec(_spec())
    assert {s["type"] for s in steps} == {t.value for t in StepType}
    assert [s["key"] for s in steps] == [s["key"] for s in sample_steps()]


def test_validation_never_mutates_the_spec():
    spec = _spec()
    before = copy.deepcopy(spec)
    validate_structured_spec(spec)
    assert spec == before


# -- stable keys and order ----------------------------------------------------------------------------------------------


def test_positions_are_the_authored_order_and_keys_are_unique_slugs():
    view = public_steps(_spec())
    assert [s["position"] for s in view] == list(range(1, len(view) + 1))
    assert len({s["key"] for s in view}) == len(view)


def test_duplicate_step_keys_are_rejected():
    _bad(lambda s: s["steps"][1].__setitem__("key", "baseline"), "duplicate step key")


@pytest.mark.parametrize("key", ["", "A-Upper", "has space", "-lead", "trail-", "double--dash", "x", "a" * 65, 7])
def test_step_keys_must_be_lowercase_slugs(key):
    _bad(lambda s: s["steps"][0].__setitem__("key", key), "key")


def test_reordering_steps_changes_positions_but_not_keys_or_fingerprints():
    spec = _spec()
    a = {s["key"]: (s["position"], s["fingerprint"]) for s in public_steps(spec)}
    spec["steps"][1], spec["steps"][2] = spec["steps"][2], spec["steps"][1]
    b = {s["key"]: (s["position"], s["fingerprint"]) for s in public_steps(spec)}
    assert a["what-is-ai"][1] == b["what-is-ai"][1]
    assert a["what-is-ai"][0] != b["what-is-ai"][0]


# -- required vs optional -----------------------------------------------------------------------------------------------


def test_required_must_be_an_explicit_boolean():
    _bad(lambda s: s["steps"][0].__setitem__("required", "yes"), "required")
    _bad(lambda s: s["steps"][0].pop("required"), "missing field")


def test_a_day_needs_at_least_one_required_step():
    def all_optional(spec):
        for step in spec["steps"]:
            step["required"] = False
    _bad(all_optional, "at least one required")


def test_required_and_optional_flags_survive_into_the_public_view():
    view = {s["key"]: s["required"] for s in public_steps(_spec())}
    assert view["baseline"] is False and view["what-is-ai"] is True and view["explain-ai"] is False


# -- schema shape -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("version", [None, 0, 2, "1", True])
def test_schema_version_must_be_exactly_one(version):
    spec = _spec()
    spec["step_schema_version"] = version
    with pytest.raises(StepContractError):
        validate_structured_spec(spec)


def test_steps_without_a_schema_version_are_rejected():
    spec = _spec()
    del spec["step_schema_version"]
    with pytest.raises(StepContractError, match="step_schema_version"):
        validate_structured_spec(spec)


def test_step_count_and_size_are_bounded():
    _bad(lambda s: s.__setitem__("steps", []), "1 to")
    many = sample_steps() * 5
    for i, step in enumerate(many):
        step["key"] = f"step-{i:03d}"
    assert len(many) > MAX_STEPS
    with pytest.raises(StepContractError, match="1 to"):
        validate_structured_spec(_spec(steps=many))
    _bad(lambda s: _step(s, "what-is-ai")["content"]["blocks"].append({"kind": "md", "text": "x" * 21000}), "at most")


@pytest.mark.parametrize("field", ["key", "type", "title", "estimated_minutes", "content"])
def test_each_core_field_is_required(field):
    _bad(lambda s: s["steps"][1].pop(field), "missing field")


def test_unknown_step_fields_and_unknown_types_are_rejected():
    _bad(lambda s: s["steps"][1].__setitem__("answer", "leak"), "unknown field")
    _bad(lambda s: s["steps"][1].__setitem__("type", "video"), "type")


def test_estimated_minutes_must_be_a_bounded_integer():
    for bad in (0, -1, 181, "5", 5.5, True):
        _bad(lambda s, b=bad: s["steps"][1].__setitem__("estimated_minutes", b), "estimated_minutes")


def test_teaching_blocks_are_a_closed_vocabulary():
    _bad(lambda s: _step(s, "what-is-ai")["content"]["blocks"].append({"kind": "script", "text": "<script>"}), "kind must be one of")
    _bad(lambda s: _step(s, "what-is-ai")["content"]["blocks"].append({"kind": "tabs", "items": [{"label": "only one", "text": "x"}]}), "2 to 6")
    _bad(lambda s: _step(s, "what-is-ai")["content"]["blocks"].append({"kind": "md", "text": "  "}), "non-empty")
    _bad(lambda s: _step(s, "what-is-ai")["content"].__setitem__("blocks", []), "1 to")


# -- private content never leaves the server --------------------------------------------------------------------------------


def test_a_think_step_must_carry_private_reveal_and_matching_claims():
    _bad(lambda s: _step(s, "bp-reminder").pop("private"), "needs private")
    _bad(lambda s: _step(s, "three-claims")["private"]["claims"].pop("c2"), "exactly one entry per statement")
    _bad(lambda s: _step(s, "three-claims")["private"]["claims"]["c1"].__setitem__("answer", "maybe"), "answer")
    _bad(lambda s: _step(s, "bp-reminder")["private"].__setitem__("claims", {}), "reveal_md only")


def test_only_think_and_lab_may_carry_private_content():
    _bad(lambda s: _step(s, "what-is-ai").__setitem__("private", {"reveal_md": "x"}), "must not carry private")
    _bad(lambda s: _step(s, "ai-or-not").__setitem__("private", {"answers": {}}), "must not carry private")
    _bad(lambda s: _step(s, "explain-ai").__setitem__("private", {"reference_points": ["x"]}), "must not carry private")


def test_public_view_never_contains_private_material():
    view = public_steps(_spec())
    blob = json.dumps(view)
    assert REVEAL not in blob and CLAIM_WHY not in blob and "Correct answer: X." not in blob
    assert all("private" not in s for s in view)


def test_strip_private_removes_it_without_touching_other_fields_or_non_structured_specs():
    spec = _spec()
    clean = strip_private(spec)
    assert all("private" not in s for s in clean["steps"])
    assert clean["knowledge_check"] == spec["knowledge_check"]
    assert any("private" in s for s in spec["steps"])  # original untouched
    legacy = {"knowledge_check": [{"id": "q", "answer": "a"}]}
    assert strip_private(legacy) == legacy
    assert strip_private(None) is None


# -- bindings reuse existing mechanisms --------------------------------------------------------------------------------------


def test_a_check_step_binds_to_the_days_existing_knowledge_check():
    _bad(lambda s: _step(s, "ai-or-not").pop("binding"), "must bind")
    _bad(lambda s: s.__setitem__("knowledge_check", []), "knowledge_check")
    _bad(lambda s: s["steps"].append({**copy.deepcopy(_step(s, "ai-or-not")), "key": "another-check"}), "at most one check")


def test_an_explain_back_binding_must_match_the_days_ail5c_assessment():
    _bad(lambda s: _step(s, "explain-ai")["binding"].__setitem__("definition_key", "some-other-definition"), "assessment_definition_key")
    _bad(lambda s: s["steps"].append({**copy.deepcopy(_step(s, "explain-ai")), "key": "explain-again"}), "at most one explain_back")


def test_a_lab_step_binds_to_an_existing_engine_and_only_on_a_lab_day():
    _bad(lambda s: _step(s, "lab-run").pop("binding"), "must bind")
    _bad(lambda s: _step(s, "lab-run")["binding"].__setitem__("engine", "homemade_runner"), "engine")
    _bad(lambda s: _step(s, "lab-run")["binding"].__setitem__("engine", "ma7_workflow"), "engine_binding")
    _bad(lambda s: s.__setitem__("kind", "lecture"), "lab Day")


def test_steps_that_reuse_no_mechanism_must_not_carry_a_binding():
    _bad(lambda s: _step(s, "what-is-ai").__setitem__("binding", {"kind": "assessment", "definition_key": "x"}), "must not carry a binding")


def test_a_reflection_can_only_compare_to_an_earlier_reflect_step():
    _bad(lambda s: _step(s, "compare-answers")["content"].__setitem__("compare_to", "what-is-ai"), "EARLIER reflect")
    _bad(lambda s: _step(s, "compare-answers")["content"].__setitem__("compare_to", "does-not-exist"), "EARLIER reflect")
    spec = _spec()
    spec["steps"].insert(0, spec["steps"].pop(7))  # reflection now precedes the reflect it points at
    with pytest.raises(StepContractError, match="EARLIER reflect"):
        validate_structured_spec(spec)


# -- fingerprints ---------------------------------------------------------------------------------------------------------


def test_fingerprint_is_stable_and_tracks_only_learner_visible_definition():
    base = _step(_spec(), "bp-reminder")
    assert fingerprint(base) == fingerprint(copy.deepcopy(base))
    edited = copy.deepcopy(base)
    edited["private"]["reveal_md"] = "a corrected answer key"
    assert fingerprint(edited) == fingerprint(base)  # correcting a hidden answer does not invalidate progress
    edited = copy.deepcopy(base)
    edited["estimated_minutes"] = 9
    edited["professor_hint"] = "hint"
    assert fingerprint(edited) == fingerprint(base)
    for change in (("title", "Another title"), ("required", False)):
        edited = copy.deepcopy(base)
        edited[change[0]] = change[1]
        assert fingerprint(edited) != fingerprint(base)
    edited = copy.deepcopy(base)
    edited["content"]["question_md"] = "A different question"
    assert fingerprint(edited) != fingerprint(base)


# -- opt-in and the ORM guard -----------------------------------------------------------------------------------------------


def test_non_structured_specs_are_untouched():
    for legacy in (None, {}, {"academy_key": "k", "knowledge_check": []}, {"steps_taken": 3}):
        assert not is_structured(legacy)
        assert public_steps(legacy) is None
    assert is_structured({"steps": []}) and is_structured({"step_schema_version": 1})


def test_learning_item_rejects_an_invalid_structured_spec_at_construction_and_assignment():
    bad = _spec()
    bad["steps"][0]["type"] = "video"
    with pytest.raises(StepContractError):
        LearningItem(concept_id="c", item_type="resource", title="t", spec=bad)
    item = LearningItem(concept_id="c", item_type="resource", title="t", spec=_spec())
    with pytest.raises(StepContractError):
        item.spec = bad
    item.spec = {"academy_key": "legacy-shape"}  # a non-structured spec is always accepted
    assert item.spec == {"academy_key": "legacy-shape"}
