"""AIL.4C execution/API coverage with a deterministic fake provider."""

import json
from decimal import Decimal

import pytest

from app.errors import NotFoundError
from app.providers.base import InvokeResponse
from app.schemas.professor import ProfessorContextRequest, ProfessorIntent
from app.services.professor_execution_service import ProfessorExecutionService
from tests.conftest import make_model, make_provider, make_provider_model


class FakeProfessorAdapter:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def invoke(self, request):
        self.calls.append(request)
        return InvokeResponse(text=self.response, tokens_in=120, tokens_out=80, latency_ms=3)


def _response():
    return json.dumps(
        {
            "intent": "ASK_PROFESSOR",
            "direct_answer": "Start with the recorded learning context and test one idea at a time.",
            "explanation": "This is an advisory explanation based on the context supplied by AIL.",
            "evidence": [],
            "grounded_assertions": [
                {
                    "assertion_kind": "ai_explanation",
                    "text": "This is an advisory explanation based on the context supplied by AIL.",
                    "references": [],
                }
            ],
            "uncertainties": ["The Professor did not use private project content."],
            "suggested_next_actions": [
                {"type": "learn", "target_id": None, "reason": "Choose the next explicit learning action.", "advisory": True}
            ],
            "attachment_references": [],
        }
    )


def test_professor_execution_uses_normal_lineage_and_model_policy(db, bootstrap, monkeypatch):
    model = make_model(db, canonical_model_id="free/professor")
    provider = make_provider(db)
    make_provider_model(
        db,
        model=model,
        provider=provider,
        cost_input_per_mtok=Decimal(0),
        cost_output_per_mtok=Decimal(0),
    )
    db.commit()
    adapter = FakeProfessorAdapter(_response())

    result = ProfessorExecutionService(
        db, adapter_factory=lambda _db, _provider: adapter
    ).create_and_execute(
        bootstrap.user,
        ProfessorContextRequest(intent=ProfessorIntent.ASK_PROFESSOR, question="What should I practice?"),
    )

    assert result.status == "complete"
    assert result.direct_answer.startswith("Start with")
    assert result.agent_run_id and result.artifact_id
    assert adapter.calls
    assert adapter.calls[0].max_tokens > 0

    from app.models.execution import ModelCall
    from app.models.tasks import AgentRun, TaskRun

    run = db.get(AgentRun, result.agent_run_id)
    task_run = db.get(TaskRun, result.task_run_id)
    assert run.role.value == "professor"
    assert run.model_id == model.id
    assert task_run.status.value == "completed"
    assert db.query(ModelCall).filter_by(agent_run_id=run.id).one().tokens_out == 80


def test_professor_continuation_uses_only_explicit_previous_interaction(db, bootstrap):
    from app.services.system_project_service import ensure_ail_system_project

    project = ensure_ail_system_project(db, bootstrap.user)
    db.commit()
    response = ProfessorExecutionService(db)
    with pytest.raises(NotFoundError):
        response.continue_interaction(
            bootstrap.user,
            "not-an-interaction",
            ProfessorContextRequest(intent=ProfessorIntent.ASK_PROFESSOR, question="Explain more simply."),
        )
    assert project.kind.value == "system_ail"


def test_professor_context_preview_does_not_execute_or_mutate_learning(db, bootstrap):
    from sqlalchemy import text

    before = {
        table: db.execute(text(f"select count(*) from {table}")).scalar_one()
        for table in ("learner_profiles", "learning_evidence", "learning_plan_items")
    }
    preview = ProfessorExecutionService(db).preview(
        bootstrap.user,
        ProfessorContextRequest(intent=ProfessorIntent.ASK_PROFESSOR, question="Explain my context."),
    )
    after = {table: db.execute(text(f"select count(*) from {table}")).scalar_one() for table in before}
    assert preview.context.intent == ProfessorIntent.ASK_PROFESSOR
    assert before == after
