"""AIL.5C — the ONE authoritative assistance / independence policy.

Every system that needs to know what an H-level, a verification level or a
grader kind means for DEMONSTRATED asks this module. ``LearnerStateService``
(the platform floor), 5B ``EvidenceQualification``, the assessment evidence
writer and the fresh-challenge rule all delegate here; nothing else may
hard-code "H3 means ...".

Independence is never a hidden score. Each piece of evidence keeps its raw
H-level and verification level; policy only *classifies* it (``full`` /
``partial`` / ``formative``) and reports a reason code.

Platform floor for a DEMONSTRATED leg (not configurable, only tightenable
per requirement through ``max_assistance`` / ``min_verification`` /
``grader_in``):

1. superseded rows never count;
2. demo-data rows never count;
3. self-reported work (``execution_verification=self_reported`` or
   ``grader=self``) never counts;
4. assistance above H2 never counts (H3 partial, H4 guided, H5 solution);
5. project execution evidence must be platform- or sandbox-verified.
"""

from typing import Any, Iterable, Optional, Tuple

from app.db.enums import AssistanceLevel, EvidenceType, ExecutionVerification, GradingMode

FULL = "full"
PARTIAL = "partial"
FORMATIVE = "formative"

# H0-H2 = full independence for a DEMONSTRATED leg. This is the single place
# the number lives.
MAX_DEMONSTRATION_ASSISTANCE_RANK = 2

# Fresh work done in Assessment Mode (the Mentor is locked) is independent by
# construction. Every writer of assessment evidence records THIS level.
ASSESSMENT_MODE_ASSISTANCE = AssistanceLevel.H0

_RANK = {
    AssistanceLevel.H0: 0,
    AssistanceLevel.H1: 1,
    AssistanceLevel.H2: 2,
    AssistanceLevel.H3: 3,
    AssistanceLevel.H4: 4,
    AssistanceLevel.H5: 5,
}

_REASON = {
    AssistanceLevel.H0: "h0_independent",
    AssistanceLevel.H1: "h1_conceptual_clue",
    AssistanceLevel.H2: "h2_targeted_pointer",
    AssistanceLevel.H3: "h3_partial_structure",
    AssistanceLevel.H4: "h4_guided_walkthrough",
    AssistanceLevel.H5: "h5_solution_shown",
}

VERIFIED = frozenset({ExecutionVerification.PLATFORM_VERIFIED, ExecutionVerification.SANDBOX_VERIFIED})

# Evidence whose meaning is "the learner ran / built something": it is only
# a DEMONSTRATED leg when the platform (or, post-MA9, a sandbox) verified it.
EXECUTION_EVIDENCE_TYPES = frozenset(
    {
        EvidenceType.MODIFICATION,
        EvidenceType.REPRODUCTION,
        EvidenceType.DEBUGGING,
        EvidenceType.PROJECT_ASSESSMENT,
    }
)

# Evidence types that a model can be the only judge of. Never the sole leg.
JUDGMENT_EVIDENCE_TYPES = frozenset({EvidenceType.EXPLAIN_BACK})

ATTESTATION_CHOICES = ("no_external_help", "used_docs", "used_ai_assistant", "other")


def assistance_rank(level: Optional[AssistanceLevel]) -> Optional[int]:
    if level is None:
        return None
    return _RANK[AssistanceLevel(level)]


def classify_assistance(level: Optional[AssistanceLevel]) -> str:
    """full (H0-H2 or none recorded) / partial (H3) / formative (H4-H5)."""
    rank = assistance_rank(level)
    if rank is None or rank <= MAX_DEMONSTRATION_ASSISTANCE_RANK:
        return FULL
    if AssistanceLevel(level) == AssistanceLevel.H3:
        return PARTIAL
    return FORMATIVE


def assistance_reason(level: Optional[AssistanceLevel]) -> str:
    if level is None:
        return "no_assistance_recorded"
    return _REASON[AssistanceLevel(level)]


def is_verified(verification: Optional[ExecutionVerification]) -> bool:
    return verification is not None and ExecutionVerification(verification) in VERIFIED


def _parse_assistance(value: Any) -> Optional[int]:
    try:
        return _RANK[AssistanceLevel(value)]
    except (ValueError, KeyError):
        return None


# -- Practiced ----------------------------------------------------------------


def level_counts_toward_practiced(level: Optional[AssistanceLevel]) -> bool:
    """H5 (solution shown) is never practice; every other level is."""
    return level is None or AssistanceLevel(level) != AssistanceLevel.H5


def counts_toward_practiced(row) -> bool:
    """Superseded rows never count; H5 (solution shown) is never practice."""
    return row.superseded_by_id is None and level_counts_toward_practiced(row.assistance_level)


# -- Demonstrated: platform floor ----------------------------------------------


def demonstration_floor(row) -> Tuple[bool, str]:
    """(passes, reason_code). The non-negotiable floor every DEMONSTRATED leg
    must clear, whatever a Concept Version's own requirements say."""
    if row.superseded_by_id is not None:
        return False, "superseded"
    if row.on_demo_data:
        return False, "demo_data"
    if row.grader == GradingMode.SELF or row.execution_verification == ExecutionVerification.SELF_REPORTED:
        return False, "self_reported"
    rank = assistance_rank(row.assistance_level)
    if rank is not None and rank > MAX_DEMONSTRATION_ASSISTANCE_RANK:
        return False, assistance_reason(row.assistance_level)
    # 5B project-backed lab rows and every 5C execution leg need a real
    # verification, not merely "not applicable".
    project_backed_lab = row.evidence_type == EvidenceType.LAB and row.milestone_attempt_id is not None
    if (row.evidence_type in EXECUTION_EVIDENCE_TYPES or project_backed_lab) and not is_verified(
        row.execution_verification
    ):
        return False, "unverified_execution"
    return True, "ok"


def leg_constraints_ok(row, requirement: dict) -> bool:
    """Per-requirement tightening from ``ConceptVersion.evidence_requirements``
    v2. Unparseable constraint values fail closed."""
    max_assistance = requirement.get("max_assistance")
    if max_assistance is not None:
        limit = _parse_assistance(max_assistance)
        if limit is None:
            return False
        limit = min(limit, MAX_DEMONSTRATION_ASSISTANCE_RANK)  # can only tighten the floor
        rank = assistance_rank(row.assistance_level)
        if rank is not None and rank > limit:
            return False

    min_verification = requirement.get("min_verification")
    if min_verification is not None:
        try:
            required = ExecutionVerification(min_verification)
        except ValueError:
            return False
        if required not in VERIFIED or not is_verified(row.execution_verification):
            return False
        if required == ExecutionVerification.SANDBOX_VERIFIED and (
            ExecutionVerification(row.execution_verification) != ExecutionVerification.SANDBOX_VERIFIED
        ):
            return False

    grader_in: Optional[Iterable[str]] = requirement.get("grader_in")
    return grader_in is None or getattr(row.grader, "value", row.grader) in set(grader_in)


def counts_toward_demonstrated(row, requirement: Optional[dict] = None) -> bool:
    ok, _reason = demonstration_floor(row)
    if not ok:
        return False
    return leg_constraints_ok(row, requirement or {})


# -- Requirement-set validation (publish time) ---------------------------------


def _iter_requirements(requirements: dict):
    for req in requirements.get("requires_all", []) or []:
        yield req
    for group in requirements.get("requires_any_of", []) or []:
        for req in group:
            yield req


def _leg_can_be_deterministic(requirement: dict) -> bool:
    evidence_type = EvidenceType(requirement["evidence_type"])
    if evidence_type in JUDGMENT_EVIDENCE_TYPES:
        return False
    grader_in = requirement.get("grader_in")
    return grader_in is None or GradingMode.DETERMINISTIC.value in set(grader_in)


def validate_evidence_requirements(requirements: Optional[dict]) -> None:
    """Raise ``ValueError`` for a malformed v2 requirement payload.

    A set that lists a judged-only leg must also list a leg that can be
    deterministic — AI judgment alone never establishes DEMONSTRATED.
    """
    if not requirements:
        return
    sets = [requirements]
    if requirements.get("alternative"):
        sets.append(requirements["alternative"])
    for one in sets:
        legs = list(_iter_requirements(one))
        for leg in legs:
            try:
                EvidenceType(leg["evidence_type"])
            except (KeyError, ValueError):
                raise ValueError("evidence_requirements: unknown or missing evidence_type")
            if "max_assistance" in leg and _parse_assistance(leg["max_assistance"]) is None:
                raise ValueError("evidence_requirements: invalid max_assistance")
            if "min_verification" in leg:
                try:
                    if ExecutionVerification(leg["min_verification"]) not in VERIFIED:
                        raise ValueError
                except ValueError:
                    raise ValueError("evidence_requirements: invalid min_verification")
            if "grader_in" in leg:
                try:
                    [GradingMode(g) for g in leg["grader_in"]]
                except (ValueError, TypeError):
                    raise ValueError("evidence_requirements: invalid grader_in")
        if not legs:
            continue
        has_judged_only = any(not _leg_can_be_deterministic(leg) for leg in legs)
        if has_judged_only and not any(_leg_can_be_deterministic(leg) for leg in legs):
            raise ValueError("evidence_requirements: an AI-judged leg needs a deterministic leg")


# -- Fresh challenge ------------------------------------------------------------

ALWAYS = "always"
IF_ASSISTED = "if_assisted"
NEVER = "never"


def fresh_challenge_required(
    policy: str,
    *,
    source_levels: Iterable[Optional[AssistanceLevel]] = (),
    study_mode_used: bool = False,
    kind_is_capstone: bool = False,
) -> Tuple[bool, str]:
    """(required, reason_code). Deterministic; no model involved."""
    if kind_is_capstone or policy == ALWAYS:
        return True, "policy_always"
    if policy == NEVER:
        return False, "policy_never"
    if study_mode_used:
        return True, "study_mode_used"
    classes = {classify_assistance(level) for level in source_levels}
    if PARTIAL in classes:
        return True, "source_work_partially_assisted"
    if FORMATIVE in classes:
        return True, "source_work_substantially_assisted"
    return False, "source_work_independent"


def attestation_is_formative(attestation: Optional[dict]) -> bool:
    """A declared AI assistant makes the attempt formative: recorded and
    useful for feedback, never qualifying. Honesty is never punished by
    hiding the feedback — only the qualification is withheld."""
    return bool(attestation) and attestation.get("declaration") == "used_ai_assistant"
