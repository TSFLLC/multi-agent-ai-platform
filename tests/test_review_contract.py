"""app.review_contract — pure parsing/prompt-context logic, MA4.

No DB, no network: parse_review_response must never raise and must never
infer ACCEPT from anything but an exact, valid structured response.
"""

import json

from app.db.enums import ReviewDecision
from app.review_contract import (
    build_repair_extra_context,
    build_reviewer_extra_context,
    parse_review_response,
)


def test_parses_valid_accept():
    text = json.dumps({"decision": "ACCEPT", "summary": "Good.", "issues": [], "repair_instructions": ""})
    result = parse_review_response(text)
    assert result.decision == ReviewDecision.ACCEPT
    assert result.parse_error is None
    assert result.issues == []


def test_parses_valid_repair_required():
    text = json.dumps(
        {
            "decision": "REPAIR_REQUIRED",
            "summary": "Wrong name.",
            "issues": ["function name wrong"],
            "repair_instructions": "Rename to is_even.",
        }
    )
    result = parse_review_response(text)
    assert result.decision == ReviewDecision.REPAIR_REQUIRED
    assert result.issues == ["function name wrong"]
    assert result.repair_instructions == "Rename to is_even."


def test_decision_is_case_insensitive_and_trims_whitespace():
    text = json.dumps({"decision": "  accept  ", "summary": "ok", "issues": []})
    result = parse_review_response(text)
    assert result.decision == ReviewDecision.ACCEPT


def test_tolerates_markdown_code_fence():
    text = "```json\n" + json.dumps({"decision": "ACCEPT", "summary": "ok", "issues": []}) + "\n```"
    result = parse_review_response(text)
    assert result.decision == ReviewDecision.ACCEPT


def test_casual_prose_is_never_inferred_as_accept():
    result = parse_review_response("Looks great, I approve! ACCEPT this please.")
    assert result.decision is None
    assert result.parse_error is not None


def test_not_json_is_invalid():
    result = parse_review_response("not json at all {{{")
    assert result.decision is None
    assert "JSON" in result.parse_error


def test_json_array_instead_of_object_is_invalid():
    result = parse_review_response(json.dumps(["ACCEPT"]))
    assert result.decision is None


def test_missing_decision_field_is_invalid():
    result = parse_review_response(json.dumps({"summary": "ok", "issues": []}))
    assert result.decision is None
    assert "decision" in result.parse_error


def test_unrecognized_decision_value_is_invalid():
    result = parse_review_response(json.dumps({"decision": "MAYBE", "summary": "ok", "issues": []}))
    assert result.decision is None


def test_repair_required_with_empty_repair_instructions_is_invalid():
    """Never silently downgrades an incomplete REPAIR_REQUIRED to a
    usable decision -- the platform must not guess at fix instructions."""
    result = parse_review_response(
        json.dumps(
            {"decision": "REPAIR_REQUIRED", "summary": "bad", "issues": ["x"], "repair_instructions": ""}
        )
    )
    assert result.decision is None
    assert "repair_instructions" in result.parse_error


def test_issues_must_be_a_list_of_strings():
    result = parse_review_response(json.dumps({"decision": "ACCEPT", "summary": "ok", "issues": [1, 2]}))
    assert result.decision is None


def test_summary_must_be_a_string_if_present():
    result = parse_review_response(json.dumps({"decision": "ACCEPT", "summary": 123, "issues": []}))
    assert result.decision is None


def test_accept_with_missing_issues_defaults_to_empty_list():
    result = parse_review_response(json.dumps({"decision": "ACCEPT", "summary": "ok"}))
    assert result.decision == ReviewDecision.ACCEPT
    assert result.issues == []


def test_empty_string_is_invalid_not_a_crash():
    result = parse_review_response("")
    assert result.decision is None
    assert result.parse_error is not None


# -- prompt context rendering -------------------------------------------------


def test_reviewer_extra_context_includes_artifact_content_and_hash():
    context = build_reviewer_extra_context(
        candidate_text="def is_even(n): return n % 2 == 0",
        candidate_artifact_id="artifact-123",
        candidate_artifact_hash="abc123",
        review_instructions="Check correctness.",
    )
    assert "def is_even(n)" in context
    assert "artifact-123" in context
    assert "abc123" in context
    assert "Check correctness." in context
    assert "ACCEPT" in context and "REPAIR_REQUIRED" in context  # instructs the contract


def test_repair_extra_context_includes_previous_candidate_and_issues():
    context = build_repair_extra_context(
        previous_candidate_text="def isEven(n): return n % 2 == 0",
        issues=["wrong function name"],
        repair_instructions="Rename to is_even.",
    )
    assert "def isEven(n)" in context
    assert "wrong function name" in context
    assert "Rename to is_even." in context
