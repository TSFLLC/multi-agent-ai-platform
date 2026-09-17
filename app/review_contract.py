"""Reviewer/repair prompt context + structured review-response parsing — MA4.

A reviewer Agent's own system prompt (its Prompt Version content, e.g. "You
are the Code Reviewer...") is authored per-Agent and, being a starter/user
Agent, never assumes this platform's JSON response contract on its own.
This module supplies the *platform's* half of that contract as prompt
context (``build_reviewer_extra_context``/``build_repair_extra_context``,
consumed by ``app.prompt_builder.build_prompt``'s ``extra_context``
parameter) independent of which Agent Version is used as reviewer — the
orchestration layer never hard-codes a specific Agent's name/prompt.

``parse_review_response`` never raises and never infers ACCEPT from casual
prose: anything that doesn't match the required shape comes back with
``decision=None`` and a ``parse_error``, which callers must treat as a
fail-safe INVALID, never as an implicit approval.
"""

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.db.enums import ReviewDecision

_REVIEW_INSTRUCTIONS = """\
Respond with a single JSON object and nothing else — no markdown formatting, \
no prose before or after it. The JSON object must have exactly these fields:

{
  "decision": "ACCEPT" or "REPAIR_REQUIRED",
  "summary": "one or two sentence summary of your assessment",
  "issues": ["specific issue 1", "specific issue 2"],
  "repair_instructions": "specific, actionable fix instructions -- required \
and non-empty when decision is REPAIR_REQUIRED, otherwise an empty string"
}

"issues" must be a JSON array of strings (an empty array if you found none). \
Do not accept content that does not do what was asked, and do not request \
repair for purely stylistic preferences beyond what was asked."""


def build_reviewer_extra_context(
    *,
    candidate_text: str,
    candidate_artifact_id: str,
    candidate_artifact_hash: str,
    review_instructions: Optional[str] = None,
) -> str:
    parts = [
        "--- CANDIDATE ARTIFACT TO REVIEW ---",
        f"artifact_id: {candidate_artifact_id}",
        f"artifact_sha256: {candidate_artifact_hash}",
        "",
        candidate_text,
        "",
        "--- REVIEW INSTRUCTIONS ---",
        review_instructions or "Review the candidate artifact above against the task objective.",
        "",
        _REVIEW_INSTRUCTIONS,
    ]
    return "\n".join(parts)


def build_repair_extra_context(
    *, previous_candidate_text: str, issues: List[str], repair_instructions: Optional[str]
) -> str:
    parts = [
        "--- YOUR PREVIOUS CANDIDATE (rejected by review) ---",
        previous_candidate_text,
        "",
        "--- ISSUES RAISED BY THE REVIEWER ---",
    ]
    if issues:
        parts.extend(f"- {issue}" for issue in issues)
    else:
        parts.append("(no specific issues listed)")
    parts.extend(
        [
            "",
            "--- REPAIR INSTRUCTIONS ---",
            repair_instructions or "Address the issues above.",
            "",
            (
                "Produce a corrected, complete replacement for the candidate artifact. "
                "Return only the corrected content -- no explanation of what changed."
            ),
        ]
    )
    return "\n".join(parts)


@dataclass
class ParsedReview:
    decision: Optional[ReviewDecision]
    summary: Optional[str] = None
    issues: List[str] = field(default_factory=list)
    repair_instructions: Optional[str] = None
    parse_error: Optional[str] = None


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_MAX_PARSE_ERROR_LEN = 300


def parse_review_response(raw_text: str) -> ParsedReview:
    """Pure function, no I/O — unit-testable without a live provider.
    Tolerates a ```json ... ``` code fence around the object (a common
    model habit) but otherwise requires the exact contract; never raises."""
    text = _CODE_FENCE_RE.sub("", raw_text.strip()).strip()

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return ParsedReview(
            decision=None, parse_error=f"reviewer response was not valid JSON: {exc}"[:_MAX_PARSE_ERROR_LEN]
        )

    if not isinstance(payload, dict):
        return ParsedReview(decision=None, parse_error="reviewer response JSON was not an object.")

    raw_decision = payload.get("decision")
    if not isinstance(raw_decision, str):
        return ParsedReview(
            decision=None, parse_error="reviewer response is missing a string 'decision' field."
        )

    decision_map = {"ACCEPT": ReviewDecision.ACCEPT, "REPAIR_REQUIRED": ReviewDecision.REPAIR_REQUIRED}
    decision = decision_map.get(raw_decision.strip().upper())
    if decision is None:
        return ParsedReview(
            decision=None,
            parse_error=(
                f"reviewer response 'decision' must be ACCEPT or REPAIR_REQUIRED, got {raw_decision!r}."
            )[:_MAX_PARSE_ERROR_LEN],
        )

    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        return ParsedReview(decision=None, parse_error="reviewer response 'summary' must be a string.")

    raw_issues = payload.get("issues", [])
    if not isinstance(raw_issues, list) or not all(isinstance(i, str) for i in raw_issues):
        return ParsedReview(
            decision=None, parse_error="reviewer response 'issues' must be a list of strings."
        )

    repair_instructions = payload.get("repair_instructions")
    if repair_instructions is not None and not isinstance(repair_instructions, str):
        return ParsedReview(
            decision=None, parse_error="reviewer response 'repair_instructions' must be a string."
        )

    if decision == ReviewDecision.REPAIR_REQUIRED and not (
        repair_instructions and repair_instructions.strip()
    ):
        return ParsedReview(
            decision=None,
            parse_error="reviewer response decision is REPAIR_REQUIRED but 'repair_instructions' was empty.",
        )

    return ParsedReview(
        decision=decision, summary=summary, issues=raw_issues, repair_instructions=repair_instructions
    )
