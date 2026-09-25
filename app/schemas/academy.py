"""AIL.5A Academy API contracts."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.db.enums import AcademyEnrollmentStatus, AcademyPace, AcademyProgramItemKind, AcademyProgramVersionStatus


class AcademyProgramCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=10000)

    class Config:
        extra = "forbid"


class AcademyVersionCreate(BaseModel):
    duration_days: int = Field(ge=1, le=3650)
    completion_rules: Optional[dict] = None

    class Config:
        extra = "forbid"


class AcademyItemCreate(BaseModel):
    week: int = Field(ge=1)
    day: int = Field(ge=1)
    module_key: str = Field(min_length=1, max_length=120)
    position: int = Field(ge=0)
    item_kind: AcademyProgramItemKind
    concept_id: Optional[str] = None
    learning_item_id: Optional[str] = None
    required: bool = True
    estimated_minutes: Optional[int] = Field(default=None, ge=1)
    purpose_text: Optional[str] = Field(default=None, max_length=2000)
    completion_requirement: Optional[dict] = None

    class Config:
        extra = "forbid"


class AcademyEnrollmentCreate(BaseModel):
    pace: AcademyPace = AcademyPace.SCHEDULED

    class Config:
        extra = "forbid"


class AcademyProgramItemRead(BaseModel):
    id: str
    title: Optional[str] = None
    week: int
    day: int
    module_key: str
    position: int
    item_kind: AcademyProgramItemKind
    concept_id: Optional[str] = None
    learning_item_id: Optional[str] = None
    required: bool
    estimated_minutes: Optional[int] = None
    purpose_text: Optional[str] = None
    completion_requirement: Optional[dict] = None
    state: Optional[str] = None
    overlays: List[str] = []
    eligible: Optional[bool] = None


class AcademyProgramVersionRead(BaseModel):
    id: str
    program_id: str
    version: int
    status: AcademyProgramVersionStatus
    duration_days: int
    completion_rules: dict
    published_at: Optional[datetime] = None
    items: List[AcademyProgramItemRead] = []


class AcademyProgramRead(BaseModel):
    id: str
    slug: str
    title: str
    description: Optional[str] = None
    status: str
    author_user_id: str
    versions: List[AcademyProgramVersionRead] = []


class AcademyEnrollmentRead(BaseModel):
    id: str
    user_id: str
    program_version_id: str
    pace: AcademyPace
    status: AcademyEnrollmentStatus
    started_at: datetime
    completed_at: Optional[datetime] = None


class AcademyProgressRead(BaseModel):
    required_items: int
    completed_items: int
    required_concepts: int
    demonstrated_concepts: int
    practiced_or_better: int
    understood_or_better: int
    exposed: int
    review_due: int
    next_item_id: Optional[str] = None
    complete: bool


class AcademyEnrollmentDetailRead(BaseModel):
    enrollment: AcademyEnrollmentRead
    program: AcademyProgramRead
    progress: AcademyProgressRead


class AcademyTodayRead(BaseModel):
    enrollment: AcademyEnrollmentRead
    day: int
    items: List[AcademyProgramItemRead]
    progress: AcademyProgressRead


class AcademyLessonItemRead(BaseModel):
    id: str
    item_type: str
    title: str
    body_md: Optional[str] = None
    grading_mode: Optional[str] = None
    reviewed: bool
    version: int
    est_minutes: Optional[int] = None


class AcademyLessonReviewRead(BaseModel):
    eligible: bool = False
    due: bool = False
    failed: bool = False
    demonstrated: bool = False
    due_reasons: List[str] = []


class AcademyLessonRead(BaseModel):
    concept_id: str
    slug: str
    name: str
    level: str
    kind: str
    concept_version_id: str
    concept_version: int
    plain_definition: str
    technical_explanation: Optional[str] = None
    examples_md: Optional[str] = None
    learning_items: List[AcademyLessonItemRead] = []
    learner_state: str
    review_overlays: List[str] = []
    review: AcademyLessonReviewRead
    prerequisite_eligible: bool
    unmet_prerequisite_count: int = 0
    evidence_count: int = 0
    passed_evidence_count: int = 0
