from typing import List, Optional

from pydantic import BaseModel

from app.db.enums import ComparisonRunStatus


class ComparisonCandidateCreate(BaseModel):
    agent_run_id: str
    label: str


class ComparisonCandidateRead(BaseModel):
    id: str
    agent_run_id: str
    label: str
    rank: Optional[int] = None
    is_winner: bool


class ComparisonRunCreate(BaseModel):
    task_run_id: str
    candidates: List[ComparisonCandidateCreate]


class ComparisonRunRead(BaseModel):
    id: str
    task_run_id: str
    status: ComparisonRunStatus
    winner_agent_run_id: Optional[str] = None
    candidates: List[ComparisonCandidateRead] = []


class SelectWinnerRequest(BaseModel):
    comparison_candidate_id: str
