"""Comparison resource contracts — Section 25.2, 25.5, 18. MA5 correction:
``ComparisonRunCreate`` now takes ``task_id`` (a comparison creates its own
independent per-candidate Task Runs) rather than MA0's ``task_run_id``,
which read as grouping *already-existing* Agent Runs — that framing cannot
express "configure N candidates, then launch them," which is what the
frozen ``comparison_candidates`` schema's nullable ``agent_run_id``/
``task_run_id`` (Section 24.4 #7, MA5 correction) already requires. This is
the only breaking change to the MA0 stub contract; every other shape is
additive."""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, model_validator

from app.db.enums import ComparisonRunStatus, EvaluationMethod


class ComparisonReviewConfig(BaseModel):
    """Opts one candidate into MA4 Agent-to-Agent Review instead of a plain
    single Agent Run -- never required, most candidates omit this."""

    reviewer_agent_version_id: str
    max_repair_iterations: Optional[int] = None
    review_instructions: Optional[str] = None


class ComparisonCandidateCreate(BaseModel):
    agent_version_id: str
    label: str
    # Section 3: "same Agent, different models" -- overrides the Agent
    # Version's own (shared, immutable) model_policy for this candidate
    # only. Same shape as agent_versions.model_policy_json.
    model_policy_override: Optional[dict] = None
    review: Optional[ComparisonReviewConfig] = None


class ComparisonRunCreate(BaseModel):
    task_id: str
    candidates: List[ComparisonCandidateCreate]
    budget_id: Optional[str] = None


class ComparisonCandidateRead(BaseModel):
    id: str
    label: str
    agent_version_id: str
    model_policy_override: Optional[dict] = None
    review: Optional[ComparisonReviewConfig] = None
    task_run_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    # Task Run status of this candidate's own independent execution, or
    # "not_launched" before POST /comparisons/{id}/launch.
    status: str
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    artifact_id: Optional[str] = None
    artifact_hash: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_amount: Decimal = Decimal(0)
    latency_ms: int = 0
    # Never populated by MA5 -- MA6 owns objective/judge scoring (Section
    # 17.4 "No Hard-Coded Rankings"); kept only because it is part of the
    # frozen comparison_candidates schema (Section 24.4 #7).
    rank: Optional[int] = None
    is_winner: bool = False
    created_at: datetime


class ComparisonRunRead(BaseModel):
    id: str
    task_id: str
    task_run_id: str
    status: ComparisonRunStatus
    # Derived, read-time-only value -- see
    # app.services.comparison_service.compute_comparison_phase. Never
    # stored; "ready_for_selection" has no ComparisonRunStatus counterpart.
    phase: str
    winner_agent_run_id: Optional[str] = None
    winner_artifact_id: Optional[str] = None
    winner_artifact_hash: Optional[str] = None
    cancellation_requested_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    candidates: List[ComparisonCandidateRead] = []
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost: Decimal = Decimal(0)


class SelectWinnerRequest(BaseModel):
    """Endpoint name/field preserve the frozen MA0 contract (Section 25.5)
    even though the action is a human *canonical selection*, never an
    automatic "winner" (Section 18.3). ``artifact_hash`` is the MA5
    addition binding the selection to the exact artifact content the human
    reviewed, mirroring ``approvals.action_fingerprint`` (Section 24.4
    #15) -- a mismatch is rejected with 409 ``artifact_hash_mismatch``."""

    comparison_candidate_id: str
    artifact_hash: str


class ComparisonEvaluationRequest(BaseModel):
    """POST /comparisons/{id}/evaluations (Section: MA6 Slice 3C) -- one
    request configures the *shared* Evaluation Definition Version/method/
    evaluator for every eligible candidate; the operator always picks the
    Evaluation Definition Version and, for AGENT_EVALUATOR, the evaluator
    Agent Version/model themselves, exactly like
    app.schemas.evaluation_runs.EvaluationRunCreate's single-AgentRun
    shape -- never auto-selected. ``method`` defaults to DETERMINISTIC so
    existing callers only need to supply an Evaluation Definition Version."""

    evaluation_definition_version_id: str
    method: EvaluationMethod = EvaluationMethod.DETERMINISTIC
    evaluator_agent_version_id: Optional[str] = None
    evaluator_model_policy_override: Optional[dict] = None
    budget_id: Optional[str] = None

    @model_validator(mode="after")
    def _require_evaluator_agent_version_for_agent_evaluator(self) -> "ComparisonEvaluationRequest":
        if self.method == EvaluationMethod.AGENT_EVALUATOR and not self.evaluator_agent_version_id:
            raise ValueError("evaluator_agent_version_id is required when method is agent_evaluator.")
        return self


class ComparisonCandidateEvaluationResult(BaseModel):
    """One bounded per-candidate outcome -- never a comparative verdict.
    ``status`` is "created" | "reused" (idempotent retry) | "skipped" (not
    yet eligible) | "failed" (eligible but EvaluationRun creation itself
    was rejected); see
    app.services.evaluation_execution_service.ComparisonCandidateEvaluationOutcome."""

    comparison_candidate_id: str
    subject_agent_run_id: Optional[str] = None
    status: str
    evaluation_run_id: Optional[str] = None
    reason: Optional[str] = None


class ComparisonEvaluationResponse(BaseModel):
    comparison_id: str
    results: List[ComparisonCandidateEvaluationResult] = []
