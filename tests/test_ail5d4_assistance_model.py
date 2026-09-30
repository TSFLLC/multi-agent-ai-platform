"""AIL.5D.4 — the pure assistance model of the step-scoped Professor (modes, the help ladder, H-levels)."""

import pytest

from app.academy_professor import (
    HELP_LADDER, HelpLevel, HelpRequest, ProfessorMode, assistance_for, decide_level, describe, instructions, mode_for_step, solution_eligible,
)
from app.academy_steps import StepType
from app.db.enums import AssistanceLevel


def _step(step_type, **extra):
    return {"type": step_type, **extra}


# -- modes --------------------------------------------------------------------------------------------------------------------


def test_every_step_type_has_a_mode_teaching_for_reading_and_guided_for_anything_the_learner_does():
    modes = {t.value: mode_for_step(_step(t.value)) for t in StepType}
    assert modes["teach"] == modes["example"] == ProfessorMode.TEACHING
    assert {modes[t] for t in ("reflect", "think", "check", "explain_back", "reflection", "lab")} == {ProfessorMode.GUIDED}


def test_a_step_can_declare_independent_practice_and_nothing_else_changes_the_mode():
    assert mode_for_step(_step("check", mode="independent")) == ProfessorMode.INDEPENDENT
    assert mode_for_step(_step("teach", mode="guided")) == ProfessorMode.GUIDED
    assert mode_for_step(_step("teach", mode="teaching")) == ProfessorMode.TEACHING      # unknown/other values fall back to the type rule
    assert mode_for_step(_step("think", mode="assessment")) == ProfessorMode.GUIDED      # assessment is never a Professor mode


def test_assessment_is_not_a_professor_mode():
    assert {m.value for m in ProfessorMode} == {"teaching", "guided", "independent"}


# -- the ladder ---------------------------------------------------------------------------------------------------------------------


def test_the_ladder_is_clarify_hint_stronger_hint_explain():
    assert [level.value for level in HELP_LADDER] == ["clarify", "hint", "stronger_hint", "explain"]


def test_a_free_question_is_clarification_in_guided_and_independent_modes_and_a_full_explanation_when_teaching():
    assert decide_level(ProfessorMode.TEACHING, HelpRequest.ASK, 0, True) == HelpLevel.EXPLAIN
    for mode in (ProfessorMode.GUIDED, ProfessorMode.INDEPENDENT):
        for eligible in (False, True):
            assert decide_level(mode, HelpRequest.ASK, 5, eligible) == HelpLevel.CLARIFY      # asking never escalates


@pytest.mark.parametrize("mode", [ProfessorMode.GUIDED, ProfessorMode.INDEPENDENT])
def test_hint_requests_climb_one_rung_at_a_time_and_stop_below_explain_until_the_learner_is_eligible(mode):
    before = [decide_level(mode, HelpRequest.HINT, n, False) for n in range(6)]
    assert before == [HelpLevel.HINT, HelpLevel.STRONGER_HINT, HelpLevel.STRONGER_HINT, HelpLevel.STRONGER_HINT, HelpLevel.STRONGER_HINT, HelpLevel.STRONGER_HINT]
    after = [decide_level(mode, HelpRequest.HINT, n, True) for n in range(6)]
    assert after == [HelpLevel.HINT, HelpLevel.STRONGER_HINT, HelpLevel.EXPLAIN, HelpLevel.EXPLAIN, HelpLevel.EXPLAIN, HelpLevel.EXPLAIN]


def test_the_level_is_monotone_and_never_solves_the_exercise_immediately():
    for mode in (ProfessorMode.GUIDED, ProfessorMode.INDEPENDENT):
        levels = [HELP_LADDER.index(decide_level(mode, HelpRequest.HINT, n, True)) for n in range(8)]
        assert levels == sorted(levels) and levels[0] == 1   # the very first hint is a hint, not the explanation
    assert decide_level(ProfessorMode.GUIDED, HelpRequest.HINT, -3, True) == HelpLevel.HINT


# -- eligibility ---------------------------------------------------------------------------------------------------------------------


def test_explaining_fully_is_only_eligible_when_it_cannot_do_the_exercise_for_the_learner():
    assert solution_eligible(_step("teach"), {}) and solution_eligible(_step("example"), {})
    assert solution_eligible(_step("think"), {"committed": False}) is False and solution_eligible(_step("think"), {"committed": True}) is True
    assert solution_eligible(_step("check"), {"passed": False}) is False and solution_eligible(_step("check"), {"passed": True}) is True
    for t in ("reflect", "reflection", "explain_back"):
        assert solution_eligible(_step(t), {}) is True      # the question may be explained; the response is never written for them
    assert solution_eligible(_step("lab"), {"passed": True}) is False


# -- tracking on the existing H0-H5 scale ---------------------------------------------------------------------------------------------


def test_assistance_is_recorded_on_the_existing_independence_scale():
    assert assistance_for(ProfessorMode.TEACHING, HelpLevel.EXPLAIN, revealed_solution=False) is None     # not help on an exercise
    assert assistance_for(ProfessorMode.TEACHING, HelpLevel.EXPLAIN, revealed_solution=True) is None
    mapped = [assistance_for(ProfessorMode.GUIDED, level, revealed_solution=False) for level in HELP_LADDER]
    assert mapped == [AssistanceLevel.H1, AssistanceLevel.H2, AssistanceLevel.H3, AssistanceLevel.H4]
    assert assistance_for(ProfessorMode.INDEPENDENT, HelpLevel.HINT, revealed_solution=True) == AssistanceLevel.H5   # solution shown


def test_guidance_text_never_permits_writing_the_learners_response_or_grading():
    for mode in ProfessorMode:
        for level in HELP_LADDER:
            text = instructions(mode, level, "think", False)
            assert "Never write the learner's own response" in text and "Never grade" in text
    assert "do not reveal, confirm or rule out the answer" in instructions(ProfessorMode.GUIDED, HelpLevel.HINT, "think", False)
    assert "do not reveal" not in instructions(ProfessorMode.GUIDED, HelpLevel.EXPLAIN, "think", True)
    assert "independent practice" in instructions(ProfessorMode.INDEPENDENT, HelpLevel.HINT, "check", False)


def test_describe_carries_everything_the_context_needs():
    facts = describe(ProfessorMode.GUIDED, HelpLevel.HINT, HelpRequest.HINT, prior_hints=0, eligible=False, step_type="think")
    assert {"mode", "level", "request", "prior_hints", "solution_eligible", "instructions"} <= set(facts)
    assert facts["mode"] == "guided" and facts["level"] == "hint" and facts["request"] == "hint"
