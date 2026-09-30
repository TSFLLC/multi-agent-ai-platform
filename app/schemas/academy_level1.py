"""Learner-facing contracts for the Level 1 orchestration layer."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Level1KnowledgeCheckRequest(BaseModel):
    answers: Dict[str, Any]


class Level1LabLaunchRequest(BaseModel):
    assistance_level: Optional[str] = None
    input_text: Optional[str] = None


class Level1LabResultRequest(BaseModel):
    assistance_level: str
    conclusion: Optional[str] = None
    observed_change: Optional[str] = None


class Level1DayRead(BaseModel):
    day: int
    week: int
    title: str
    kind: str
    item_id: str
    estimated_minutes: Optional[int] = None
    state: str
    evidence_earned: bool
    next: Optional[int] = None
    capstone_stage: Optional[str] = None


class Level1ItemRead(BaseModel):
    id: str
    day: int
    week: int
    title: str
    kind: str
    estimated_minutes: Optional[int] = None
    body_md: Optional[str] = None
    spec: dict
    concept_id: str
    concept_slug: str
    capability_boundary: Optional[str] = None
    capstone_stage: Optional[str] = None


class Level1EvidenceRead(BaseModel):
    evidence_id: str
    concept_id: str
    learner_state: str
    passed: Optional[bool] = None
    results: Optional[List[dict]] = None


class Level1LabRead(BaseModel):
    academy_program: str
    week: int
    day: int
    title: str
    learning_objective: str
    learning_item_id: str
    experiment_id: Optional[str] = None
    engine: str
    fixture_key: str
    fixture_version: int = 1
    task_id: Optional[str] = None
    task_run_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    project_template_id: Optional[str] = None
    project_attempt_id: Optional[str] = None
    workflow_id: Optional[str] = None
    workflow_version_id: Optional[str] = None
    workflow_run_id: Optional[str] = None
    instructions: Optional[str] = None
    return_to: str
    capability_boundary: Optional[str] = None


class Level1AssessmentStartRequest(BaseModel):
    definition_key: str = Field(min_length=1, max_length=160)
    project_attempt_id: Optional[str] = None


class Level1AssessmentDraftRequest(BaseModel):
    draft: Dict[str, Any]


class Level1AssessmentSubmitRequest(BaseModel):
    attestation: Dict[str, Any]


class Level1AssistanceRequest(BaseModel):
    assistance_level: str


class Level1GraduationRead(BaseModel):
    eligible: bool
    required_concepts_complete: bool
    required_hands_on_complete: bool
    capstone_complete: bool
    missing_concepts: List[str]
    missing_days: List[int]
    missing_capstone_stages: List[int]
    note: str


class Level1ReviewRead(BaseModel):
    day: int
    strengths: List[str]
    revisit: List[str]
    recommendations: List[str]
    assessment_results_consulted: int


class Level1StepsRead(BaseModel):
    """AIL.5D.1: the learner-safe structured steps of one Day plus the learner's own progress. ``steps`` never
    contains ``private`` (answers, reveals, rubric material)."""

    item_id: str
    lineage_id: str
    version: int
    title: str
    step_schema_version: int
    steps: List[Dict[str, Any]]
    progress: Dict[str, Any]


class Level1StepProgressRead(BaseModel):
    step: Dict[str, Any]
    day: Dict[str, Any]
