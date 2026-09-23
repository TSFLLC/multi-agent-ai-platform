"""Learner State Service — AIL.1A, docs/ail-learning-spec-v1.md Sec 18.

Learner State is COMPUTED, never persisted (AIL architecture
reconciliation, frozen contract #7; spec Sec 18.5) — there is no
``learner_concept_states`` table, and this service is the only place the
ladder is derived, from ``learning_evidence`` rows plus the concept
version's ``evidence_requirements`` JSON rule spec.

Canonical ladder (highest satisfied rule wins, each rule evaluated
independently — spec Sec 18.2):

    NOT_STARTED -> EXPOSED -> UNDERSTOOD -> PRACTICED -> DEMONSTRATED

Overlays this service computes: SELF_REPORTED, CHANGED. REVIEW_DUE and
REVIEW_FAILED are explicitly deferred to AIL.4 (spec Sec 35) — they need
freshness/interval and review-attempt bookkeeping this slice does not
own; nothing here or in the schema blocks adding them later.

``evidence_requirements`` schema (a rule payload, not a relationship —
spec Sec 24.4/10.5.5), used only by DEMONSTRATED:

    {
      "requires_all": [{"evidence_type": "knowledge_check", "min_passed": 1,
                         "allow_generated": false}, ...],
      "requires_any_of": [[{...}, {...}], ...],   # >=1 requirement per group
      "alternative": { ... same two keys ... }     # spec Sec 18.4 rule 4
    }

DEMONSTRATED requires the primary set (or, failing that, the alternative
set) fully satisfied AND at least one satisfying requirement backed by a
``grader=deterministic`` evidence row (spec Sec 18.4 rule 1 — no
DEMONSTRATED on AI-graded evidence alone). A ``knowledge_check``
requirement with ``allow_generated=false`` (the default) only counts
evidence rows whose ``question_origin`` is ``reviewed`` or unset (spec Sec
18.4 rule 2 — generated/unreviewed questions establish UNDERSTOOD only,
never DEMONSTRATED).
"""

from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from app.db.enums import ConceptKind, EvidenceType, GradingMode, QuestionOrigin
from app.models.concepts import ConceptVersion
from app.models.learner import LearningEvidence
from app.services.concept_graph_service import ConceptGraphService
from app.services.learning_evidence_service import LearningEvidenceService

NOT_STARTED = "not_started"
EXPOSED = "exposed"
UNDERSTOOD = "understood"
PRACTICED = "practiced"
DEMONSTRATED = "demonstrated"

_LADDER_ORDER = [NOT_STARTED, EXPOSED, UNDERSTOOD, PRACTICED, DEMONSTRATED]

SELF_REPORTED = "self_reported"
CHANGED = "changed"


@dataclass
class LearnerConceptState:
    concept_id: str
    ladder: str
    overlays: Set[str] = field(default_factory=set)
    evidence: List[LearningEvidence] = field(default_factory=list)

    def is_at_least(self, level: str) -> bool:
        return _LADDER_ORDER.index(self.ladder) >= _LADDER_ORDER.index(level)


def default_evidence_requirements(kind: ConceptKind) -> dict:
    """Per-kind DEMONSTRATED requirement defaults — spec Sec 18.3's table,
    at evidence-type granularity (this slice does not distinguish a
    "scenario" question from an ordinary knowledge-check question at the
    evidence level; both produce ``evidence_type=knowledge_check``).

    Definitional's spec rule is "knowledge check >=80% AND 1 correct
    scenario question" — two distinct pieces of evidence, both of which
    this slice represents as ``knowledge_check`` rows — so it requires
    ``min_passed=2``, not 1. Without that, UNDERSTOOD (min_passed=1) and
    DEMONSTRATED would require identical evidence for definitional
    concepts, collapsing two rungs of the ladder into one."""

    knowledge_check = {"evidence_type": "knowledge_check", "min_passed": 1, "allow_generated": False}
    observation = {"evidence_type": "observation", "min_passed": 1}
    lab = {"evidence_type": "lab", "min_passed": 1}
    interpretation = {"evidence_type": "interpretation", "min_passed": 1}

    if kind == ConceptKind.DEFINITIONAL:
        return {"requires_all": [{**knowledge_check, "min_passed": 2}]}
    if kind == ConceptKind.MECHANISM:
        return {"requires_all": [knowledge_check], "requires_any_of": [[lab, interpretation]]}
    if kind == ConceptKind.OPERATIONAL:
        return {"requires_all": [knowledge_check, observation, lab, interpretation]}
    if kind == ConceptKind.ARCHITECTURAL:
        return {"requires_all": [knowledge_check, observation, interpretation]}
    raise ValueError(f"Unknown concept kind: {kind}")


class LearnerStateService:
    def __init__(self, db):
        self.db = db
        self._concepts = ConceptGraphService(db)
        self._evidence = LearningEvidenceService(db)

    def state(self, user_id: str, concept_id: str) -> LearnerConceptState:
        evidence = self._evidence.list_evidence(user_id, concept_id=concept_id)
        graded = [e for e in evidence if e.evidence_type != EvidenceType.SELF_REPORT]

        ladder = NOT_STARTED
        if self._exposed_ok(graded):
            ladder = EXPOSED
        if self._understood_ok(graded):
            ladder = UNDERSTOOD
        if self._practiced_ok(graded):
            ladder = PRACTICED

        version = self._concepts.get_current_version(concept_id)
        if version is not None and self._demonstrated_ok(graded, version):
            ladder = DEMONSTRATED

        overlays: Set[str] = set()
        if any(e.evidence_type == EvidenceType.SELF_REPORT for e in evidence):
            overlays.add(SELF_REPORTED)
        if ladder == DEMONSTRATED and self._changed_since(evidence, version):
            overlays.add(CHANGED)

        return LearnerConceptState(concept_id=concept_id, ladder=ladder, overlays=overlays, evidence=evidence)

    def states_for_concepts(self, user_id: str, concept_ids: List[str]) -> dict:
        return {cid: self.state(user_id, cid) for cid in concept_ids}

    # -- Ladder rules (spec Sec 18.2) ----------------------------------------

    @staticmethod
    def _exposed_ok(evidence: List[LearningEvidence]) -> bool:
        return any(e.evidence_type == EvidenceType.LESSON_COMPLETED for e in evidence)

    @staticmethod
    def _understood_ok(evidence: List[LearningEvidence]) -> bool:
        return any(e.evidence_type == EvidenceType.KNOWLEDGE_CHECK and e.passed for e in evidence)

    @staticmethod
    def _practiced_ok(evidence: List[LearningEvidence]) -> bool:
        return any(
            e.evidence_type in (EvidenceType.OBSERVATION, EvidenceType.LAB) and e.passed for e in evidence
        )

    def _demonstrated_ok(self, evidence: List[LearningEvidence], version: ConceptVersion) -> bool:
        requirements = version.evidence_requirements or default_evidence_requirements(
            self._concepts.get_concept(version.concept_id).kind
        )
        satisfied, has_deterministic_leg = self._requirements_satisfied(evidence, requirements)
        if satisfied and has_deterministic_leg:
            return True

        alternative = requirements.get("alternative")
        if alternative:
            satisfied, has_deterministic_leg = self._requirements_satisfied(evidence, alternative)
            return satisfied and has_deterministic_leg
        return False

    @staticmethod
    def _requirement_evidence(evidence: List[LearningEvidence], requirement: dict) -> List[LearningEvidence]:
        evidence_type = EvidenceType(requirement["evidence_type"])
        matches = [e for e in evidence if e.evidence_type == evidence_type and e.passed]
        if evidence_type == EvidenceType.KNOWLEDGE_CHECK and not requirement.get("allow_generated", False):
            matches = [e for e in matches if e.question_origin != QuestionOrigin.GENERATED]
        return matches

    def _requirements_satisfied(
        self, evidence: List[LearningEvidence], requirements: dict
    ) -> Tuple[bool, bool]:
        satisfying_rows: List[LearningEvidence] = []

        for requirement in requirements.get("requires_all", []):
            matches = self._requirement_evidence(evidence, requirement)
            if len(matches) < requirement.get("min_passed", 1):
                return False, False
            satisfying_rows.extend(matches)

        for group in requirements.get("requires_any_of", []):
            group_matches: List[LearningEvidence] = []
            for requirement in group:
                matches = self._requirement_evidence(evidence, requirement)
                if len(matches) >= requirement.get("min_passed", 1):
                    group_matches = matches
                    break
            if not group_matches:
                return False, False
            satisfying_rows.extend(group_matches)

        has_deterministic_leg = any(e.grader == GradingMode.DETERMINISTIC for e in satisfying_rows)
        return True, has_deterministic_leg

    @staticmethod
    def _changed_since(evidence: List[LearningEvidence], current_version: Optional[ConceptVersion]) -> bool:
        """Single-hop check: true when the current version differs from
        whatever version the evidence cites AND the current version's own
        ``change_severity`` (relative to its immediate predecessor) is
        material. A learner who skips more than one published version
        between demonstrating and re-checking, where an intermediate hop
        was material but the latest hop was not, is a known gap — no
        version-history walk is implemented in this slice."""
        if current_version is None:
            return False
        demonstrating_versions = {e.concept_version_id for e in evidence}
        if current_version.id in demonstrating_versions:
            return False
        return (
            current_version.change_severity is not None
            and current_version.change_severity.value == "material"
        )
