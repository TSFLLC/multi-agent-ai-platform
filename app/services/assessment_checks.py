"""AIL.5C deterministic check catalog.

Everything code can determine, code determines: an artifact exists, a run
exists and belongs to this learner, an evaluation's findings, an experiment
was concluded, a pointer resolves, a hash still matches. A handler returns a
finding from the same vocabulary MA6 uses (``EvaluationFinding``) plus the
facts it relied on. The Grader never sees, restates or overrides these.

Ownership is always proved through the learner's own records
(``Task.created_by``, ``EvaluationRun.requested_by_user_id``,
``Experiment.user_id``, the learner's project attempt) — never through project
membership, because every learner owns the shared AIL project.

Handlers are data-driven: ``criterion["check"] = {"type": ..., ...params}``.
MA6 ``EvaluationRun`` results are consumed READ-ONLY; nothing here writes to
MA6 tables.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assessment_contract import sha256_hex
from app.db.enums import (
    AgentRunStatus,
    EvaluationFinding,
    EvaluationRunStatus,
    ExperimentStatus,
    MilestoneAttemptStatus,
)
from app.models.artifacts_eval import Artifact
from app.models.assessment import AssessmentAttempt, AssessmentDefinition
from app.models.evaluation_runs import EvaluationRun
from app.models.execution import ModelCall
from app.models.lab import Experiment
from app.models.learner import LearningEvidence
from app.models.tasks import Task, TaskRun
from app.services.assessment_experiment_facts import experiment_facts
from app.services.independence_policy import counts_toward_practiced
from app.services.project_evidence_verification import _owned_agent_run

MET, PARTIAL, NOT_MET, NOT_APPLICABLE = (
    EvaluationFinding.MET,
    EvaluationFinding.PARTIAL,
    EvaluationFinding.NOT_MET,
    EvaluationFinding.NOT_APPLICABLE,
)


class CheckUnavailable(RuntimeError):
    """The platform cannot evaluate a check right now (not the learner's fault):
    the attempt becomes UNABLE_TO_ASSESS rather than failing the learner."""


@dataclass
class CheckOutcome:
    finding: EvaluationFinding
    detail: str
    facts: Dict[str, Any] = field(default_factory=dict)
    refs: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class CheckContext:
    db: Session
    user_id: str
    attempt: AssessmentAttempt
    definition: AssessmentDefinition
    manifest: Dict[str, Any]
    submission: Dict[str, Any]
    challenge: Dict[str, Any]


def aware(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def dig(value: Any, path: str, default: Any = None) -> Any:
    """Dotted-path lookup into nested dicts (lists are not traversed)."""
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _ids(ctx: CheckContext, field_name: str) -> List[str]:
    raw = dig(ctx.submission, field_name)
    if isinstance(raw, str):
        raw = [raw]
    return [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []


# -- content checks ----------------------------------------------------------------------


def check_choice_match(ctx: CheckContext, spec: dict) -> CheckOutcome:
    items = ctx.challenge.get("items") or []
    keys = (ctx.challenge.get("server_only") or {}).get("answer_keys") or {}
    if not items:
        raise CheckUnavailable("no drawn question items were issued")
    correct = 0
    per_item = {}
    for item in items:
        key = item["entry_key"]
        chosen = dig(ctx.submission, f"responses.{key}.selected")
        ok = isinstance(chosen, list) and set(chosen) == set(keys.get(key, []))
        per_item[key] = bool(ok)
        correct += 1 if ok else 0
    need = len(items) if spec.get("min_correct", "all") == "all" else int(spec["min_correct"])
    finding = MET if correct >= need else NOT_MET
    return CheckOutcome(
        finding,
        f"{correct} of {len(items)} answered correctly (needed {need})",
        {"correct": correct, "total": len(items), "needed": need, "per_item": per_item},
    )


def check_length_bounds(ctx: CheckContext, spec: dict) -> CheckOutcome:
    text = _text(dig(ctx.submission, spec["field"]))
    low, high = int(spec.get("min_chars", 1)), int(spec.get("max_chars", 20000))
    ok = low <= len(text) <= high
    return CheckOutcome(
        MET if ok else NOT_MET, f"{len(text)} characters (allowed {low}-{high})", {"chars": len(text)}
    )


def check_section_present(ctx: CheckContext, spec: dict) -> CheckOutcome:
    text = _text(dig(ctx.submission, spec["field"]))
    wanted = list(spec.get("sections") or [])
    present = []
    for name in wanted:
        pattern = re.compile(rf"^\s*(#+\s*|\*\*)?{re.escape(name)}\b", re.IGNORECASE | re.MULTILINE)
        if pattern.search(text):
            present.append(name)
    if wanted and len(present) == len(wanted):
        finding = MET
    elif present:
        finding = PARTIAL
    else:
        finding = NOT_MET
    return CheckOutcome(
        finding, f"{len(present)} of {len(wanted)} sections present", {"present": present, "wanted": wanted}
    )


def check_schema_valid(ctx: CheckContext, spec: dict) -> CheckOutcome:
    value = dig(ctx.submission, spec["field"])
    types = {"string": str, "number": (int, float), "list": list, "boolean": bool}
    missing, wrong = [], []
    for key in spec.get("required_keys") or []:
        if not isinstance(value, dict) or key not in value:
            missing.append(key)
    for key, kind in (spec.get("types") or {}).items():
        if isinstance(value, dict) and key in value and not isinstance(value[key], types[kind]):
            wrong.append(key)
    ok = isinstance(value, dict) and not missing and not wrong
    return CheckOutcome(
        MET if ok else NOT_MET,
        "structure is valid" if ok else "structure is incomplete",
        {"missing": missing, "wrong_type": wrong},
    )


def check_numeric_match(ctx: CheckContext, spec: dict) -> CheckOutcome:
    expected = dig(ctx.challenge, spec["expected_path"])
    given = dig(ctx.submission, spec["field"])
    if expected is None:
        raise CheckUnavailable("no expected value was issued for this challenge")
    try:
        ok = abs(float(given) - float(expected)) <= float(spec.get("tolerance", 0))
    except (TypeError, ValueError):
        ok = False
    return CheckOutcome(
        MET if ok else NOT_MET,
        "answer matches the platform-computed value" if ok else "answer does not match",
        {"expected_kind": spec["expected_path"]},
    )


def check_pointer_valid(ctx: CheckContext, spec: dict) -> CheckOutcome:
    """The learner pointed at a real record that is in the frozen manifest
    allow-list (so it provably belongs to them)."""
    pointer = dig(ctx.submission, spec["field"])
    allowed = {(r["type"], r["id"]): r for r in ctx.manifest.get("records", [])}
    types = spec.get("pointer_types")
    if not isinstance(pointer, dict):
        return CheckOutcome(NOT_MET, "no record was selected")
    record = allowed.get((pointer.get("type"), pointer.get("id")))
    if record is None or (types and pointer.get("type") not in types):
        return CheckOutcome(NOT_MET, "the selected record is not one of your recorded results")
    required = spec.get("require_fact")
    if required and not (record.get("facts") or {}).get(required):
        return CheckOutcome(NOT_MET, f"the selected record does not have {required}")
    return CheckOutcome(
        MET,
        "the selected record is one of your recorded results",
        refs=[{"type": pointer["type"], "id": pointer["id"]}],
    )


def check_manifest_unchanged(ctx: CheckContext, spec: dict) -> CheckOutcome:
    ok = ctx.attempt.input_manifest_hash == sha256_hex(
        ctx.manifest
    ) and ctx.attempt.submission_hash == sha256_hex(ctx.submission)
    return CheckOutcome(
        MET if ok else NOT_MET, "frozen inputs are unchanged" if ok else "frozen inputs changed after submit"
    )


# -- learner-record checks --------------------------------------------------------------


def check_milestones_evidenced(ctx: CheckContext, spec: dict) -> CheckOutcome:
    """A milestone counts only with real learning evidence — a "Complete" click
    proves nothing. Judged per MILESTONE: any of its attempts (e.g. an
    independent Study Mode variant after an H5 attempt) may carry the evidence;
    worked-example (study) attempts themselves never count."""
    attempts = [
        m
        for m in ctx.manifest.get("milestone_attempts", [])
        if m.get("status") != MilestoneAttemptStatus.SKIPPED_STUDY_MODE.value and m.get("mode") != "study"
    ]
    ids = [m["id"] for m in attempts]
    evidenced_attempts = set()
    if ids:
        rows = ctx.db.execute(
            select(LearningEvidence).where(
                LearningEvidence.user_id == ctx.user_id, LearningEvidence.milestone_attempt_id.in_(ids)
            )
        ).scalars()
        evidenced_attempts = {r.milestone_attempt_id for r in rows if r.passed and counts_toward_practiced(r)}
    milestones: Dict[str, bool] = {}
    for m in attempts:
        milestones[m["milestone_id"]] = (
            milestones.get(m["milestone_id"], False) or m["id"] in evidenced_attempts
        )
    done = sum(1 for ok in milestones.values() if ok)
    need = len(milestones) if spec.get("min", "all") == "all" else int(spec["min"])
    ok = len(milestones) > 0 and done >= need
    return CheckOutcome(
        MET if ok else (PARTIAL if done else NOT_MET),
        f"{done} of {len(milestones)} milestones have real evidence (needed {need})",
        {"evidenced": done, "total": len(milestones), "needed": need},
    )


def check_evidence_exists(ctx: CheckContext, spec: dict) -> CheckOutcome:
    ids = ctx.manifest.get("learning_evidence_ids") or []
    rows = []
    if ids:
        rows = list(
            ctx.db.execute(
                select(LearningEvidence).where(
                    LearningEvidence.user_id == ctx.user_id, LearningEvidence.id.in_(ids)
                )
            ).scalars()
        )
    wanted = spec.get("evidence_type")
    rows = [r for r in rows if r.passed and (wanted is None or r.evidence_type.value == wanted)]
    need = int(spec.get("min", 1))
    return CheckOutcome(
        MET if len(rows) >= need else NOT_MET,
        f"{len(rows)} qualifying evidence record(s) (needed {need})",
        {"count": len(rows)},
    )


def check_artifact_present(ctx: CheckContext, spec: dict) -> CheckOutcome:
    milestone_ids = [m["id"] for m in ctx.manifest.get("milestone_attempts", [])]
    count = 0
    if milestone_ids:
        count = len(
            list(
                ctx.db.execute(
                    select(Artifact).where(Artifact.milestone_attempt_id.in_(milestone_ids))
                ).scalars()
            )
        )
    for run_id in _ids(ctx, spec.get("field", "run_ids")):
        if _owned_agent_run(ctx.db, ctx.user_id, run_id) is not None:
            count += len(
                list(ctx.db.execute(select(Artifact).where(Artifact.agent_run_id == run_id)).scalars())
            )
    need = int(spec.get("min", 1))
    return CheckOutcome(
        MET if count >= need else NOT_MET, f"{count} artifact(s) found (needed {need})", {"count": count}
    )


# -- platform-run checks ---------------------------------------------------------------


def _owned_terminal_runs(ctx: CheckContext, field_name: str):
    runs, problems = [], []
    for run_id in _ids(ctx, field_name):
        run = _owned_agent_run(ctx.db, ctx.user_id, run_id)
        if run is None:
            problems.append("a cited run was not found among your own runs")
        elif run.status != AgentRunStatus.COMPLETED:
            problems.append("a cited run has not completed successfully")
        else:
            runs.append(run)
    return runs, problems


def check_run_exists_owned_terminal(ctx: CheckContext, spec: dict) -> CheckOutcome:
    runs, problems = _owned_terminal_runs(ctx, spec.get("field", "run_ids"))
    need = int(spec.get("min", 1))
    ok = len(runs) >= need and not problems
    return CheckOutcome(
        MET if ok else (PARTIAL if runs else NOT_MET),
        f"{len(runs)} completed run(s) of your own (needed {need})"
        + (f"; {problems[0]}" if problems else ""),
        {"count": len(runs)},
        [{"type": "agent_run", "id": r.id} for r in runs],
    )


def check_run_after_challenge_start(ctx: CheckContext, spec: dict) -> CheckOutcome:
    runs, _problems = _owned_terminal_runs(ctx, spec.get("field", "run_ids"))
    started = aware(ctx.attempt.started_at)
    fresh = [r for r in runs if aware(r.created_at) >= started]
    need = int(spec.get("min", 1))
    ok = len(fresh) >= need and len(fresh) == len(runs)
    return CheckOutcome(
        MET if ok else NOT_MET,
        f"{len(fresh)} of {len(runs)} run(s) were made after the challenge began (needed {need})",
        {"fresh": len(fresh), "cited": len(runs)},
    )


def check_inputs_match_challenge(ctx: CheckContext, spec: dict) -> CheckOutcome:
    expected = dig(ctx.challenge, spec["expected_path"])
    if not expected:
        raise CheckUnavailable("this challenge issued no expected input")
    runs, _problems = _owned_terminal_runs(ctx, spec.get("field", "run_ids"))
    needle = _squash(str(expected))
    matched = []
    for run in runs:
        task_run = ctx.db.get(TaskRun, run.task_run_id)
        task = ctx.db.get(Task, task_run.task_id) if task_run else None
        haystack = _squash(
            f"{(task.title if task else '')} {(task.description if task else '')} {(task.requirements if task else '')}"
        )
        if needle and needle in haystack:
            matched.append(run.id)
    return CheckOutcome(
        MET if matched else NOT_MET,
        (
            "a run used the issued challenge input"
            if matched
            else "no cited run used the issued challenge input"
        ),
        {"matched": len(matched)},
        [{"type": "agent_run", "id": i} for i in matched],
    )


def check_evaluation_run_findings(ctx: CheckContext, spec: dict) -> CheckOutcome:
    """MA6 results, READ-ONLY. The learner's own completed evaluation runs made
    after the challenge began; nothing is re-judged."""
    ids = _ids(ctx, spec.get("field", "evaluation_run_ids"))
    if not ids:
        return CheckOutcome(NOT_MET, "no evaluation run was cited")
    wanted = set(spec.get("criteria") or [])
    started = aware(ctx.attempt.started_at)
    bad, refs, hashes = 0, [], []
    for run_id in ids:
        run = ctx.db.get(EvaluationRun, run_id)
        if (
            run is None
            or run.requested_by_user_id != ctx.user_id
            or run.status != EvaluationRunStatus.COMPLETED
            or (spec.get("after_start", True) and aware(run.created_at) < started)
        ):
            bad += 1
            continue
        refs.append({"type": "evaluation_run", "id": run.id})
        hashes.append(run.subject_artifact_content_hash)
        findings = [r.finding for r in run.criterion_results if not wanted or r.criterion_key in wanted]
        if not findings or any(f in (NOT_MET, PARTIAL) for f in findings):
            bad += 1
    ok = bad == 0
    return CheckOutcome(
        MET if ok else NOT_MET,
        (
            "every cited evaluation met the expected criteria"
            if ok
            else f"{bad} cited evaluation(s) did not meet the criteria"
        ),
        {"evaluations": len(ids), "not_met": bad, "subject_artifact_hashes": hashes},
        refs,
    )


def check_cost_within(ctx: CheckContext, spec: dict) -> CheckOutcome:
    """Never fabricates cost: an unknown-cost call makes the finding PARTIAL."""
    runs, _ = _owned_terminal_runs(ctx, spec.get("field", "run_ids"))
    total, unknown = Decimal(0), False
    for run in runs:
        for call in ctx.db.execute(select(ModelCall).where(ModelCall.agent_run_id == run.id)).scalars():
            if call.cost_amount is None:
                unknown = True
            else:
                total += call.cost_amount
    if unknown:
        return CheckOutcome(
            PARTIAL, "cost is unknown for at least one call, so it cannot be verified", {"cost_known": False}
        )
    ok = total <= Decimal(str(spec["max_usd"]))
    return CheckOutcome(
        MET if ok else NOT_MET,
        "cost is within the bound" if ok else "cost is above the bound",
        {"cost_known": True},
    )


# -- Personal Lab -------------------------------------------------------------------------


def check_experiment_concluded(ctx: CheckContext, spec: dict) -> CheckOutcome:
    experiment_id = dig(ctx.submission, spec.get("field", "experiment_id"))
    experiment = ctx.db.get(Experiment, experiment_id) if isinstance(experiment_id, str) else None
    if experiment is None or experiment.user_id != ctx.user_id:
        return CheckOutcome(NOT_MET, "no experiment of yours was selected")
    ok = experiment.status == ExperimentStatus.COMPLETED and len(_text(experiment.conclusion_text)) >= int(
        spec.get("min_chars", 1)
    )
    return CheckOutcome(
        MET if ok else NOT_MET,
        (
            "the experiment finished and has your own conclusion"
            if ok
            else "the experiment is unfinished or has no conclusion"
        ),
        refs=[{"type": "experiment", "id": experiment.id}],
    )


def check_experiment_claim(ctx: CheckContext, spec: dict) -> CheckOutcome:
    """The learner's structured claim ("which variant met more criteria?") is
    checked against the platform's own per-label counts."""
    experiment_id = dig(ctx.submission, spec.get("field", "experiment_id"))
    facts = experiment_facts(ctx.db, ctx.user_id, experiment_id) if isinstance(experiment_id, str) else None
    if facts is None or not facts["has_findings"]:
        return CheckOutcome(NOT_APPLICABLE, "there are no platform findings to check the claim against")
    ranked = sorted(facts["labels"].items(), key=lambda kv: kv[1]["met"], reverse=True)
    if len(ranked) > 1 and ranked[0][1]["met"] == ranked[1][1]["met"]:
        return CheckOutcome(
            NOT_APPLICABLE,
            "the platform results are tied, so there is no single correct claim",
            {"tie": True},
        )
    claimed = dig(ctx.submission, spec["claim_field"])
    ok = claimed == ranked[0][0]
    return CheckOutcome(
        MET if ok else NOT_MET,
        "your claim matches the recorded results" if ok else "your claim does not match the recorded results",
        {"labels": facts["labels"]},
        [{"type": "experiment", "id": experiment_id}],
    )


CHECKS: Dict[str, Callable[[CheckContext, dict], CheckOutcome]] = {
    "choice_match": check_choice_match,
    "length_bounds": check_length_bounds,
    "section_present": check_section_present,
    "schema_valid": check_schema_valid,
    "numeric_match": check_numeric_match,
    "pointer_valid": check_pointer_valid,
    "manifest_unchanged": check_manifest_unchanged,
    "milestones_evidenced": check_milestones_evidenced,
    "evidence_exists": check_evidence_exists,
    "artifact_present": check_artifact_present,
    "run_exists_owned_terminal": check_run_exists_owned_terminal,
    "run_after_challenge_start": check_run_after_challenge_start,
    "inputs_match_challenge": check_inputs_match_challenge,
    "evaluation_run_findings": check_evaluation_run_findings,
    "cost_within": check_cost_within,
    "experiment_concluded": check_experiment_concluded,
    "experiment_claim": check_experiment_claim,
}


def run_check(ctx: CheckContext, criterion: dict) -> CheckOutcome:
    spec = criterion["check"]
    return CHECKS[spec["type"]](ctx, spec)
