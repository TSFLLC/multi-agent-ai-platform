"""AIL.4A Personal Stay-Ahead Today — read-only response contract.

Every value here is derived on read from records the user already owns or is
explicitly authorized to read. Nothing is persisted, scored, ranked or
generated: text fields are fixed templates filled with recorded facts.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class StayAheadFamily(str, Enum):
    USED_MODEL_CHANGED = "USED_MODEL_CHANGED"
    EXPERIMENT_MAY_BE_STALE = "EXPERIMENT_MAY_BE_STALE"
    WATCHED_DEVELOPMENT_CHANGED = "WATCHED_DEVELOPMENT_CHANGED"
    CONCEPT_CHANGED = "CONCEPT_CHANGED"
    WORTH_REVISITING = "WORTH_REVISITING"


class StayAheadReasonCode(str, Enum):
    """Why a signal exists. Each code names one recorded fact, never a
    weight. They are code-level constants only; no table stores them."""

    # Relationship to the user (why THIS user is seeing it)
    # Kept apart on purpose: PERSONAL_USAGE is the authenticated user's own
    # Personal Lab experiment usage; OPTED_IN_PROJECT_USAGE is shared project
    # evidence that is never attributed to the user (the repository records no
    # per-user ownership of project ModelCalls).
    PERSONAL_USAGE = "PERSONAL_USAGE"
    OPTED_IN_PROJECT_USAGE = "OPTED_IN_PROJECT_USAGE"
    HAS_LEARNING_EVIDENCE = "HAS_LEARNING_EVIDENCE"
    WATCHING_CONCEPT = "WATCHING_CONCEPT"
    WATCHING_DEVELOPMENT = "WATCHING_DEVELOPMENT"
    EXPERIMENT_COUNTED_TOWARD_LEARNING = "EXPERIMENT_COUNTED_TOWARD_LEARNING"
    # What changed: models
    MODEL_PRICE_CHANGED = "MODEL_PRICE_CHANGED"
    MODEL_CONTEXT_CHANGED = "MODEL_CONTEXT_CHANGED"
    MODEL_CAPABILITY_CHANGED = "MODEL_CAPABILITY_CHANGED"
    MODEL_STATUS_CHANGED = "MODEL_STATUS_CHANGED"
    # What changed: watched developments
    WATCH_NEW_EVIDENCE = "WATCH_NEW_EVIDENCE"
    WATCH_CONCEPT_LINK_CONFIRMED = "WATCH_CONCEPT_LINK_CONFIRMED"
    WATCH_REVISIT_DATE_REACHED = "WATCH_REVISIT_DATE_REACHED"
    # What changed: concepts
    CONCEPT_NEW_MATERIAL_VERSION = "CONCEPT_NEW_MATERIAL_VERSION"
    CONCEPT_NEW_LINKED_DEVELOPMENT = "CONCEPT_NEW_LINKED_DEVELOPMENT"


class StayAheadEvidenceRef(BaseModel):
    """A pointer to the exact record a statement rests on."""

    type: str
    id: str
    role: Optional[str] = None
    label: Optional[str] = None


class StayAheadLink(BaseModel):
    """A link only. Following it never executes anything; the frontend maps
    ``kind`` to an existing Radar / Model / Personal Lab route."""

    kind: str  # "model" | "development" | "experiment"
    id: str
    label: str


class StayAheadReason(BaseModel):
    id: str
    family: StayAheadFamily
    title: str
    what_changed: str
    why: str
    reason_codes: List[str]
    since: Optional[datetime] = None
    changed_at: datetime
    subject: Dict[str, Any] = Field(default_factory=dict)
    changes: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_refs: List[StayAheadEvidenceRef] = Field(default_factory=list)
    links: List[StayAheadLink] = Field(default_factory=list)


class StayAheadSignal(StayAheadReason):
    # Present only on WORTH_REVISITING: the learner-state rung and the exact
    # signals this card absorbed (each keeps its own reason codes/evidence).
    concept: Optional[Dict[str, Any]] = None
    learner_state: Optional[Dict[str, Any]] = None
    distinct_reason_count: Optional[int] = None
    reasons: List[StayAheadReason] = Field(default_factory=list)


class StayAheadSection(BaseModel):
    total: int
    shown: int
    items: List[StayAheadSignal]


class StayAheadReviewCard(BaseModel):
    """AIL.4B: one Concept the user previously demonstrated that is worth
    reviewing again. Every field is a recorded fact or a fixed template; there
    is no score, and the card offers a link/action, never runs one."""

    id: str
    kind: str  # REVIEW_DUE | REVIEW_FAILED | CONCEPT_CHANGED_REVIEW
    title: str
    what: str
    why: str
    reason_codes: List[str]
    concept: Dict[str, Any]
    learner_state: Dict[str, Any]
    baseline: Optional[Dict[str, Any]] = None
    interval: Dict[str, Any] = Field(default_factory=dict)
    material_changes: List[Dict[str, Any]] = Field(default_factory=list)
    attempt: Optional[Dict[str, Any]] = None
    action: Dict[str, Any] = Field(default_factory=dict)
    evidence_refs: List[StayAheadEvidenceRef] = Field(default_factory=list)
    due_at: Optional[datetime] = None


class StayAheadReviewSection(BaseModel):
    total: int
    shown: int
    items: List[StayAheadReviewCard]


class StayAheadSections(BaseModel):
    worth_revisiting: StayAheadSection
    used_models_changed: StayAheadSection
    watched_developments: StayAheadSection
    experiments_to_rerun: StayAheadSection
    concepts_changed: StayAheadSection
    review: StayAheadReviewSection


class StayAheadToday(BaseModel):
    generated_at: datetime
    window_days: int
    window_start: datetime
    sections: StayAheadSections
