"""Evaluator prompt context + structured evaluation-response parsing — MA6
Slice 3A.

Mirrors ``app.review_contract``'s shape and fail-safe philosophy applied to
model-based evaluation instead of review: this module supplies the
*platform's* half of the evaluator response contract as prompt context
(``build_evaluator_extra_context``, consumed by
``app.prompt_builder.build_prompt``'s ``extra_context`` parameter)
independent of which published AgentVersion is used as evaluator — the
orchestration layer never hard-codes a specific Agent's name/prompt, and
the evaluator's own authored system prompt never needs to already know
this platform's JSON contract on its own.

``parse_evaluation_response`` never raises and never infers/fabricates a
finding for a criterion it couldn't validate: anything that doesn't match
the required shape comes back with ``findings=[]`` and a ``parse_error``,
which callers must treat as a failed Evaluation Run, never as an implicit
MET. An evaluator response must cover EXACTLY the criteria bound to the
immutable ``EvaluationDefinitionVersion`` it was asked to evaluate against
— no missing, no duplicate, no unknown criterion key — so an evaluator can
never silently invent or drop rubric criteria (MA6 non-negotiable
invariant). No aggregate score/percentage/rank field exists anywhere in
this contract, on either side (request or response).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.db.enums import EvaluationFinding

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_MAX_PARSE_ERROR_LEN = 300

_FINDING_MAP = {
    "MET": EvaluationFinding.MET,
    "PARTIAL": EvaluationFinding.PARTIAL,
    "NOT_MET": EvaluationFinding.NOT_MET,
    "NOT_APPLICABLE": EvaluationFinding.NOT_APPLICABLE,
}


# -- prompt context rendering ---------------------------------------------------


def _render_criterion_line(criterion: Dict[str, Any]) -> str:
    line = f"- key={criterion['key']!r} label={criterion['label']!r}"
    description = criterion.get("description")
    if description:
        line += f" description={description!r}"
    return line


def _build_evaluation_instructions(criteria: List[Dict[str, Any]]) -> str:
    keys_repr = ", ".join(repr(c["key"]) for c in criteria)
    return (
        "Respond with a single JSON object and nothing else -- no markdown formatting, "
        "no prose before or after it. The JSON object must have exactly this field:\n\n"
        "{\n"
        '  "criteria": [\n'
        "    {\n"
        '      "key": "<criterion key, exactly as given above>",\n'
        '      "finding": "MET" or "PARTIAL" or "NOT_MET" or "NOT_APPLICABLE",\n'
        '      "rationale": "one or two sentence justification for this finding",\n'
        '      "evidence": [{"quote": "short exact quote from the candidate output", '
        '"criterion_key": "<same key>"}]\n'
        "    }\n"
        "    ...\n"
        "  ]\n"
        "}\n\n"
        "You MUST include exactly one entry for each of the following criterion keys, "
        f"and no others: {keys_repr}.\n\n"
        '"evidence" is optional (an empty array if there is nothing to quote), but '
        "when present, every entry's \"criterion_key\" must equal that same criterion's "
        'own "key". Use "NOT_APPLICABLE" when this criterion cannot be assessed from '
        "the candidate output at all -- never guess. Do not provide an overall score, "
        "percentage, ranking, or recommendation of any kind -- only per-criterion findings."
    )


def build_evaluator_extra_context(
    *,
    subject_task_title: str,
    subject_task_description: Optional[str],
    subject_task_requirements: Optional[str],
    candidate_text: str,
    candidate_artifact_id: str,
    candidate_artifact_hash: str,
    criteria: List[Dict[str, Any]],
) -> str:
    """``criteria``: list of ``{"key", "label", "description"?}``, in the
    order they should appear to the evaluator (the immutable
    ``EvaluationDefinitionVersion``'s own criteria order)."""
    parts = ["--- SUBJECT TASK ---", subject_task_title]
    if subject_task_description:
        parts.append(subject_task_description)
    if subject_task_requirements:
        parts.append(subject_task_requirements)
    parts.extend(
        [
            "",
            "--- CANDIDATE OUTPUT TO EVALUATE ---",
            f"artifact_id: {candidate_artifact_id}",
            f"artifact_sha256: {candidate_artifact_hash}",
            "",
            candidate_text,
            "",
            "--- EVALUATION RUBRIC ---",
        ]
    )
    parts.extend(_render_criterion_line(c) for c in criteria)
    parts.extend(["", _build_evaluation_instructions(criteria)])
    return "\n".join(parts)


# -- structured-response parsing -------------------------------------------------


@dataclass
class ParsedEvidence:
    quote: str
    criterion_key: str


@dataclass
class ParsedCriterionFinding:
    key: str
    finding: EvaluationFinding
    rationale: str
    evidence: List[ParsedEvidence] = field(default_factory=list)


@dataclass
class ParsedEvaluation:
    findings: List[ParsedCriterionFinding] = field(default_factory=list)
    parse_error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.parse_error is None


def parse_evaluation_response(raw_text: str, *, expected_criterion_keys: List[str]) -> ParsedEvaluation:
    """Pure function, no I/O — unit-testable without a live provider.
    Tolerates a ```json ... ``` code fence around the object (a common
    model habit) but otherwise requires the exact contract; never raises.
    Rejects the entire response (returns ``findings=[]`` and a
    ``parse_error``) on any structural violation -- never partially trusts
    a malformed response by keeping the criteria that did parse."""
    text = _CODE_FENCE_RE.sub("", raw_text.strip()).strip()

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return ParsedEvaluation(
            parse_error=f"evaluator response was not valid JSON: {exc}"[:_MAX_PARSE_ERROR_LEN]
        )

    if not isinstance(payload, dict):
        return ParsedEvaluation(parse_error="evaluator response JSON was not an object.")

    raw_criteria = payload.get("criteria")
    if not isinstance(raw_criteria, list):
        return ParsedEvaluation(parse_error="evaluator response is missing a 'criteria' array.")

    expected_set = set(expected_criterion_keys)
    seen_keys: set = set()
    findings: List[ParsedCriterionFinding] = []

    for index, entry in enumerate(raw_criteria):
        if not isinstance(entry, dict):
            return ParsedEvaluation(parse_error=f"evaluator response criteria[{index}] was not an object.")

        raw_key = entry.get("key")
        if not isinstance(raw_key, str) or not raw_key.strip():
            return ParsedEvaluation(
                parse_error=f"evaluator response criteria[{index}] is missing a string 'key'."
            )
        key = raw_key.strip()

        if key not in expected_set:
            return ParsedEvaluation(
                parse_error=(
                    f"evaluator response referenced unknown criterion key {key!r} -- not part of this "
                    "Evaluation Definition Version."
                )[:_MAX_PARSE_ERROR_LEN]
            )
        if key in seen_keys:
            return ParsedEvaluation(parse_error=f"evaluator response duplicated criterion key {key!r}.")
        seen_keys.add(key)

        raw_finding = entry.get("finding")
        if not isinstance(raw_finding, str):
            return ParsedEvaluation(
                parse_error=f"evaluator response criterion {key!r} is missing a string 'finding'."
            )
        finding = _FINDING_MAP.get(raw_finding.strip().upper())
        if finding is None:
            return ParsedEvaluation(
                parse_error=(
                    f"evaluator response criterion {key!r} has invalid finding {raw_finding!r} "
                    "(must be one of MET, PARTIAL, NOT_MET, NOT_APPLICABLE)."
                )[:_MAX_PARSE_ERROR_LEN]
            )

        raw_rationale = entry.get("rationale")
        if not isinstance(raw_rationale, str) or not raw_rationale.strip():
            return ParsedEvaluation(
                parse_error=f"evaluator response criterion {key!r} is missing a non-empty string 'rationale'."
            )
        rationale = raw_rationale.strip()

        raw_evidence = entry.get("evidence", [])
        if not isinstance(raw_evidence, list):
            return ParsedEvaluation(
                parse_error=f"evaluator response criterion {key!r} 'evidence' must be a list."
            )
        evidence: List[ParsedEvidence] = []
        for ev_index, ev_entry in enumerate(raw_evidence):
            if not isinstance(ev_entry, dict):
                return ParsedEvaluation(
                    parse_error=f"evaluator response criterion {key!r} evidence[{ev_index}] was not an object."
                )
            raw_quote = ev_entry.get("quote")
            if not isinstance(raw_quote, str) or not raw_quote.strip():
                return ParsedEvaluation(
                    parse_error=(
                        f"evaluator response criterion {key!r} evidence[{ev_index}] is missing a "
                        "non-empty string 'quote'."
                    )
                )
            raw_ev_key = ev_entry.get("criterion_key")
            if not isinstance(raw_ev_key, str) or not raw_ev_key.strip():
                return ParsedEvaluation(
                    parse_error=(
                        f"evaluator response criterion {key!r} evidence[{ev_index}] is missing a string "
                        "'criterion_key'."
                    )
                )
            if raw_ev_key.strip() != key:
                return ParsedEvaluation(
                    parse_error=(
                        f"evaluator response criterion {key!r} evidence[{ev_index}] has "
                        f"criterion_key={raw_ev_key.strip()!r}, which does not match the criterion it "
                        "belongs to."
                    )
                )
            evidence.append(ParsedEvidence(quote=raw_quote.strip(), criterion_key=key))

        findings.append(
            ParsedCriterionFinding(key=key, finding=finding, rationale=rationale, evidence=evidence)
        )

    missing = expected_set - seen_keys
    if missing:
        return ParsedEvaluation(
            parse_error=f"evaluator response is missing required criteria: {sorted(missing)}."
        )

    return ParsedEvaluation(findings=findings)
