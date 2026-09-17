from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel

from app.db.enums import ReviewDecision
from app.schemas.artifacts import ArtifactRead
from app.schemas.tasks import AgentRunRead


class ReviewedTaskRunCreate(BaseModel):
    """Section: MA4 review configuration -- a reviewed Task Run must
    explicitly identify its primary Agent Version, reviewer Agent
    Version, and repair limit (bounded, never uncontrolled)."""

    primary_agent_version_id: str
    reviewer_agent_version_id: str
    max_repair_iterations: Optional[int] = None
    review_instructions: Optional[str] = None
    budget_id: Optional[str] = None


class AgentReviewRead(BaseModel):
    id: str
    task_run_id: str
    iteration_number: int
    decision: ReviewDecision
    summary: Optional[str] = None
    issues: List[str] = []
    repair_instructions: Optional[str] = None
    parse_error: Optional[str] = None
    candidate_agent_run_id: str
    candidate_artifact_id: str
    candidate_artifact_hash: Optional[str] = None
    reviewer_agent_run_id: str
    reviewer_agent_version_id: str
    reviewer_provider_model_snapshot_id: Optional[str] = None
    created_at: datetime


class TaskRunUsageTotal(BaseModel):
    agent_run_id: str
    role: Optional[str] = None
    tokens_in: int
    tokens_out: int
    cost_amount: Decimal


class ReviewedTaskRunSummary(BaseModel):
    """One consolidated read view of a BUILD_REVIEW Task Run's review
    cycle -- configuration, every Agent Run/candidate/review it produced,
    the final artifact (if any), and aggregate usage/cost. Reuses
    AgentRunRead/ArtifactRead as-is; adds only what a review cycle needs
    beyond a single Agent Run."""

    task_run_id: str
    status: str
    outcome: str  # "accepted" | "repair_limit_exhausted" | "reviewer_response_invalid" | "execution_failed" | "cancelled" | "in_progress"
    primary_agent_version_id: Optional[str] = None
    reviewer_agent_version_id: Optional[str] = None
    max_repair_iterations: Optional[int] = None
    agent_runs: List[AgentRunRead]
    reviews: List[AgentReviewRead]
    final_artifact: Optional[ArtifactRead] = None
    usage: List[TaskRunUsageTotal]
    total_cost: Decimal
