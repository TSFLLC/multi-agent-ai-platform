from typing import Optional

from pydantic import BaseModel

from app.db.enums import EvaluationHumanDecision, EvaluationJudgeSource


class EvaluationRead(BaseModel):
    id: str
    agent_run_id: str
    objective_metrics: Optional[dict] = None
    judge_score: Optional[dict] = None
    judge_source: Optional[EvaluationJudgeSource] = None
    human_decision: Optional[EvaluationHumanDecision] = None
