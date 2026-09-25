"""AIL.5C test factories: concepts, definitions, learners, project work, runs and
a scripted fake Grader provider. Kept apart from ``tests/conftest.py`` (shared
infrastructure) like ``ail1a_factories``."""

import json
import re
from decimal import Decimal
from typing import Callable, List, Optional
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
    MilestoneAttempt,
    ProjectAttempt,
)
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
REQ_MOD = {
    "requires_all": [
        {"evidence_type": "knowledge_check", "min_passed": 1},
        {
            "evidence_type": "modification",
            "min_passed": 1,
            "max_assistance": "h2",
            "min_verification": "platform_verified",
        },
    ]
}


def make_ail_concept(
    db, slug=None, kind=ConceptKind.MECHANISM, requirements=None, name="Structured Output", core=False
):
    concept = make_concept(db, slug=slug or f"c-{uuid4().hex[:8]}", name=name, kind=kind, is_core=core)
    version = make_published_version(
        db,
        concept,
        plain_definition="Structured output constrains a model to a schema.",
        evidence_requirements=requirements or REQ_KC,
    )
    return concept, version


def _choice(n: int) -> dict:
    return {
        "entry_key": f"q{n}",
        "prompt": f"Question {n}?",
        "options": ["wrong", "right", "also wrong"],
        "answer_key": [1],
    }


def kc_definition(
    db, author, concept, *, key="kc-structured-output", pool=6, draw=2, publish=True, links_extra=None
):
    """Knowledge check: deterministic only, inline authored pool."""
    criteria = [
        {
            "key": "answers_correct",
            "label": "You answered the questions correctly",
            "method": "deterministic",
            "required": True,
            "description": "Every drawn question is answered correctly.",
            "check": {"type": "choice_match", "min_correct": "all"},
            "on_not_met": [
                {"kind": "learning_item", "ref": "review-the-concept", "label": "Review the lesson"}
            ],
        }
    ]
    defn = AssessmentDefinitionService(db).create_draft(
        author_user_id=author.id,
        definition_key=key,
        kind=AssessmentKind.KNOWLEDGE_CHECK,
        title="Structured output check",
        instructions_md="Answer the questions.",
        criteria=criteria,
        concept_links=[{"concept_id": concept.id, "criterion_keys": ["answers_correct"]}]
        + (links_extra or []),
        challenge_spec={"entry_kind": "choice", "draw_size": draw, "pool": [_choice(i) for i in range(pool)]},
        allowed_resources=["Your lessons"],
    )
    return AssessmentDefinitionService(db).publish(defn.id) if publish else defn


def explain_definition(
    db, author, concept, *, key="eb-structured-output", crosscheck="deciding", publish=True
):
    """Explain-back: one deterministic length check + two required judged criteria."""
    criteria = [
        {
            "key": "long_enough",
            "label": "Your explanation has enough detail",
            "method": "deterministic",
            "required": True,
            "check": {"type": "length_bounds", "field": "fields.explanation", "min_chars": 30},
        },
        {
            "key": "accuracy",
            "label": "Your explanation is accurate",
            "method": "grader",
            "required": True,
            "description": "Explains what structured output is.",
            "anchors": {"met": "Correct and specific"},
            "reference_points": ["Structured output constrains the model to a schema"],
            "facts": ["long_enough"],
            "on_not_met": [
                {"kind": "learning_item", "ref": "structured-output-lesson", "label": "Revisit the lesson"}
            ],
        },
        {
            "key": "limits",
            "label": "You state a limit",
            "method": "grader",
            "required": True,
            "reference_points": ["A schema does not guarantee the content is true"],
            "on_not_met": [
                {
                    "kind": "learning_item",
                    "ref": "structured-output-limits",
                    "label": "Re-read the limits section",
                }
            ],
        },
    ]
    prompts = [
        {
            "entry_key": f"p{i}",
            "prompt_md": f"Explain structured output ({i}).",
            "reference_points": [f"point {i}"],
        }
        for i in range(3)
    ]
    defn = AssessmentDefinitionService(db).create_draft(
        author_user_id=author.id,
        definition_key=key,
        kind=AssessmentKind.EXPLAIN_BACK,
        title="Explain structured output",
        instructions_md="Explain it in your own words.",
        criteria=criteria,
        concept_links=[{"concept_id": concept.id, "criterion_keys": ["long_enough", "accuracy", "limits"]}],
        challenge_spec={"entry_kind": "prompt", "draw_size": 1, "pool": prompts},
        grading_policy={"crosscheck": crosscheck},
    )
    return AssessmentDefinitionService(db).publish(defn.id) if publish else defn


def modification_definition(db, author, concept, *, key="mod-structured-output", publish=True):
    """Modification challenge: deterministic run checks against a seeded variant."""
    criteria = [
        {
            "key": "ran",
            "label": "You ran your changed agent",
            "method": "deterministic",
            "required": True,
            "check": {"type": "run_exists_owned_terminal", "field": "run_ids", "min": 1},
        },
        {
            "key": "fresh_run",
            "label": "The run was made during the challenge",
            "method": "deterministic",
            "required": True,
            "check": {"type": "run_after_challenge_start", "field": "run_ids", "min": 1},
        },
        {
            "key": "used_input",
            "label": "The run used the challenge input",
            "method": "deterministic",
            "required": True,
            "check": {
                "type": "inputs_match_challenge",
                "field": "run_ids",
                "expected_path": "parameters.input_text",
            },
            "on_not_met": [
                {"kind": "milestone", "ref": "p2-milestone-3", "label": "Rebuild with the new input"}
            ],
        },
    ]
    variants = [
        {
            "entry_key": f"v{i}",
            "title": f"Variant {i}",
            "statement_md": f"Change your agent to handle input {i}.",
            "parameters": {"input_text": f"invoice-{i}-total"},
        }
        for i in range(3)
    ]
    defn = AssessmentDefinitionService(db).create_draft(
        author_user_id=author.id,
        definition_key=key,
        kind=AssessmentKind.MODIFICATION,
        title="Modify your extractor",
        instructions_md="Adapt your agent to a new requirement.",
        criteria=criteria,
        concept_links=[{"concept_id": concept.id, "criterion_keys": ["ran", "fresh_run", "used_input"]}],
        challenge_spec={"entry_kind": "variant", "draw_size": 1, "pool": variants},
    )
    return AssessmentDefinitionService(db).publish(defn.id) if publish else defn


# -- learners / project work --------------------------------------------------------------------


def second_user(db, org, email="learner-b@example.com"):
    user = make_user(db, org=org, email=email)
    db.commit()
    return user


def make_project_attempt(
    db,
    user,
    template,
    *,
    levels=(AssistanceLevel.H1,),
    study=False,
    submit=True,
    verified=True,
    variant_after_h5=False,
):
    """A submitted 5B project attempt with milestone attempts at the given help levels."""
    from app.models.academy import ProjectMilestone

    pa = ProjectAttempt(
        user_id=user.id, project_template_id=template.id, brief_snapshot={"title": template.title}
    )
    db.add(pa)
    db.flush()
    milestone = db.query(ProjectMilestone).filter_by(project_template_id=template.id).first()
    mas = []
    for level in levels:
        ma = MilestoneAttempt(
            project_attempt_id=pa.id,
            project_milestone_id=milestone.id,
            status=MilestoneAttemptStatus.PASSED,
            max_assistance_level=level,
            mode=MilestoneAttemptMode.STUDY if study else MilestoneAttemptMode.NORMAL,
        )
        db.add(ma)
        mas.append(ma)
    if variant_after_h5:  # an independent Study Mode variant after an H5 attempt
        db.add(
            MilestoneAttempt(
                project_attempt_id=pa.id,
                project_milestone_id=milestone.id,
                status=MilestoneAttemptStatus.PASSED,
                max_assistance_level=AssistanceLevel.H0,
                mode=MilestoneAttemptMode.VARIANT,
            )
        )
    db.flush()
    if submit:
        db.add(
            AssessmentReadySubmission(
                user_id=user.id,
                project_attempt_id=pa.id,
                snapshot={"project_attempt_id": pa.id, "milestone_attempt_ids": [m.id for m in mas]},
            )
        )
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
        pm = make_provider_model(
            db,
            model=model,
            provider=provider,
            cost_input_per_mtok=Decimal(0),
            cost_output_per_mtok=Decimal(0),
        )
        ids.append(pm.id)
    db.commit()
    return ids


_RESPONSE_DATA = re.compile(
    r"<<<LEARNER RESPONSE DATA[^>]*>>>\n(.*?)\n<<<END LEARNER RESPONSE DATA>>>", re.DOTALL
)
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
        quote = blocks[0][:24] if blocks else ""
        n = len(self.requests)
        result = self.script(n, keys, quote) if self.script else default_findings(keys, quote)
        if isinstance(result, Exception):
            raise result
        text = result if isinstance(result, str) else json.dumps({"criteria": result})
        return InvokeResponse(
            text=text,
            tokens_in=100,
            tokens_out=50,
            tokens_total=150,
            latency_ms=2,
            provider_http_status=200,
            finish_reason="stop",
        )


def judgment(
    key, quote, finding="met", confidence="high", gap=None, rationale="The response addresses this."
):
    if finding in ("partial", "not_met") and gap is None:
        gap = "A required idea is missing."
    return {
        "key": key,
        "finding": finding,
        "confidence": confidence,
        "rationale": rationale,
        "quotes": [quote] if finding in ("met", "partial") and quote else [],
        "gap": gap,
    }


def default_findings(keys, quote, **kw):
    return [judgment(k, quote, **kw) for k in keys]


# -- MA6 evaluation runs / Personal Lab experiments (platform facts) ----------------------------------------------


def make_evaluation_run(
    db, user, findings, *, agent_version=None, created_at=None, status=None, task_project=None
):
    """A COMPLETED MA6 EvaluationRun requested by ``user`` with one criterion
    result per finding (``["met", "not_met", ...]``). Read-only fact source."""
    from app.db.enums import EvaluationFinding, EvaluationMethod, EvaluationRunStatus
    from app.models.evaluation_definitions import EvaluationCriterion
    from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
    from tests.conftest import make_artifact, make_evaluation_definition_version

    run = make_agent_run(db, agent_version=agent_version)
    artifact = make_artifact(db, run, content_hash=uuid4().hex + uuid4().hex[:32])
    version = make_evaluation_definition_version(db)
    evaluation = EvaluationRun(
        subject_agent_run_id=run.id,
        subject_artifact_id=artifact.id,
        subject_artifact_content_hash=artifact.content_hash,
        evaluation_definition_version_id=version.id,
        method=EvaluationMethod.DETERMINISTIC,
        status=status or EvaluationRunStatus.COMPLETED,
        requested_by_user_id=user.id,
    )
    db.add(evaluation)
    db.flush()
    if created_at is not None:
        evaluation.created_at = created_at
    for i, finding in enumerate(findings):
        criterion = (
            db.query(EvaluationCriterion).filter_by(evaluation_definition_version_id=version.id).first()
        )
        if i > 0:
            criterion = EvaluationCriterion(
                evaluation_definition_version_id=version.id, key=f"c{i}", label=f"C{i}", order_index=i
            )
            db.add(criterion)
            db.flush()
        db.add(
            EvaluationCriterionResult(
                evaluation_run_id=evaluation.id,
                evaluation_criterion_id=criterion.id,
                criterion_key=criterion.key,
                order_index=i,
                finding=EvaluationFinding(finding),
                rationale="platform",
            )
        )
    db.commit()
    return evaluation


def make_experiment(
    db, user, *, concluded=True, labels=None, conclusion="I think variant A was better, but n was small."
):
    """A COMPLETED experiment owned by ``user`` with per-label evaluated runs:
    ``labels={"A": ["met", "met"], "B": ["met", "not_met"]}``."""
    from datetime import datetime, timezone

    from app.db.enums import ExperimentStatus, ExperimentType
    from app.models.lab import Experiment, ExperimentTaskRun

    experiment = Experiment(
        user_id=user.id,
        experiment_type=ExperimentType.MODEL_COMPARISON,
        status=ExperimentStatus.COMPLETED,
        config_snapshot={},
        repetitions=1,
        hypothesis="Which variant is better?",
        conclusion_text=conclusion if concluded else None,
        conclusion_type="supported" if concluded else None,
        concluded_at=datetime.now(timezone.utc) if concluded else None,
    )
    db.add(experiment)
    db.flush()
    for position, (label, findings) in enumerate((labels or {}).items()):
        task_run = make_task_run(db, status=TaskRunStatus.COMPLETED)
        run = make_agent_run(db, task_run=task_run, status=AgentRunStatus.COMPLETED)
        db.add(
            ExperimentTaskRun(
                experiment_id=experiment.id,
                task_run_id=task_run.id,
                task_position=position,
                repetition=0,
                label=label,
            )
        )
        evaluation = make_evaluation_run(db, user, findings)
        evaluation.subject_agent_run_id = run.id
    db.commit()
    return experiment


def project_definition(
    db, author, concept, template, *, key="proj-extractor", fresh="if_assisted", challenge=False, publish=True
):
    """A project assessment judged deterministically from the frozen 5B hand-off."""
    from app.assessment_contract import DEFAULT_GRADING_POLICY  # noqa: F401

    criteria = [
        {
            "key": "milestones",
            "label": "Every milestone has real evidence",
            "method": "deterministic",
            "required": True,
            "check": {"type": "milestones_evidenced", "min": "all"},
            "on_not_met": [
                {
                    "kind": "milestone",
                    "ref": "finish-milestones",
                    "label": "Finish the milestones with real results",
                }
            ],
        },
        {
            "key": "evaluated",
            "label": "Your app was evaluated and met the criteria",
            "method": "deterministic",
            "required": True,
            "check": {"type": "evaluation_run_findings", "field": "evaluation_run_ids", "after_start": False},
        },
        {
            "key": "frozen",
            "label": "Your submission is unchanged",
            "method": "deterministic",
            "required": True,
            "check": {"type": "manifest_unchanged"},
        },
    ]
    spec = None
    if challenge:
        spec = {
            "entry_kind": "variant",
            "draw_size": 1,
            "pool": [
                {
                    "entry_key": f"pv{i}",
                    "title": f"Variant {i}",
                    "statement_md": "Rerun your project on a new input.",
                    "parameters": {"input_text": f"pv-input-{i}"},
                }
                for i in range(3)
            ],
        }
        criteria += [
            {
                "key": "fresh_run",
                "label": "You ran your project during the challenge",
                "method": "deterministic",
                "required": True,
                "check": {"type": "run_after_challenge_start", "field": "run_ids", "min": 1},
            },
            {
                "key": "used_input",
                "label": "The run used the challenge input",
                "method": "deterministic",
                "required": True,
                "check": {
                    "type": "inputs_match_challenge",
                    "field": "run_ids",
                    "expected_path": "parameters.input_text",
                },
            },
        ]
    defn = AssessmentDefinitionService(db).create_draft(
        author_user_id=author.id,
        definition_key=key,
        kind=AssessmentKind.PROJECT,
        title="Assess your extractor project",
        instructions_md="Assessment of your submitted project.",
        criteria=criteria,
        concept_links=[{"concept_id": concept.id, "criterion_keys": [c["key"] for c in criteria]}],
        challenge_spec=spec,
        independence_policy={"fresh_required": fresh},
        project_template_id=template.id,
    )
    return AssessmentDefinitionService(db).publish(defn.id) if publish else defn


def add_milestone_evidence(db, user, project_attempt, concept, version, *, verified=True):
    """Real learning evidence on every milestone attempt of a project attempt,
    at the help level already recorded on that milestone attempt."""
    from app.db.enums import EvidenceType
    from app.services.learning_evidence_service import LearningEvidenceService

    ids = []
    for ma in db.query(MilestoneAttempt).filter_by(project_attempt_id=project_attempt.id).all():
        if ma.max_assistance_level == AssistanceLevel.H5:
            continue  # 5B never qualifies H5 work as evidence
        row = LearningEvidenceService(db).record_evidence(
            user_id=user.id,
            concept_id=concept.id,
            concept_version_id=version.id,
            evidence_type=EvidenceType.LAB,
            grader=GradingMode.DETERMINISTIC if verified else GradingMode.SELF,
            passed=True,
            assistance_level=ma.max_assistance_level,
            execution_verification=(
                ExecutionVerification.PLATFORM_VERIFIED if verified else ExecutionVerification.SELF_REPORTED
            ),
            milestone_attempt_id=ma.id,
        )
        ids.append(row.id)
    return ids
