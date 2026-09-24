"""AIL.4C execution/API coverage with a deterministic fake provider."""

import json
from decimal import Decimal

import pytest

from app.config import settings
from app.errors import NotFoundError
from app.models.observability import ExecutionEvent
from app.providers.base import InvokeResponse, ProviderConnectionError
from app.schemas.professor import ProfessorContextRequest, ProfessorIntent
from app.services.professor_execution_service import ProfessorExecutionService
from tests.conftest import make_model, make_provider, make_provider_model


class FakeProfessorAdapter:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def invoke(self, request):
        self.calls.append(request)
        return InvokeResponse(
            text=self.response,
            tokens_in=120,
            tokens_out=80,
            tokens_total=200,
            latency_ms=3,
            provider_http_status=200,
            finish_reason="stop",
        )


class RateLimitedProfessorAdapter:
    def invoke(self, request):
        raise ProviderConnectionError("OpenRouter provider rate limited (HTTP 429).", status_code=429)


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


def test_professor_staging_pin_uses_registry_provider_model_id_only(monkeypatch):
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "professor_staging_provider_model_id", "registry-provider-model-id")

    policy = ProfessorExecutionService._professor_model_policy(
        type("AgentVersionStub", (), {"model_policy": {"mode": "auto", "auto_policy": "prefer_free"}})()
    )

    assert policy == {"mode": "manual", "manual_provider_model_id": "registry-provider-model-id"}


def test_professor_pin_is_inactive_without_staging_configuration(monkeypatch):
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "professor_staging_provider_model_id", "registry-provider-model-id")

    policy = ProfessorExecutionService._professor_model_policy(
        type("AgentVersionStub", (), {"model_policy": {"mode": "auto", "auto_policy": "prefer_free"}})()
    )

    assert policy["mode"] == "auto"
    assert policy["auto_policy"] == "prefer_free"


def test_professor_execution_uses_normal_lineage_and_model_policy(db, bootstrap, monkeypatch):
    model = make_model(db, canonical_model_id="free/professor", structured_output_support=True)
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
    assert adapter.calls[0].response_format == {"type": "json_object"}

    from app.models.execution import ModelCall
    from app.models.tasks import AgentRun, TaskRun

    run = db.get(AgentRun, result.agent_run_id)
    task_run = db.get(TaskRun, result.task_run_id)
    assert run.role.value == "professor"
    assert run.model_id == model.id
    assert task_run.status.value == "completed"
    assert db.query(ModelCall).filter_by(agent_run_id=run.id).one().tokens_out == 80
    event = (
        db.query(ExecutionEvent)
        .filter_by(task_run_id=task_run.id, event_type="model_call.completed")
        .one()
    )
    metadata = json.loads(event.decision_summary)
    assert metadata["registry_model_id"] == "free/professor"
    assert metadata["provider_model_identifier"] == "free/professor"
    assert metadata["structured_output_support"] is True
    assert metadata["response_format_json_object_sent"] is True
    assert metadata["configured_max_tokens"] > 0
    assert metadata["provider_http_status"] == 200
    assert metadata["finish_reason"] == "stop"
    assert metadata["input_tokens"] == 120
    assert metadata["output_tokens"] == 80
    assert metadata["total_tokens"] == 200
    assert metadata["final_content_type"] == "str"
    assert metadata["final_content_length"] == len(_response())
    assert "Start with the recorded" not in event.decision_summary


def test_professor_rate_limit_is_sanitized_but_classified(db, bootstrap):
    model = make_model(db, canonical_model_id="free/professor", structured_output_support=True)
    provider = make_provider(db)
    make_provider_model(db, model=model, provider=provider, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()

    result = ProfessorExecutionService(
        db, adapter_factory=lambda _db, _provider: RateLimitedProfessorAdapter()
    ).create_and_execute(
        bootstrap.user,
        ProfessorContextRequest(intent=ProfessorIntent.ASK_PROFESSOR, question="safe question"),
    )

    assert result.status == "provider_rate_limited"
    assert result.error_kind == "provider_rate_limited"
    assert result.error_message == "The AI provider is temporarily busy. Please try again shortly."
    assert "429" not in result.error_message
    assert "private" not in result.error_message
    task_event = (
        db.query(ExecutionEvent)
        .filter_by(task_run_id=result.task_run_id, event_type="model_call.failed")
        .order_by(ExecutionEvent.sequence_number.desc())
        .first()
    )
    assert task_event is not None
    metadata = json.loads(task_event.decision_summary)
    assert metadata["error_category"] == "provider_connection_error"
    assert metadata["provider_http_status"] == 429
    assert metadata["response_format_json_object_sent"] is True
    assert "safe question" not in task_event.decision_summary


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
