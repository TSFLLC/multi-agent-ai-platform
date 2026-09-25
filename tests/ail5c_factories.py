"""AIL.5C test factories: concepts, definitions, learners, project work, runs and
a scripted fake Grader provider. Kept apart from ``tests/conftest.py`` (shared
infrastructure) like ``ail1a_factories``."""

import json
import re
from decimal import Decimal
from typing import Callable, Dict, List, Optional
from uuid import uuid4

from app.db.enums import (
    AgentRunStatus,
    AssessmentKind,
    AssistanceLevel,
    ConceptKind,
    ExecutionVerification,
    GradingMode,
    MilestoneAttemptMode,
    MilestoneAttemptStatus,
    TaskRunStatus,
)
from app.models.academy import (
    AssessmentReadySubmission,
    CandidateEvidence,
    MilestoneAttempt,
    ProjectAttempt,
)
from app.models.tasks import Task
from app.providers.base import InvokeResponse
from app.services.assessment_definition_service import AssessmentDefinitionService
from tests.ail1a_factories import make_concept, make_published_version, make_user
from tests.conftest import (
    make_agent_run,
    make_agent_version,
    make_model,
    make_provider,
    make_provider_model,
    make_task,
    make_task_run,
)

# -- concepts / definitions ---------------------------------------------------------------

REQ_KC = {"requires_all": [{"evidence_type": "knowledge_check", "min_passed": 1}]}
REQ_MOD = {"requires_all": [
    {"evidence_type": "knowledge_check", "min_passed": 1},
    {"evidence_type": "modification", "min_passed": 1, "max_assistance": "h2", "min_verification": "platform_verified"},
]}


def make_ail_concept(db, slug=None, kind=ConceptKind.MECHANISM, requirements=None, name="Structured Output"):
    concept = make_concept(db, slug=slug or f"c-{uuid4().hex[:8]}", name=name, kind=kind)
    version = make_published_version(
        db, concept, plain_definition="Structured output constrains a model to a schema.",
        evidence_requirements=requirements or REQ_KC,
    )
    return concept, version


def _choice(n: int) -> dict:
    return {"entry_key": f"q{n}", "prompt": f"Question {n}?", "options": ["wrong", "right", "also wrong"], "answer_key": [1]}


def kc_definition(db, author, concept, *, key="kc-structured-output", pool=6, draw=2, publish=True, links_extra=None):
    """Knowledge check: deterministic only, inline authored pool."""
    criteria = [{
        "key": "answers_correct", "label": "You answered the questions correctly", "method": "deterministic",
        "required": True, "description": "Every drawn question is answered correctly.",
        "check": {"type": "choice_match", "min_correct": "all"},
        "on_not_met": [{"kind": "learning_item", "ref": "review-the-concept", "label": "Review the lesson"}],
    }]
    defn = AssessmentDefinitionService(db).create_draft(
        author_user_id=author.id, definition_key=key, kind=AssessmentKind.KNOWLEDGE_CHECK,
        title="Structured output check", instructions_md="Answer the questions.", criteria=criteria,
        concept_links=[{"concept_id": concept.id, "criterion_keys": ["answers_correct"]}] + (links_extra or []),
        challenge_spec={"entry_kind": "choice", "draw_size": draw, "pool": [_choice(i) for i in range(pool)]},
        allowed_resources=["Your lessons"],
    )
    return AssessmentDefinitionService(db).publish(defn.id) if publish else defn


def explain_definition(db, author, concept, *, key="eb-structured-output", crosscheck="deciding", publish=True):
    """Explain-back: one deterministic length check + two required judged criteria."""
    criteria = [
        {"key": "long_enough", "label": "Your explanation has enough detail", "method": "deterministic", "required": True,
         "check": {"type": "length_bounds", "field": "fields.explanation", "min_chars": 30}},
        {"key": "accuracy", "label": "Your explanation is accurate", "method": "grader", "required": True,
         "description": "Explains what structured output is.", "anchors": {"met": "Correct and specific"},
         "reference_points": ["Structured output constrains the model to a schema"], "facts": ["long_enough"],
         "on_not_met": [{"kind": "learning_item", "ref": "structured-output-lesson", "label": "Revisit the lesson"}]},
        {"key": "limits", "label": "You state a limit", "method": "grader", "required": True,
         "reference_points": ["A schema does not guarantee the content is true"]},
    ]
    prompts = [{"entry_key": f"p{i}", "prompt_md": f"Explain structured output ({i}).", "reference_points": [f"point {i}"]} for i in range(3)]
    defn = AssessmentDefinitionService(db).create_draft(
        author_user_id=author.id, definition_key=key, kind=AssessmentKind.EXPLAIN_BACK, title="Explain structured output",
        instructions_md="Explain it in your own words.", criteria=criteria,
        concept_links=[{"concept_id": concept.id, "criterion_keys": ["long_enough", "accuracy", "limits"]}],
        challenge_spec={"entry_kind": "prompt", "draw_size": 1, "pool": prompts},
        grading_policy={"crosscheck": crosscheck},
    )
    return AssessmentDefinitionService(db).publish(defn.id) if publish else defn


def modification_definition(db, author, concept, *, key="mod-structured-output", publish=True):
    """Modification challenge: deterministic run checks against a seeded variant."""
    criteria = [
        {"key": "ran", "label": "You ran your changed agent", "method": "deterministic", "required": True,
         "check": {"type": "run_exists_owned_terminal", "field": "run_ids", "min": 1}},
        {"key": "fresh_run", "label": "The run was made during the challenge", "method": "deterministic", "required": True,
         "check": {"type": "run_after_challenge_start", "field": "run_ids", "min": 1}},
        {"key": "used_input", "label": "The run used the challenge input", "method": "deterministic", "required": True,
         "check": {"type": "inputs_match_challenge", "field": "run_ids", "expected_path": "parameters.input_text"},
         "on_not_met": [{"kind": "milestone", "ref": "p2-milestone-3", "label": "Rebuild with the new input"}]},
    ]
    variants = [
        {"entry_key": f"v{i}", "title": f"Variant {i}", "statement_md": f"Change your agent to handle input {i}.",
         "parameters": {"input_text": f"invoice-{i}-total"}} for i in range(3)
    ]
    defn = AssessmentDefinitionService(db).create_draft(
        author_user_id=author.id, definition_key=key, kind=AssessmentKind.MODIFICATION, title="Modify your extractor",
        instructions_md="Adapt your agent to a new requirement.", criteria=criteria,
        concept_links=[{"concept_id": concept.id, "criterion_keys": ["ran", "fresh_run", "used_input"]}],
        challenge_spec={"entry_kind": "variant", "draw_size": 1, "pool": variants},
    )
    return AssessmentDefinitionService(db).publish(defn.id) if publish else defn


# -- learners / project work --------------------------------------------------------------------


def second_user(db, org, email="learner-b@example.com"):
    user = make_user(db, org=org, email=email)
    db.commit()
    return user


def make_project_attempt(db, user, template, *, levels=(AssistanceLevel.H1,), study=False, submit=True, verified=True):
    """A submitted 5B project attempt with milestone attempts at the given help levels."""
    from app.models.academy import ProjectMilestone

    pa = ProjectAttempt(user_id=user.id, project_template_id=template.id, brief_snapshot={"title": template.title})
    db.add(pa)
    db.flush()
    milestone = db.query(ProjectMilestone).filter_by(project_template_id=template.id).first()
    mas = []
    for level in levels:
        ma = MilestoneAttempt(
            project_attempt_id=pa.id, project_milestone_id=milestone.id, status=MilestoneAttemptStatus.PASSED,
            max_assistance_level=level,
            mode=MilestoneAttemptMode.STUDY if study else MilestoneAttemptMode.NORMAL,
        )
        db.add(ma)
        mas.append(ma)
    db.flush()
    if submit:
        db.add(AssessmentReadySubmission(
            user_id=user.id, project_attempt_id=pa.id,
            snapshot={"project_attempt_id": pa.id, "milestone_attempt_ids": [m.id for m in mas]},
        ))
    db.commit()
    return pa


def make_owned_run(db, user, *, project, description="", status=AgentRunStatus.COMPLETED, created_at=None):
    """A completed AgentRun in a Task the learner created."""
    task = make_task(db, project)
    task.created_by = user.id
    task.description = description
    task_run = make_task_run(db, task, status=TaskRunStatus.COMPLETED)
    run = make_agent_run(db, task_run=task_run, agent_version=make_agent_version(db), status=status)
    if created_at is not None:
        run.created_at = created_at
    db.commit()
    return run


# -- scripted Grader provider --------------------------------------------------------------------


def setup_free_models(db, count=2, structured=True):
    """``count`` FREE, structured-output-capable provider models under one provider."""
    provider = make_provider(db)
    ids = []
    for i in range(count):
        model = make_model(db, canonical_model_id=f"free/grader-{i}", structured_output_support=structured)
        pm = make_provider_model(db, model=model, provider=provider, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
        ids.append(pm.id)
    db.commit()
    return ids


_RESPONSE_DATA = re.compile(r"<<<LEARNER RESPONSE DATA[^>]*>>>\n(.*?)\n<<<END LEARNER RESPONSE DATA>>>", re.DOTALL)
_KEYS = re.compile(r'"key": "([a-z][a-z0-9_]+)"')


class ScriptedGrader:
    """A fake provider adapter. ``script(call_no, keys, quote) -> list[dict]`` (or
    a raw string / an Exception to raise). Records every request it receives."""

    def __init__(self, script: Optional[Callable] = None):
        self.script = script
        self.requests: List = []

    def invoke(self, request):
        self.requests.append(request)
        prompt = request.user_prompt
        keys = list(dict.fromkeys(_KEYS.findall(prompt.split("<<<END CRITERIA>>>")[0])))
        blocks = _RESPONSE_DATA.findall(prompt)
        quote = (blocks[0][:24] if blocks else "")
        n = len(self.requests)
        result = self.script(n, keys, quote) if self.script else default_findings(keys, quote)
        if isinstance(result, Exception):
            raise result
        text = result if isinstance(result, str) else json.dumps({"criteria": result})
        return InvokeResponse(text=text, tokens_in=100, tokens_out=50, tokens_total=150, latency_ms=2,
                              provider_http_status=200, finish_reason="stop")


def judgment(key, quote, finding="met", confidence="high", gap=None, rationale="The response addresses this."):
    if finding in ("partial", "not_met") and gap is None:
        gap = "A required idea is missing."
    return {"key": key, "finding": finding, "confidence": confidence, "rationale": rationale,
            "quotes": [quote] if finding in ("met", "partial") and quote else [], "gap": gap}


def default_findings(keys, quote, **kw):
    return [judgment(k, quote, **kw) for k in keys]
