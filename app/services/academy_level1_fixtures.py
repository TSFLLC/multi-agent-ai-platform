"""Server-controlled Level 1 fixtures over the existing execution engines.

These are curriculum fixtures, not new runtimes.  Their durable definition is
kept in the existing Agent/Task, ProjectTemplate, Experiment, and MA7
Workflow rows so every launch retains the normal platform provenance.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from functools import wraps
from threading import RLock
from typing import Any, Dict
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import (
    AssistanceLevel,
    DefaultModelStrategy,
    EvidenceRefType,
    EvidenceType,
    ExecutionMode,
    ExecutionVerification,
    GradingMode,
    AgentRunStatus,
    ProjectAudienceLevel,
    ProjectLadderLevel,
    ProjectTemplateBuildMode,
    VersionStatus,
    WorkflowNodeType,
)
from app.models.academy import MilestoneAttempt, ProjectMilestone, ProjectTemplate, ProjectTemplateConceptLink
from app.models.agents import Agent, AgentVersion
from app.models.artifacts_eval import Artifact
from app.models.concepts import LearningItem
from app.models.identity import User
from app.models.tasks import Task, TaskRun
from app.models.workflow import Workflow, WorkflowVersion
from app.models.workflow import WorkflowRun, WorkflowNodeRun
from app.services.agent_registry_service import AgentRegistryService
from app.services.system_project_service import ensure_ail_system_project
from app.services.task_service import TaskService
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.concept_graph_service import ConceptGraphService
from app.services.learning_evidence_service import LearningEvidenceService


DAY10_FIXTURE = "level1-day10-structured-extraction"
DAY15_FIXTURE = "level1-day15-bounded-grounding"
DAY19_FIXTURE = "level1-day19-agent-handoff-workflow"
DAY20_FIXTURE = "level1-day20-broken-system-evaluation"
DAY14_TEMPLATE = "level1-day14-small-ai-application"
_FIXTURE_PROVISION_LOCK = RLock()


def _serialized_fixture_provision(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _FIXTURE_PROVISION_LOCK:
            return fn(*args, **kwargs)
    return wrapped


def _agent(db: Session, user: User, *, name: str, role: str, description: str, prompt: str) -> AgentVersion:
    project = ensure_ail_system_project(db, user)
    agent = db.execute(select(Agent).where(Agent.project_id == project.id, Agent.name == name)).scalar_one_or_none()
    registry = AgentRegistryService(db)
    if agent is None:
        agent = registry.create_agent(project_id=project.id, name=name, role=role, description=description)
    versions = registry.list_agent_versions(agent_id=agent.id)
    active = [v for v in versions if v.status == VersionStatus.ACTIVE]
    if active:
        return sorted(active, key=lambda v: v.version)[-1]
    prompt_versions = registry.list_prompt_versions(agent_id=agent.id)
    pv = prompt_versions[-1] if prompt_versions else registry.create_prompt_version(agent_id=agent.id, content=prompt, created_by=user.id)
    version = registry.create_agent_version(
        agent_id=agent.id,
        name=name,
        role=role,
        description=description,
        prompt_version_id=pv.id,
        capabilities=["text_generation", "structured_output"],
        model_policy={"mode": "auto", "auto_policy": "any"},
        default_model_strategy=DefaultModelStrategy.AUTO_PREFERRED,
    )
    return registry.publish_agent_version(version.id)


def _task(db: Session, user: User, *, project_id: str, title: str, fixture: str, requirements: dict, mode: ExecutionMode) -> Task:
    existing = db.execute(select(Task).where(Task.project_id == project_id, Task.title == title)).scalars().first()
    if existing:
        return existing
    return TaskService(db).create_task(
        project_id=project_id,
        title=title,
        description=f"Curriculum-owned Level 1 fixture: {fixture}",
        execution_mode=mode,
        requirements={"academy_fixture": fixture, "fixture_version": 1, **requirements},
        created_by=user.id,
    )


@_serialized_fixture_provision
def ensure_day10(db: Session, user: User, item: LearningItem) -> Dict[str, Any]:
    agent = _agent(
        db, user, name="Level 1 Structured Extractor", role="structured_extractor",
        description="Level 1 server-controlled structured extraction Agent; immutable curriculum fixture.",
        prompt=("Extract only the fields in the supplied schema from the learner's text. "
                 "Return valid JSON, never prose, and use null when a field is absent."),
    )
    project = ensure_ail_system_project(db, user)
    task = _task(db, user, project_id=project.id, title="Level 1 Day 10 Structured Extraction", fixture=DAY10_FIXTURE,
                 mode=ExecutionMode.SINGLE_AGENT, requirements={"learning_item_id": item.id, "expected_schema": {"name": "string", "email": "string", "amount": "number"}, "cases": [
                     {"id": "invoice-01", "input": "Ada Lovelace, ada@example.com, paid $42", "expected": {"name": "Ada Lovelace", "email": "ada@example.com", "amount": 42}},
                     {"id": "invoice-02", "input": "Grace Hopper paid 19 dollars; grace@example.com", "expected": {"name": "Grace Hopper", "email": "grace@example.com", "amount": 19}},
                 ]})
    return {"fixture_key": DAY10_FIXTURE, "agent_version": agent, "task": task, "project_id": project.id}


@_serialized_fixture_provision
def ensure_day15(db: Session, user: User, item: LearningItem) -> Dict[str, Any]:
    grounded = _agent(db, user, name="Level 1 Bounded Grounding", role="grounding_reader", description="Uses only supplied bounded context; not vector RAG.", prompt="Answer only from the supplied source context and quote the supporting fact.")
    ungrounded = _agent(db, user, name="Level 1 Ungrounded Comparison", role="grounding_comparison", description="Comparison arm for the bounded grounding lesson; not retrieval.", prompt="Answer the question without supplied context and mark unsupported claims.")
    project = ensure_ail_system_project(db, user)
    task = _task(db, user, project_id=project.id, title="Level 1 Day 15 Bounded Grounding", fixture=DAY15_FIXTURE,
                 mode=ExecutionMode.SINGLE_AGENT, requirements={"learning_item_id": item.id, "not_vector_rag": True, "source_fixture": {"version": 1, "text": "The Cedar Library opens at 9:00 AM on weekdays. It is closed on Sundays. The library offers quiet study rooms."}, "questions": [{"id": "hours", "question": "When does the Cedar Library open on weekdays?", "supporting_fact": "opens at 9:00 AM"}, {"id": "unsupported", "question": "How many books can a visitor borrow?", "supporting_fact": None}]})
    return {"fixture_key": DAY15_FIXTURE, "grounded_agent_version": grounded, "ungrounded_agent_version": ungrounded, "task": task, "project_id": project.id}


@_serialized_fixture_provision
def ensure_day19(db: Session, user: User, item: LearningItem) -> Dict[str, Any]:
    first = _agent(db, user, name="Level 1 Workflow Analyzer", role="workflow_analyzer", description="First controlled Agent in the Day 19 handoff workflow.", prompt="Extract the important facts and label the handoff for the reviewer.")
    second = _agent(db, user, name="Level 1 Workflow Reviewer", role="workflow_reviewer", description="Second controlled Agent in the Day 19 handoff workflow.", prompt="Review the analyzer handoff and produce a concise final recommendation.")
    project = ensure_ail_system_project(db, user)
    workflow = db.execute(select(Workflow).where(Workflow.project_id == project.id, Workflow.name == "Level 1 Agent Handoff Workflow")).scalar_one_or_none()
    definition = WorkflowDefinitionService(db)
    if workflow is None:
        workflow = definition.create_workflow(project.id, "Level 1 Agent Handoff Workflow", "Published curriculum-owned Day 19 workflow.")
        n1 = definition.add_node(workflow.id, 1, "analyze", WorkflowNodeType.AGENT, {"agent_version_id": first.id})
        n2 = definition.add_node(workflow.id, 1, "review", WorkflowNodeType.AGENT, {"agent_version_id": second.id})
        definition.add_edge(workflow.id, 1, n1.id, n2.id)
        wv = definition.publish_version(workflow.id, 1)
    else:
        wv = definition.get_latest_version(workflow.id)
    task = _task(db, user, project_id=project.id, title="Level 1 Day 19 Agent Workflow Input", fixture=DAY19_FIXTURE,
                 mode=ExecutionMode.WORKFLOW, requirements={"learning_item_id": item.id, "inspect_handoffs": True, "workflow_id": workflow.id})
    return {"fixture_key": DAY19_FIXTURE, "workflow": workflow, "workflow_version": wv, "task": task, "project_id": project.id}


@_serialized_fixture_provision
def ensure_day20(db: Session, user: User, item: LearningItem) -> Dict[str, Any]:
    baseline = _agent(db, user, name="Level 1 Broken System Baseline", role="broken_system_baseline", description="Intentionally omits the required source constraint for Day 20.", prompt="Answer quickly without checking whether claims are supported.")
    improved = _agent(db, user, name="Level 1 Improved System", role="broken_system_improved", description="Permitted improved version for the Day 20 comparison.", prompt="Answer only with claims supported by the supplied context and identify missing evidence.")
    project = ensure_ail_system_project(db, user)
    task = _task(db, user, project_id=project.id, title="Level 1 Day 20 Broken System Evaluation", fixture=DAY20_FIXTURE,
                 mode=ExecutionMode.SINGLE_AGENT, requirements={"learning_item_id": item.id, "requires_baseline_and_improved": True, "fixture": {"version": 1, "failure": "unsupported claims are not identified", "permitted_change": "add a supplied-context support check"}})
    return {"fixture_key": DAY20_FIXTURE, "baseline_agent_version": baseline, "improved_agent_version": improved, "task": task, "project_id": project.id}


@_serialized_fixture_provision
def ensure_day14_template(db: Session, user: User, item: LearningItem) -> ProjectTemplate:
    existing = db.execute(select(ProjectTemplate).where(ProjectTemplate.template_key == DAY14_TEMPLATE, ProjectTemplate.version == 1)).scalar_one_or_none()
    if existing:
        return existing
    template = ProjectTemplate(
        id=str(uuid4()), template_key=DAY14_TEMPLATE, version=1, status="published",
        title="Level 1 Small AI Application", audience_level=ProjectAudienceLevel.BEGINNER,
        ladder_level=ProjectLadderLevel.L4, build_mode=ProjectTemplateBuildMode.WORKFLOW,
        brief_md="Build a bounded AI application with explicit input/output, a governed Agent execution, representative tests, and an explanation.",
        difficulty_profile={"academy_fixture": DAY14_TEMPLATE}, evidence_requirements={"requires_actual_result": True, "requires_agent_run": True},
        variant_spec={"curriculum": "ail5-level1-practical-ai-foundations-v2", "learning_item_id": item.id},
        est_minutes_min=90, est_minutes_max=180, capstone_eligible=False, author_user_id=user.id, published_at=datetime.utcnow(),
    )
    db.add(template); db.flush()
    db.add(ProjectTemplateConceptLink(id=str(uuid4()), project_template_id=template.id, concept_id=item.concept_id, role="applied"))
    for position, (title, artifact, text) in enumerate([
        ("Define input and output", "requirements", "Define the small problem and expected input/output."),
        ("Build and execute", "working_artifact", "Configure the bounded Agent and capture a real execution result."),
        ("Test and explain", "test_and_explanation", "Run representative cases, inspect failures, and explain the implementation."),
    ], 1):
        db.add(ProjectMilestone(id=str(uuid4()), project_template_id=template.id, position=position, title=title, instructions_md=text, check_spec={"academy_day": 14, "requires_result": position > 1}, learning_item_id=item.id, expected_artifact_kind=artifact, evidence_type="lab", reference_solution_ref=f"academy/{DAY14_TEMPLATE}/v1/{position}"))
    try:
        db.commit()
    except Exception:
        db.rollback()
        winner = db.execute(select(ProjectTemplate).where(ProjectTemplate.template_key == DAY14_TEMPLATE, ProjectTemplate.version == 1)).scalar_one_or_none()
        if winner is None:
            raise
        return winner
    db.refresh(template)
    return template


def ensure_day14(db: Session, user: User, item: LearningItem) -> Dict[str, Any]:
    template = ensure_day14_template(db, user, item)
    agent = _agent(db, user, name="Level 1 Small Application Agent", role="small_application_builder", description="Controlled execution Agent for the Day 14 bounded application.", prompt="Run the configured small application, return its structured result, and preserve input/output expectations.")
    project = ensure_ail_system_project(db, user)
    task = _task(db, user, project_id=project.id, title="Level 1 Day 14 Small Application Execution", fixture=DAY14_TEMPLATE, mode=ExecutionMode.SINGLE_AGENT, requirements={"learning_item_id": item.id, "project_template_id": template.id, "expected_input": "short user request", "expected_output": "structured application response"})
    return {"fixture_key": DAY14_TEMPLATE, "template": template, "agent_version": agent, "task": task, "project_id": project.id}


def qualify_agent_fixture(db: Session, user: User, agent_run_id: str, assistance: str, conclusion: str | None = None, observed_change: str | None = None):
    """Qualify only a completed, artifact-producing curriculum Agent run.

    This is deliberately a bounded adapter: the Agent worker remains the
    execution authority, while this method only verifies its durable output
    and writes through the canonical evidence service.
    """
    run = db.get(__import__("app.models.tasks", fromlist=["AgentRun"]).AgentRun, agent_run_id)
    if run is None or run.status != AgentRunStatus.COMPLETED:
        raise ValueError("The governed Agent run is not completed")
    task_run = db.get(TaskRun, run.task_run_id)
    task = task_run.task
    if task.created_by != user.id:
        raise ValueError("Agent run is not owned by the learner")
    fixture = (task.requirements or {}).get("academy_fixture")
    artifact = db.execute(select(Artifact).where(Artifact.agent_run_id == run.id).order_by(Artifact.created_at.desc())).scalars().first()
    if artifact is None or not artifact.storage_ref or not Path(artifact.storage_ref).exists():
        raise ValueError("The governed Agent run has no durable output artifact")
    raw = Path(artifact.storage_ref).read_text(encoding="utf-8")
    if not raw.strip():
        raise ValueError("The governed Agent run produced an empty result")
    if fixture == DAY10_FIXTURE:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Day 10 output is not valid structured JSON") from exc
        expected = (task.requirements or {}).get("cases", [])
        if not any(all(parsed.get(k) == v for k, v in case["expected"].items()) for case in expected):
            raise ValueError("Day 10 output does not satisfy an authored extraction case")
    elif fixture == DAY20_FIXTURE:
        if not task_run.config_snapshot or task_run.config_snapshot.get("run_role") != "improved":
            raise ValueError("Day 20 qualification requires the improved run")
        if not task_run.config_snapshot.get("baseline_task_run_id") or not conclusion or not observed_change:
            raise ValueError("Day 20 requires linked baseline, improved result, and learner comparison")
    elif fixture == DAY15_FIXTURE and not conclusion:
        raise ValueError("Day 15 requires a learner-supported/unsupported conclusion")
    level = AssistanceLevel(assistance.lower())
    item_id = (task.requirements or {}).get("learning_item_id")
    item = db.get(LearningItem, item_id)
    if item is None:
        raise ValueError("Fixture is not bound to a current Learning Item")
    concept_version = ConceptGraphService(db).get_current_version(item.concept_id)
    milestone_attempt_id = None
    if fixture == DAY14_TEMPLATE:
        frozen = (task_run.config_snapshot or {}).get("frozen_task_snapshot") or {}
        project_attempt_id = frozen.get("project_attempt_id")
        if not project_attempt_id:
            raise ValueError("Day 14 qualification requires the owned Build With Me ProjectAttempt")
        milestone_attempt_id = db.execute(
            select(MilestoneAttempt.id)
            .join(ProjectMilestone, ProjectMilestone.id == MilestoneAttempt.project_milestone_id)
            .where(MilestoneAttempt.project_attempt_id == project_attempt_id, ProjectMilestone.project_template_id == (task.requirements or {}).get("project_template_id"), ProjectMilestone.position == 2)
        ).scalar_one_or_none()
        if milestone_attempt_id is None:
            raise ValueError("Day 14 Build With Me execution milestone is not provisioned")
    evidence = LearningEvidenceService(db).record_evidence(
        user_id=user.id, concept_id=item.concept_id, concept_version_id=concept_version.id,
        learning_item_id=item.id, evidence_type=EvidenceType.LAB, grader=GradingMode.DETERMINISTIC,
        passed=True, ref_type=EvidenceRefType.AGENT_RUN, ref_id=run.id,
        score={"fixture": fixture, "conclusion": conclusion, "observed_change": observed_change, "project_attempt_id": ((task_run.config_snapshot or {}).get("frozen_task_snapshot") or {}).get("project_attempt_id")},
        assistance_level=level, execution_verification=ExecutionVerification.PLATFORM_VERIFIED,
        milestone_attempt_id=milestone_attempt_id,
    )
    snapshot = dict(task_run.config_snapshot or {})
    snapshot["learner_conclusion"] = conclusion
    snapshot["observed_change"] = observed_change
    task_run.config_snapshot = snapshot
    db.commit()
    return evidence


def qualify_workflow_fixture(db: Session, user: User, workflow_run_id: str, assistance: str, conclusion: str | None = None):
    run = db.get(WorkflowRun, workflow_run_id)
    if run is None or run.status.value != "completed":
        raise ValueError("The governed MA7 Workflow run is not completed")
    task_run = run.task_run
    task = task_run.task
    if task.created_by != user.id or (task.requirements or {}).get("academy_fixture") != DAY19_FIXTURE:
        raise ValueError("Workflow run is not an owned Day 19 curriculum run")
    node_runs = db.execute(select(WorkflowNodeRun).where(WorkflowNodeRun.workflow_run_id == run.id)).scalars().all()
    if not node_runs or any(node.status.value != "completed" for node in node_runs):
        raise ValueError("Every Day 19 workflow node must complete before qualification")
    if not conclusion:
        raise ValueError("Day 19 requires a learner explanation of the handoffs")
    level = AssistanceLevel(assistance.lower())
    item = db.get(LearningItem, (task.requirements or {}).get("learning_item_id"))
    if item is None:
        raise ValueError("Workflow is not bound to a current Learning Item")
    concept_version = ConceptGraphService(db).get_current_version(item.concept_id)
    return LearningEvidenceService(db).record_evidence(
        user_id=user.id, concept_id=item.concept_id, concept_version_id=concept_version.id,
        learning_item_id=item.id, evidence_type=EvidenceType.LAB, grader=GradingMode.DETERMINISTIC,
        passed=True, ref_type=EvidenceRefType.WORKFLOW_RUN, ref_id=run.id,
        score={"fixture": DAY19_FIXTURE, "completed_nodes": len(node_runs), "conclusion": conclusion},
        assistance_level=level, execution_verification=ExecutionVerification.PLATFORM_VERIFIED,
    )
