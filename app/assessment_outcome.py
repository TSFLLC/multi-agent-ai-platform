"""AIL.5C outcome aggregation — code, never the model.

Pure functions: no database, no model calls. Given the deterministic criterion
results and (when judgment criteria exist) the merged Grader judgments, decide:

* the outcome (``PASSED | NEEDS_WORK | PROVISIONAL | HUMAN_REVIEW_REQUIRED |
  UNABLE_TO_ASSESS``), or ``None`` while grading is still pending;
* the demonstration effect (kept apart from the outcome so "PASSED" stays
  honest for assisted work);
* gaps and a deterministic remediation map (no dead ends).

Hard rules encoded here:

1. A required deterministic criterion that is not met forces NEEDS_WORK, no
   matter what any model says (the Grader is not even consulted).
2. The Grader never decides a deterministic fact and never sets state.
3. A confident, cross-confirmed failure decides NEEDS_WORK; disagreement is
   HUMAN_REVIEW_REQUIRED; low confidence or a missing cross-check is
   PROVISIONAL. Neither ever produces evidence.
4. AI judgment alone never establishes a DEMONSTRATED-eligible effect for
   assisted or self-declared-AI work.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.db.enums import (
    AssessmentOutcome,
    DemonstrationEffect,
    EvaluationFinding,
    ExecutionVerification,
    GraderConfidence,
)
from app.services import independence_policy as policy

MET, PARTIAL, NOT_MET, NOT_APPLICABLE = (
    EvaluationFinding.MET.value,
    EvaluationFinding.PARTIAL.value,
    EvaluationFinding.NOT_MET.value,
    EvaluationFinding.NOT_APPLICABLE.value,
)

# deterministic check types that prove real platform execution happened
EXECUTION_CHECK_TYPES = frozenset(
    {
        "run_exists_owned_terminal",
        "run_after_challenge_start",
        "inputs_match_challenge",
        "evaluation_run_findings",
        "experiment_concluded",
    }
)

# grading round status values (owned by the grader service)
GRADING_NOT_NEEDED = "not_needed"
GRADING_COMPLETE = "complete"
GRADING_TRANSIENT_FAILURE = "transient_failure"
GRADING_UNUSABLE = "unusable"
GRADING_CROSSCHECK_UNAVAILABLE = "crosscheck_unavailable"

DEFAULT_NEXT_STEP = {"kind": "retry", "label": "Try again with a new challenge after the cooldown"}


@dataclass
class Aggregate:
    outcome: Optional[AssessmentOutcome]
    effect: DemonstrationEffect
    gaps: List[Dict[str, Any]] = field(default_factory=list)
    remediation: List[Dict[str, Any]] = field(default_factory=list)
    reason_code: str = ""
    execution_verification: ExecutionVerification = ExecutionVerification.NOT_APPLICABLE
    independence: Dict[str, Any] = field(default_factory=dict)


def _is_failure(finding: str) -> bool:
    return finding in (PARTIAL, NOT_MET)


def judged_agreement(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge one criterion's judgments from up to two runs into a single row.

    ``agreement``: ``single`` (one run), ``agree`` or ``disagree``. The merged
    ``confidence`` is the LOWEST of the runs' — categorical, never a mean.
    """
    order = {GraderConfidence.LOW.value: 0, GraderConfidence.MEDIUM.value: 1, GraderConfidence.HIGH.value: 2}
    first = runs[0]
    merged = dict(first)
    merged["runs"] = runs
    merged["confidence"] = min((r["confidence"] for r in runs), key=lambda c: order[c])
    if len(runs) == 1:
        merged["agreement"] = "single"
    else:
        merged["agreement"] = "agree" if len({r["finding"] for r in runs}) == 1 else "disagree"
    return merged


def source_work_summary(manifest: Dict[str, Any], challenge_issued: bool, attestation: Optional[dict]) -> Dict[str, Any]:
    """Independence facts, re-derived from platform records already in the
    frozen manifest (never from the learner's claim)."""
    work = manifest.get("source_work") or {}
    levels = [l for l in work.get("levels", [])]
    from app.db.enums import AssistanceLevel

    classes = sorted({policy.classify_assistance(AssistanceLevel(l)) for l in levels}) if levels else []
    top = max((policy.assistance_rank(AssistanceLevel(l)) or 0 for l in levels), default=None)
    return {
        "source_levels": levels,
        "source_max_assistance": ("h%d" % top) if top is not None else None,
        "source_classes": classes,
        "study_mode_used": bool(work.get("study_mode_used")),
        "challenge_issued": challenge_issued,
        "independence_basis": "assessment_mode" if challenge_issued else "source_work",
        "declaration": (attestation or {}).get("declaration"),
    }


def _effect(independence: Dict[str, Any], verified: bool, evidence_is_execution: bool, attestation: Optional[dict]) -> DemonstrationEffect:
    if policy.attestation_is_formative(attestation):
        return DemonstrationEffect.FORMATIVE_ONLY
    if independence["challenge_issued"]:
        # Assessment Mode: the Mentor was locked, the challenge was fresh.
        if evidence_is_execution and not verified:
            return DemonstrationEffect.COUNTS_TOWARD_PRACTICED_ONLY
        return DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED
    classes = set(independence["source_classes"])
    if policy.FORMATIVE in classes:
        levels = set(independence["source_levels"])
        if "h5" in levels:
            return DemonstrationEffect.FORMATIVE_ONLY
        return DemonstrationEffect.COUNTS_TOWARD_PRACTICED_ONLY
    if policy.PARTIAL in classes:
        return DemonstrationEffect.COUNTS_TOWARD_PRACTICED_ONLY
    if evidence_is_execution and not verified:
        return DemonstrationEffect.COUNTS_TOWARD_PRACTICED_ONLY
    return DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED


def _gap(criterion: Dict[str, Any], source: str) -> Dict[str, Any]:
    return {
        "criterion_key": criterion["key"],
        "label": criterion["label"],
        "source": source,
        "finding": criterion["finding"],
        "detail": criterion.get("gap") or criterion.get("detail") or "",
    }


def aggregate(
    *,
    deterministic: List[Dict[str, Any]],
    judged: List[Dict[str, Any]],
    grading_status: str,
    crosscheck_required: bool,
    independence: Dict[str, Any],
    attestation: Optional[dict],
    evidence_is_execution: bool,
    definition_criteria: List[Dict[str, Any]],
) -> Aggregate:
    """``deterministic``/``judged`` rows carry ``key,label,required,finding``
    (judged rows also ``confidence``/``agreement``)."""
    by_key = {c["key"]: c for c in definition_criteria}
    verified = any(
        d["finding"] == MET and d.get("check_type") in EXECUTION_CHECK_TYPES for d in deterministic
    )
    verification = (
        ExecutionVerification.PLATFORM_VERIFIED if verified else ExecutionVerification.NOT_APPLICABLE
    )
    result = Aggregate(
        outcome=None,
        effect=DemonstrationEffect.NONE,
        execution_verification=verification,
        independence=independence,
    )

    required_det = [d for d in deterministic if d["required"]]
    det_failed = [d for d in required_det if _is_failure(d["finding"])]
    gaps = [_gap(d, "platform") for d in deterministic if _is_failure(d["finding"])]

    def finish(outcome: AssessmentOutcome, reason: str, extra_gaps: Optional[List[dict]] = None) -> Aggregate:
        result.outcome = outcome
        result.reason_code = reason
        result.gaps = gaps + (extra_gaps or [])
        result.remediation = remediation_for(result.gaps, by_key)
        if outcome == AssessmentOutcome.PASSED:
            result.effect = _effect(independence, verified, evidence_is_execution, attestation)
        else:
            result.effect = DemonstrationEffect.NONE
        return result

    # Rule 1: deterministic failure is final — the Grader is never consulted.
    if det_failed:
        return finish(AssessmentOutcome.NEEDS_WORK, "required_deterministic_criterion_not_met")

    required_judged = [j for j in judged if j["required"]]
    if not required_judged and grading_status in (GRADING_NOT_NEEDED, GRADING_COMPLETE):
        return finish(AssessmentOutcome.PASSED, "all_required_criteria_met")
    if grading_status == GRADING_NOT_NEEDED:
        return finish(AssessmentOutcome.PASSED, "all_required_criteria_met")

    if grading_status == GRADING_TRANSIENT_FAILURE:
        return result  # outcome None: the attempt stays AWAITING_GRADING, retry is safe
    if grading_status == GRADING_UNUSABLE:
        return finish(AssessmentOutcome.UNABLE_TO_ASSESS, "grader_output_unusable")

    judged_gaps = [_gap(j, "grader") for j in judged if _is_failure(j["finding"])]
    if grading_status == GRADING_CROSSCHECK_UNAVAILABLE:
        return finish(AssessmentOutcome.PROVISIONAL, "crosscheck_unavailable", judged_gaps)

    confident_failure = [
        j for j in required_judged if _is_failure(j["finding"]) and j["agreement"] != "disagree" and j["confidence"] != GraderConfidence.LOW.value
    ]
    if confident_failure:
        return finish(AssessmentOutcome.NEEDS_WORK, "required_judgment_not_met", judged_gaps)
    if any(j["agreement"] == "disagree" for j in required_judged):
        return finish(AssessmentOutcome.HUMAN_REVIEW_REQUIRED, "graders_disagree", judged_gaps)
    if any(j["confidence"] == GraderConfidence.LOW.value for j in required_judged):
        return finish(AssessmentOutcome.PROVISIONAL, "low_confidence", judged_gaps)
    if crosscheck_required and any(j["agreement"] == "single" for j in required_judged):
        return finish(AssessmentOutcome.PROVISIONAL, "crosscheck_unavailable", judged_gaps)
    return finish(AssessmentOutcome.PASSED, "all_required_criteria_met", judged_gaps)


def remediation_for(gaps: List[Dict[str, Any]], criteria_by_key: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deterministic remediation — authored per criterion, never chosen by a
    model. Always at least one next step for a non-pass (no dead ends)."""
    steps: List[Dict[str, Any]] = []
    seen = set()
    for gap in gaps:
        criterion = criteria_by_key.get(gap["criterion_key"], {})
        for step in criterion.get("on_not_met") or []:
            marker = (step.get("kind"), step.get("ref"))
            if marker not in seen:
                seen.add(marker)
                steps.append({**step, "for_criterion": gap["criterion_key"]})
    if not steps or not any(s.get("kind") == "retry" for s in steps):
        steps.append(dict(DEFAULT_NEXT_STEP))
    return steps
