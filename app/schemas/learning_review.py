"""AIL.4B review attempts — request/response contract.

Requests forbid extra fields: a client-supplied ``user_id`` is a 422. The
answer key never appears in any response.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, StrictInt


class ReviewStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept_id: str


class ReviewCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected: List[StrictInt]


class ReviewItemRead(BaseModel):
    id: str
    title: str
    body_md: Optional[str] = None
    item_type: str
    options: List[str]
    multiple: bool


class ReviewAttemptRead(BaseModel):
    id: str
    concept_id: str
    concept_version_id: str
    learning_item_id: Optional[str] = None
    status: str
    resulting_learning_evidence_id: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None


class ReviewStartRead(BaseModel):
    attempt: ReviewAttemptRead
    item: Optional[ReviewItemRead] = None
    created: bool


class ReviewLearnerStateRead(BaseModel):
    ladder: str
    overlays: List[str]


class ReviewCompleteRead(BaseModel):
    attempt: ReviewAttemptRead
    passed: bool
    evidence_id: Optional[str] = None
    replay: bool
    learner_state: ReviewLearnerStateRead
    message: str


class ReviewPromptRead(BaseModel):
    concept_id: str
    slot: int
    prompt_kind: str
    delivered_at: datetime
    new: bool  # delivered by THIS call (False for one delivered earlier this week)


class ReviewPromptAllocationRead(BaseModel):
    week_start: datetime
    limit: int
    delivered: List[ReviewPromptRead]
    new_count: int
