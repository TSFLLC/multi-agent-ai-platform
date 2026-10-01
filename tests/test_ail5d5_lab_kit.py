"""AIL.5D.5 — the pure Educational Lab Kit contract."""

import copy

import pytest

from app import academy_lab_kit as K


def guided_scenario(**over):
    sc = {
        "key": "guided-1", "title": "Guided", "mode": "guided",
        "objective_md": "See grounding.", "instructions_md": "Run, observe, change, rerun.",
        "prompt_template": "Answer {style}: {question}",
        "variables": [
            {"key": "question", "label": "Question", "kind": "text", "max_chars": 200, "default": "What is 2+2?"},
            {"key": "style", "label": "Style", "kind": "choice", "default": "brief",
             "options": [{"key": "brief", "label": "Brief", "text": "briefly"}, {"key": "long", "label": "Long", "text": "at length"}]},
        ],
        "predictions": [{"id": "p1", "text": "What will happen?"}],
        "requirements": {"min_runs": 2, "require_change": True, "require_comparison": True,
                         "require_observation": True, "require_reflection": True},
        "limits": {"max_runs": 4},
        "reveal_md": "SECRET-ANSWER",
    }
    sc.update(over)
    return sc


def kit_spec(*scenarios):
    return {"kit_key": "demo-kit", "lab_kit": {"key": "demo-kit", "version": 1, "title": "Demo",
            "agent": {"key": "demo-agent", "name": "Demo", "role": "Lab", "prompt": "Answer."},
            "scenarios": list(scenarios) or [guided_scenario()]}}


def independent_scenario(**over):
    sc = guided_scenario(key="indep-1", mode="independent", predictions=[],
                         requirements={"min_runs": 2, "require_change": True})
    sc.pop("reveal_md")
    sc.update(over)
    return sc


def test_valid_kit_validates_and_does_not_mutate():
    spec = kit_spec(guided_scenario(), independent_scenario())
    before = copy.deepcopy(spec)
    assert len(K.validate_lab_kit(spec)) == 2
    assert spec == before


@pytest.mark.parametrize("mutate,fragment", [
    (lambda s: s.__setitem__("prompt_template", "Answer {nope}"), "undeclared"),
    (lambda s: s.__setitem__("prompt_template", "Answer {style}"), "never used"),
    (lambda s: s.__setitem__("mode", "exam"), "guided or independent"),
    (lambda s: s.__setitem__("predictions", []), "at least one prediction"),
    (lambda s: s["limits"].__setitem__("max_runs", 1), "at least min_runs"),
    (lambda s: s["limits"].__setitem__("max_runs", 99), "between 1 and"),
    (lambda s: s["requirements"].__setitem__("min_runs", 1), "at least 2 runs"),
    (lambda s: s.__setitem__("surprise", 1), "unknown field"),
])
def test_invalid_scenarios_are_refused(mutate, fragment):
    sc = guided_scenario()
    mutate(sc)
    with pytest.raises(K.KitContractError, match=fragment):
        K.validate_lab_kit(kit_spec(sc))


def test_independent_scenario_carries_less_guidance():
    with pytest.raises(K.KitContractError, match="no predictions"):
        K.validate_lab_kit(kit_spec(independent_scenario(predictions=[{"id": "pp", "text": "x"}])))
    with pytest.raises(K.KitContractError, match="no prompting"):
        K.validate_lab_kit(kit_spec(independent_scenario(reflection_prompt_md="why?")))


def test_kit_key_must_match_and_scenarios_unique():
    spec = kit_spec()
    spec["kit_key"] = "other"
    with pytest.raises(K.KitContractError, match="kit_key"):
        K.validate_lab_kit(spec)
    with pytest.raises(K.KitContractError, match="duplicate scenario"):
        K.validate_lab_kit(kit_spec(guided_scenario(), guided_scenario()))


def test_variables_accept_only_declared_and_bounded_values():
    sc = guided_scenario()
    assert K.validate_variables(sc, {"question": "Hi", "style": "long"}) == {"question": "Hi", "style": "long"}
    assert K.validate_variables(sc, {})["style"] == "brief"          # authored default
    for bad in ({"question": "x", "extra": "y"}, {"style": "wild"}, {"question": "x" * 201}, {"question": "  "}, {"question": 5}, "str"):
        with pytest.raises(K.VariableError):
            K.validate_variables(sc, bad)


def test_render_prompt_substitutes_once_without_recursion():
    sc = guided_scenario()
    out = K.render_prompt(sc, K.validate_variables(sc, {"question": "{style}", "style": "long"}))
    assert out == "Answer at length: {style}"      # a learner's braces are text, never re-expanded


def test_public_scenario_hides_reveal():
    assert "reveal_md" not in K.public_scenario(guided_scenario())
    assert "SECRET-ANSWER" not in str(K.public_scenario(guided_scenario()))


def test_evaluate_walks_the_lifecycle_from_facts():
    sc = guided_scenario()
    ev = lambda **f: K.evaluate(sc, f)  # noqa: E731
    assert ev()["status"] == K.PracticeStatus.CREATED
    assert ev(predictions={"p1"})["status"] == K.PracticeStatus.PREDICTED
    assert ev(predictions={"p1"}, runs_started=1)["status"] == K.PracticeStatus.RUNNING
    one = [{"question": "a", "style": "brief"}]
    assert ev(predictions={"p1"}, runs_started=1, completed_runs=one)["status"] == K.PracticeStatus.OBSERVING
    two_same = one + one
    assert "change" in ev(predictions={"p1"}, completed_runs=two_same, runs_started=2)["next_required"]
    two = one + [{"question": "a", "style": "long"}]
    assert ev(predictions={"p1"}, completed_runs=two, runs_started=2)["status"] == K.PracticeStatus.COMPARING
    full = dict(predictions={"p1"}, completed_runs=two, runs_started=2, observation=True)
    assert ev(**full)["status"] == K.PracticeStatus.COMPARING
    assert ev(**full, comparison=True)["status"] == K.PracticeStatus.REFLECTING
    assert ev(**full, comparison=True, reflection=True)["status"] == K.PracticeStatus.COMPLETED
    assert ev(**full, comparison=True, reflection=True)["next_required"] == []


def test_a_comparison_alone_cannot_complete_without_runs():
    sc = guided_scenario()
    out = K.evaluate(sc, {"predictions": {"p1"}, "comparison": True, "observation": True, "reflection": True})
    assert out["status"] != K.PracticeStatus.COMPLETED


def test_status_is_forward_only_and_terminal_states_are_final():
    S = K.PracticeStatus
    assert K.advance(S.RUNNING, S.CREATED) == S.RUNNING
    assert K.advance(S.RUNNING, S.COMPARING) == S.COMPARING
    assert K.advance(S.COMPLETED, S.RUNNING) == S.COMPLETED
    assert K.advance(S.ABANDONED, S.COMPLETED) == S.ABANDONED
    assert K.advance(S.OBSERVING, S.ABANDONED) == S.ABANDONED
