"""Evaluation Service — Section 11, 17. Runs objective checks, optionally
invokes the Judge Agent, stores Evaluation records. Objective metrics are
primary; judge_score is optional/additive, never merged into one score
(ADR-4). Real implementation: MA5 (objective), MA6 (Judge Agent)."""

from app.services.base import BaseService


class EvaluationService(BaseService):
    def run_objective_checks(self, *args, **kwargs):
        raise NotImplementedError("EvaluationService objective checks land in MA5")

    def run_judge(self, *args, **kwargs):
        raise NotImplementedError("EvaluationService Judge Agent lands in MA6")
