"""AIL.5C contracts: the Grader's strict structured response, assessment
definition validation, and canonical hashing.

Pure functions only — no database, no model calls. Fail-safe by design (the
same philosophy as ``app.evaluation_contract.parse_evaluation_response``): a
response is either exactly the contract or it is rejected in full; the
platform never infers a finding the model did not state.

``grading_contract_v1`` response:

    {"criteria": [{
        "key": "...",                       # exactly the grader-method keys
        "finding": "met|partial|not_met|not_applicable",
        "confidence": "high|medium|low",    # categorical, never a probability
        "rationale": "...",
        "quotes": ["verbatim substring of the learner response", ...],
        "gap": "what is missing" | null     # judgment about the work, never advice
    }]}

The Grader may not: score, rank, advise, coach, name a learner state, or judge
a criterion it was not given (in particular never a deterministic one).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from app.db.enums import AssessmentKind, EvaluationFinding, EvidenceType, GraderConfidence

GRADING_CONTRACT_VERSION = "grading_contract_v1"

MAX_QUOTES_PER_CRITERION = 3
MAX_QUOTE_CHARS = 400
MAX_RATIONALE_CHARS = 1200
MAX_GAP_CHARS = 400

_ALLOWED_CRITERION_FIELDS = {"key", "finding", "confidence", "rationale", "quotes", "gap"}
_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
# The Grader judges the work. It never coaches and never speaks about state.
_FORBIDDEN_TEXT = re.compile(
    r"\b(mastery|mastered|certified|accredited|professional-level|demonstrated (?:the )?concept|learner state|you should|i recommend|"
    r"next steps?|try to|consider (?:adding|using|reviewing))\b",
    re.IGNORECASE,
)


class GradingResponseInvalid(ValueError):
    """The Grader's output did not satisfy ``grading_contract_v1``."""


@dataclass(frozen=True)
class GradedCriterion:
    key: str
    finding: EvaluationFinding
    confidence: GraderConfidence
    rationale: str
    quotes: List[str]
    gap: Optional[str]

    def to_json(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "finding": self.finding.value,
            "confidence": self.confidence.value,
            "rationale": self.rationale,
            "quotes": list(self.quotes),
            "gap": self.gap,
        }


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_grading_response(
    raw_text: str, *, expected_keys: Iterable[str], response_text: str
) -> List[GradedCriterion]:
    """Validate the Grader's raw output. Raises ``GradingResponseInvalid``."""
    expected: List[str] = list(expected_keys)
    text = (raw_text or "").strip()
    fenced = _JSON_FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        raise GradingResponseInvalid("response is not valid JSON")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"criteria"}
        or not isinstance(payload["criteria"], list)
    ):
        raise GradingResponseInvalid("response must be exactly an object with a 'criteria' array")

    haystack = _squash(response_text)
    results: List[GradedCriterion] = []
    seen: Set[str] = set()
    for item in payload["criteria"]:
        if not isinstance(item, dict) or set(item) != _ALLOWED_CRITERION_FIELDS:
            raise GradingResponseInvalid("every criterion must have exactly the contract fields")
        key = item["key"]
        if not isinstance(key, str) or key in seen:
            raise GradingResponseInvalid("missing, non-string or duplicate criterion key")
        seen.add(key)
        try:
            finding = EvaluationFinding(item["finding"])
            confidence = GraderConfidence(item["confidence"])
        except ValueError:
            raise GradingResponseInvalid(f"criterion {key!r}: invalid finding or confidence")
        rationale = item["rationale"]
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > MAX_RATIONALE_CHARS:
            raise GradingResponseInvalid(f"criterion {key!r}: rationale missing or too long")
        gap = item["gap"]
        if gap is not None and (not isinstance(gap, str) or not gap.strip() or len(gap) > MAX_GAP_CHARS):
            raise GradingResponseInvalid(f"criterion {key!r}: invalid gap")
        if finding in (EvaluationFinding.MET, EvaluationFinding.NOT_APPLICABLE) and gap is not None:
            raise GradingResponseInvalid(f"criterion {key!r}: a gap may only accompany partial or not_met")
        if finding in (EvaluationFinding.PARTIAL, EvaluationFinding.NOT_MET) and gap is None:
            raise GradingResponseInvalid(f"criterion {key!r}: partial or not_met must state the gap")
        for field_text in (rationale, gap or ""):
            if _FORBIDDEN_TEXT.search(field_text):
                raise GradingResponseInvalid(f"criterion {key!r}: coaching or state language is not allowed")

        quotes = item["quotes"]
        if not isinstance(quotes, list) or len(quotes) > MAX_QUOTES_PER_CRITERION:
            raise GradingResponseInvalid(f"criterion {key!r}: quotes must be a short array")
        for quote in quotes:
            if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
                raise GradingResponseInvalid(f"criterion {key!r}: invalid quote")
            if _squash(quote) not in haystack:
                raise GradingResponseInvalid(
                    f"criterion {key!r}: quote is not verbatim from the learner response"
                )
        if finding == EvaluationFinding.NOT_APPLICABLE and quotes:
            raise GradingResponseInvalid(f"criterion {key!r}: not_applicable takes no quotes")
        if finding in (EvaluationFinding.MET, EvaluationFinding.PARTIAL) and haystack and not quotes:
            raise GradingResponseInvalid(f"criterion {key!r}: a judgment must cite the learner's own words")

        results.append(GradedCriterion(key, finding, confidence, rationale.strip(), list(quotes), gap))

    if seen != set(expected) or len(results) != len(expected):
        raise GradingResponseInvalid("criterion keys must be exactly the requested grader criteria")
    order = {key: i for i, key in enumerate(expected)}
    return sorted(results, key=lambda r: order[r.key])


# -- canonical hashing ----------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def sha256_hex(value: Any) -> str:
    data = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def review_fingerprint(result_id: str, manifest_hash: Optional[str], submission_hash: Optional[str]) -> str:
    """Binds a human review to the exact result, manifest and submission the
    reviewer sees (the Approval action-fingerprint pattern)."""
    return sha256_hex({"result": result_id, "manifest": manifest_hash, "submission": submission_hash})


# -- definition validation --------------------------------------------------------------

KIND_TO_EVIDENCE = {
    AssessmentKind.KNOWLEDGE_CHECK: EvidenceType.KNOWLEDGE_CHECK,
    AssessmentKind.EXPLAIN_BACK: EvidenceType.EXPLAIN_BACK,
    AssessmentKind.MODIFICATION: EvidenceType.MODIFICATION,
    AssessmentKind.REPRODUCTION: EvidenceType.REPRODUCTION,
    AssessmentKind.DEBUGGING: EvidenceType.DEBUGGING,
    AssessmentKind.EXPERIMENT_INTERPRETATION: EvidenceType.INTERPRETATION,
    AssessmentKind.PROJECT: EvidenceType.PROJECT_ASSESSMENT,
    AssessmentKind.CAPSTONE: EvidenceType.PROJECT_ASSESSMENT,
}

METHODS = ("deterministic", "grader")
CROSSCHECK_MODES = ("deciding", "always", "never")
FRESH_POLICIES = ("always", "if_assisted", "never")
_KEY = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
MIN_POOL_MULTIPLIER = 3
CAPSTONE_MIN_DETERMINISTIC_SHARE = 0.7

DEFAULT_GRADING_POLICY = {"crosscheck": "deciding", "expiry_hours": 24, "cooldown_hours": 12}
DEFAULT_INDEPENDENCE_POLICY = {"fresh_required": "if_assisted"}


# Deterministic checks that verify the learner actually engaged with the issued
# challenge (rather than merely being handed one).
CHALLENGE_BOUND_CHECKS = frozenset(
    {"choice_match", "run_after_challenge_start", "inputs_match_challenge", "numeric_match"}
)


class DefinitionInvalid(ValueError):
    pass


def validate_definition(
    *,
    kind: AssessmentKind,
    produces_evidence_type: EvidenceType,
    criteria: List[dict],
    challenge_spec: Optional[dict],
    grading_policy: dict,
    independence_policy: dict,
    concept_links: List[dict],
    known_check_types: Iterable[str],
) -> None:
    """Raise ``DefinitionInvalid`` if the payload cannot be published."""
    if KIND_TO_EVIDENCE[kind] != produces_evidence_type:
        raise DefinitionInvalid(
            f"a {kind.value} assessment produces {KIND_TO_EVIDENCE[kind].value} evidence, "
            f"not {produces_evidence_type.value}"
        )
    if not concept_links:
        raise DefinitionInvalid("a definition must assess at least one Concept Version")
    if not criteria:
        raise DefinitionInvalid("a definition needs at least one criterion")

    check_types = set(known_check_types)
    keys: Set[str] = set()
    for c in criteria:
        key = c.get("key")
        if not isinstance(key, str) or not _KEY.match(key) or key in keys:
            raise DefinitionInvalid(f"criterion key {key!r} is missing, malformed or duplicated")
        keys.add(key)
        if c.get("method") not in METHODS:
            raise DefinitionInvalid(f"criterion {key!r}: method must be one of {METHODS}")
        if not str(c.get("label", "")).strip():
            raise DefinitionInvalid(f"criterion {key!r}: a plain-language label is required")
        if c["method"] == "deterministic":
            check = c.get("check")
            if not isinstance(check, dict) or check.get("type") not in check_types:
                raise DefinitionInvalid(f"criterion {key!r}: unknown or missing deterministic check type")
        else:
            points = c.get("reference_points")
            if (
                not isinstance(points, list)
                or not points
                or not all(isinstance(p, str) and p.strip() for p in points)
            ):
                raise DefinitionInvalid(
                    f"criterion {key!r}: a grader criterion needs authored reference_points"
                )
        for step in c.get("on_not_met") or []:
            if not isinstance(step, dict) or step.get("kind") not in (
                "learning_item",
                "milestone",
                "definition",
                "experiment",
                "professor",
            ):
                raise DefinitionInvalid(f"criterion {key!r}: invalid remediation step")

    required = [c for c in criteria if c.get("required", True)]
    if not required:
        raise DefinitionInvalid("at least one criterion must be required")
    grader_required = [c for c in required if c["method"] == "grader"]
    if kind == AssessmentKind.KNOWLEDGE_CHECK and any(c["method"] == "grader" for c in criteria):
        raise DefinitionInvalid("a knowledge check is scored deterministically only")

    if grading_policy.get("crosscheck", "deciding") not in CROSSCHECK_MODES:
        raise DefinitionInvalid("grading_policy.crosscheck is invalid")
    if grader_required and grading_policy.get("crosscheck", "deciding") == "never":
        raise DefinitionInvalid("a required AI-judged criterion must be cross-checked")
    if independence_policy.get("fresh_required", "if_assisted") not in FRESH_POLICIES:
        raise DefinitionInvalid("independence_policy.fresh_required is invalid")
    max_revisions = grading_policy.get("max_revisions")
    if max_revisions is not None and (
        isinstance(max_revisions, bool) or not isinstance(max_revisions, int) or not 0 <= max_revisions <= 3
    ):
        raise DefinitionInvalid("grading_policy.max_revisions must be an integer from 0 to 3, or absent")

    if kind == AssessmentKind.CAPSTONE:
        share = sum(1 for c in required if c["method"] == "deterministic") / len(required)
        if share < CAPSTONE_MIN_DETERMINISTIC_SHARE:
            raise DefinitionInvalid(
                "a capstone needs at least 70% of its required criteria to be deterministic"
            )
        if independence_policy.get("fresh_required", "always") != "always":
            raise DefinitionInvalid("a capstone always requires a fresh challenge")

    for link in concept_links:
        wanted = link.get("criterion_keys") or []
        if not wanted or not set(wanted) <= keys:
            raise DefinitionInvalid("each concept link must name existing criterion keys")
        if not any(c["key"] in wanted and c.get("required", True) for c in criteria):
            raise DefinitionInvalid("each concept link must be decided by at least one required criterion")

    if challenge_spec:
        pool = challenge_spec.get("pool")
        draw = challenge_spec.get("draw_size", 1)
        if not isinstance(pool, list) or not isinstance(draw, int) or draw < 1:
            raise DefinitionInvalid("challenge_spec needs a pool list and a positive draw_size")
        fixed = challenge_spec.get("fixed") or []
        if not isinstance(fixed, list):
            raise DefinitionInvalid("challenge_spec.fixed must be a list")
        entry_keys = [e.get("entry_key") for e in list(pool) + list(fixed) if isinstance(e, dict)]
        if (
            len(entry_keys) != len(pool) + len(fixed)
            or len(set(entry_keys)) != len(entry_keys)
            or not all(entry_keys)
        ):
            raise DefinitionInvalid("every challenge entry needs a unique entry_key")
        if len(pool) < MIN_POOL_MULTIPLIER * draw:
            raise DefinitionInvalid(
                f"the challenge pool must hold at least {MIN_POOL_MULTIPLIER}x the draw size "
                f"({MIN_POOL_MULTIPLIER * draw}); it has {len(pool)}"
            )
        # A fresh challenge only demonstrates independence if something checks it.
        bound = any(c["method"] == "grader" for c in required) or any(
            c["method"] == "deterministic" and c["check"]["type"] in CHALLENGE_BOUND_CHECKS for c in required
        )
        if not bound:
            raise DefinitionInvalid("a fresh challenge must be bound to a required criterion that checks it")
