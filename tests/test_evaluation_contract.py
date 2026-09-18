"""app.evaluation_contract — pure parsing/prompt-context logic, MA6 Slice 3A.

No DB, no network: parse_evaluation_response must never raise and must
never infer/fabricate a finding for a criterion it couldn't validate --
malformed input always comes back as findings=[] plus a parse_error.
"""

import json

from app.db.enums import EvaluationFinding
from app.evaluation_contract import (
    build_evaluator_extra_context,
    parse_evaluation_response,
)

_KEYS = ["correctness", "coverage"]


def _payload(criteria):
    return json.dumps({"criteria": criteria})


def _valid_entry(key="correctness", finding="MET", rationale="Looks correct.", evidence=None):
    entry = {"key": key, "finding": finding, "rationale": rationale}
    if evidence is not None:
        entry["evidence"] = evidence
    return entry


def _valid_response():
    return _payload(
        [
            _valid_entry("correctness", "MET", "Implements the spec."),
            _valid_entry("coverage", "PARTIAL", "Covers most cases."),
        ]
    )


# -- happy path -----------------------------------------------------------------


def test_parses_valid_response_with_all_criteria():
    result = parse_evaluation_response(_valid_response(), expected_criterion_keys=_KEYS)
    assert result.is_valid
    assert result.parse_error is None
    by_key = {f.key: f for f in result.findings}
    assert by_key["correctness"].finding == EvaluationFinding.MET
    assert by_key["correctness"].rationale == "Implements the spec."
    assert by_key["coverage"].finding == EvaluationFinding.PARTIAL


def test_all_four_finding_values_are_accepted():
    text = _payload(
        [
            _valid_entry("correctness", "NOT_MET", "Fails on empty input."),
            _valid_entry("coverage", "NOT_APPLICABLE", "No tests exist to check."),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert result.is_valid
    by_key = {f.key: f for f in result.findings}
    assert by_key["correctness"].finding == EvaluationFinding.NOT_MET
    assert by_key["coverage"].finding == EvaluationFinding.NOT_APPLICABLE


def test_finding_is_case_insensitive_and_trims_whitespace():
    text = _payload(
        [
            _valid_entry("correctness", "  met  ", "ok"),
            _valid_entry("coverage", "not_applicable", "ok"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert result.is_valid
    by_key = {f.key: f for f in result.findings}
    assert by_key["correctness"].finding == EvaluationFinding.MET
    assert by_key["coverage"].finding == EvaluationFinding.NOT_APPLICABLE


def test_tolerates_markdown_code_fence():
    text = "```json\n" + _valid_response() + "\n```"
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert result.is_valid


def test_valid_evidence_is_parsed():
    text = _payload(
        [
            _valid_entry(
                "correctness",
                "MET",
                "ok",
                evidence=[{"quote": "return n % 2 == 0", "criterion_key": "correctness"}],
            ),
            _valid_entry("coverage", "MET", "ok"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert result.is_valid
    by_key = {f.key: f for f in result.findings}
    assert len(by_key["correctness"].evidence) == 1
    assert by_key["correctness"].evidence[0].quote == "return n % 2 == 0"
    assert by_key["correctness"].evidence[0].criterion_key == "correctness"


def test_missing_evidence_defaults_to_empty_list():
    text = _payload([_valid_entry("correctness"), _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert result.is_valid
    assert all(f.evidence == [] for f in result.findings)


def test_empty_evidence_list_is_valid():
    text = _payload(
        [_valid_entry("correctness", evidence=[]), _valid_entry("coverage")]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert result.is_valid


# -- malformed JSON / wrong root structure ---------------------------------------


def test_not_json_is_rejected():
    result = parse_evaluation_response("not json at all {{{", expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert result.findings == []
    assert "JSON" in result.parse_error


def test_empty_string_is_rejected_not_a_crash():
    result = parse_evaluation_response("", expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert result.parse_error is not None


def test_json_array_instead_of_object_is_rejected():
    result = parse_evaluation_response(json.dumps(["MET"]), expected_criterion_keys=_KEYS)
    assert not result.is_valid


def test_json_scalar_instead_of_object_is_rejected():
    result = parse_evaluation_response(json.dumps("MET"), expected_criterion_keys=_KEYS)
    assert not result.is_valid


def test_casual_prose_is_never_inferred_as_a_finding():
    result = parse_evaluation_response(
        "This candidate meets all criteria, great job!", expected_criterion_keys=_KEYS
    )
    assert not result.is_valid
    assert result.findings == []


# -- missing / malformed criteria array ------------------------------------------


def test_missing_criteria_field_is_rejected():
    result = parse_evaluation_response(json.dumps({}), expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "criteria" in result.parse_error


def test_criteria_not_a_list_is_rejected():
    result = parse_evaluation_response(json.dumps({"criteria": "MET"}), expected_criterion_keys=_KEYS)
    assert not result.is_valid


def test_empty_criteria_list_is_rejected_as_missing_required_criteria():
    result = parse_evaluation_response(json.dumps({"criteria": []}), expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "missing required criteria" in result.parse_error


def test_criterion_entry_not_an_object_is_rejected():
    result = parse_evaluation_response(
        json.dumps({"criteria": ["correctness", _valid_entry("coverage")]}), expected_criterion_keys=_KEYS
    )
    assert not result.is_valid


# -- duplicate / unknown / missing criteria --------------------------------------


def test_duplicate_criterion_key_is_rejected():
    text = _payload(
        [
            _valid_entry("correctness", "MET", "a"),
            _valid_entry("correctness", "NOT_MET", "b"),
            _valid_entry("coverage", "MET", "c"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "duplicated" in result.parse_error


def test_unknown_criterion_key_is_rejected():
    text = _payload(
        [
            _valid_entry("correctness", "MET", "a"),
            _valid_entry("coverage", "MET", "b"),
            _valid_entry("architecture_quality", "MET", "invented by the model"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "unknown criterion key" in result.parse_error


def test_missing_expected_criterion_is_rejected():
    text = _payload([_valid_entry("correctness", "MET", "a")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "coverage" in result.parse_error


def test_missing_all_expected_criteria_lists_every_key():
    text = _payload([])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "correctness" in result.parse_error
    assert "coverage" in result.parse_error


def test_entry_missing_key_field_is_rejected():
    text = _payload([{"finding": "MET", "rationale": "ok"}, _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "key" in result.parse_error


def test_entry_with_blank_key_is_rejected():
    text = _payload([_valid_entry("  ", "MET", "ok"), _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid


# -- invalid finding --------------------------------------------------------------


def test_missing_finding_field_is_rejected():
    text = _payload([{"key": "correctness", "rationale": "ok"}, _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "finding" in result.parse_error


def test_invalid_finding_value_is_rejected():
    text = _payload([_valid_entry("correctness", "MOSTLY_MET", "ok"), _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "invalid finding" in result.parse_error


def test_finding_must_be_a_string():
    text = _payload([{"key": "correctness", "finding": 1, "rationale": "ok"}, _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid


def test_percentage_or_score_style_finding_is_rejected():
    """The finding set is frozen (MA6 non-negotiable invariant) -- an
    evaluator cannot smuggle a numeric score through the finding field."""
    text = _payload([_valid_entry("correctness", "85", "ok"), _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid


# -- missing / invalid rationale --------------------------------------------------


def test_missing_rationale_field_is_rejected():
    text = _payload([{"key": "correctness", "finding": "MET"}, _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "rationale" in result.parse_error


def test_empty_rationale_is_rejected():
    text = _payload([_valid_entry("correctness", "MET", ""), _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid


def test_whitespace_only_rationale_is_rejected():
    text = _payload([_valid_entry("correctness", "MET", "   "), _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid


def test_rationale_must_be_a_string():
    text = _payload([{"key": "correctness", "finding": "MET", "rationale": 123}, _valid_entry("coverage")])
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid


# -- invalid evidence structure ---------------------------------------------------


def test_evidence_not_a_list_is_rejected():
    text = _payload(
        [
            {"key": "correctness", "finding": "MET", "rationale": "ok", "evidence": "quote"},
            _valid_entry("coverage"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "evidence" in result.parse_error


def test_evidence_entry_not_an_object_is_rejected():
    text = _payload(
        [
            _valid_entry("correctness", evidence=["just a string"]),
            _valid_entry("coverage"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid


def test_evidence_missing_quote_is_rejected():
    text = _payload(
        [
            _valid_entry("correctness", evidence=[{"criterion_key": "correctness"}]),
            _valid_entry("coverage"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "quote" in result.parse_error


def test_evidence_empty_quote_is_rejected():
    text = _payload(
        [
            _valid_entry("correctness", evidence=[{"quote": "  ", "criterion_key": "correctness"}]),
            _valid_entry("coverage"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid


def test_evidence_missing_criterion_key_is_rejected():
    text = _payload(
        [
            _valid_entry("correctness", evidence=[{"quote": "some text"}]),
            _valid_entry("coverage"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "criterion_key" in result.parse_error


# -- evidence criterion_key must match its own criterion -------------------------


def test_evidence_criterion_key_mismatch_is_rejected():
    text = _payload(
        [
            _valid_entry(
                "correctness", evidence=[{"quote": "some text", "criterion_key": "coverage"}]
            ),
            _valid_entry("coverage"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert "does not match" in result.parse_error


def test_evidence_criterion_key_referencing_unknown_key_is_rejected():
    text = _payload(
        [
            _valid_entry(
                "correctness", evidence=[{"quote": "some text", "criterion_key": "made_up_key"}]
            ),
            _valid_entry("coverage"),
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid


# -- rejection is total: no partial trust of a malformed response ---------------


def test_one_bad_entry_invalidates_the_whole_response_even_if_others_are_valid():
    text = _payload(
        [
            _valid_entry("correctness", "MET", "this one is fine"),
            {"key": "coverage", "finding": "SORT_OF", "rationale": "bad finding value"},
        ]
    )
    result = parse_evaluation_response(text, expected_criterion_keys=_KEYS)
    assert not result.is_valid
    assert result.findings == []


# -- prompt context rendering ----------------------------------------------------


def test_evaluator_extra_context_includes_task_candidate_and_rubric():
    context = build_evaluator_extra_context(
        subject_task_title="Write an is_even function",
        subject_task_description="Return True for even integers.",
        subject_task_requirements="Must handle negative numbers.",
        candidate_text="def is_even(n): return n % 2 == 0",
        candidate_artifact_id="artifact-123",
        candidate_artifact_hash="abc123",
        criteria=[
            {"key": "correctness", "label": "Correctness", "description": "Does it work?"},
            {"key": "coverage", "label": "Requirement Coverage"},
        ],
    )
    assert "Write an is_even function" in context
    assert "Return True for even integers." in context
    assert "Must handle negative numbers." in context
    assert "def is_even(n)" in context
    assert "artifact-123" in context
    assert "abc123" in context
    assert "correctness" in context and "coverage" in context
    assert "Does it work?" in context
    assert "MET" in context and "NOT_APPLICABLE" in context  # instructs the contract
    assert "score" in context.lower()  # explicitly forbids aggregate scoring


def test_evaluator_extra_context_omits_optional_task_fields_when_absent():
    context = build_evaluator_extra_context(
        subject_task_title="Write an is_even function",
        subject_task_description=None,
        subject_task_requirements=None,
        candidate_text="def is_even(n): return n % 2 == 0",
        candidate_artifact_id="artifact-123",
        candidate_artifact_hash="abc123",
        criteria=[{"key": "correctness", "label": "Correctness"}],
    )
    assert "Write an is_even function" in context
    assert "def is_even(n)" in context


def test_evaluator_extra_context_lists_every_criterion_key_in_instructions():
    context = build_evaluator_extra_context(
        subject_task_title="T",
        subject_task_description=None,
        subject_task_requirements=None,
        candidate_text="x",
        candidate_artifact_id="a",
        candidate_artifact_hash="h",
        criteria=[{"key": "k1", "label": "L1"}, {"key": "k2", "label": "L2"}],
    )
    assert "'k1'" in context
    assert "'k2'" in context
