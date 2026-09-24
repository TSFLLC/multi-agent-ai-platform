"""AIL.4C Professor orchestration over the normal Agent execution engine."""

import json
import re
from pathlib import Path
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.enums import AgentRunRole, AgentRunStatus, ExecutionMode, TaskRunStatus, VersionStatus
from app.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.agents import Agent, AgentVersion, PromptVersion
from app.models.artifacts_eval import Artifact
from app.models.concepts import Concept
from app.models.governance import Budget
from app.models.identity import Project, User
from app.models.lab import Experiment
from app.models.learning_review import ReviewAttempt
from app.models.radar import Development, DevelopmentStatus
from app.models.tasks import AgentRun, Task, TaskRun
from app.schemas.professor import (
    ProfessorContext,
    ProfessorContextPreviewRead,
    ProfessorContextRequest,
    ProfessorIntent,
    ProfessorInteractionRead,
    ProfessorResponse,
    ProfessorTargetOption,
    ProfessorTargetOptionsRead,
    ProfessorTargetType,
)
from app.services.flight_recorder import FlightRecorderService
from app.services.professor_context_service import ProfessorContextAssembler
from app.services.professor_contract import ProfessorResponseValidationError, validate_professor_response
from app.services.system_project_service import ensure_ail_system_project, require_ail_system_access
from app.services.task_service import TaskService

_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
PROFESSOR_AGENT_ID = "00000000-0000-0000-0000-000000000108"
PROFESSOR_CONTEXT_KEY = "professor_context"


class ProfessorExecutionError(ConflictError):
    code = "professor_execution_error"


class ProfessorExecutionService:
    def __init__(self, db: Session, adapter_factory=None):
        self.db = db
        self.adapter_factory = adapter_factory

    def preview(self, user: User, request: ProfessorContextRequest) -> ProfessorContextPreviewRead:
        context = ProfessorContextAssembler(self.db).assemble(user.id, request)
        context, truncated = self._bound_context(context)
        return ProfessorContextPreviewRead(
            context=context,
            allowed_sources=[record.role for record in context.records],
            truncated=truncated,
        )

    def target_options(self, user: User, intent: ProfessorIntent) -> ProfessorTargetOptionsRead:
        """Return a small, authorized selector set; never a generic record search."""
        options = []
        if intent == ProfessorIntent.EXPLAIN_THIS:
            concepts = self.db.execute(select(Concept).order_by(Concept.name.asc()).limit(15)).scalars().all()
            developments = self.db.execute(
                select(Development)
                .where(Development.status == DevelopmentStatus.ACTIVE)
                .order_by(Development.first_seen_at.desc())
                .limit(15)
            ).scalars().all()
            options.extend(
                ProfessorTargetOption(type=ProfessorTargetType.CONCEPT, id=item.id, label=item.name)
                for item in concepts
            )
            options.extend(
                ProfessorTargetOption(
                    type=ProfessorTargetType.DEVELOPMENT,
                    id=item.id,
                    label=item.title,
                    subtitle=item.development_type,
                )
                for item in developments
            )
        elif intent == ProfessorIntent.UNDERSTAND_MY_EXPERIMENT:
            experiments = self.db.execute(
                select(Experiment)
                .where(Experiment.user_id == user.id)
                .order_by(Experiment.created_at.desc())
                .limit(40)
            ).scalars().all()
            options.extend(
                ProfessorTargetOption(
                    type=ProfessorTargetType.EXPERIMENT,
                    id=item.id,
                    label=item.hypothesis or "Untitled experiment",
                    subtitle=item.status.value.replace("_", " ").title(),
                )
                for item in experiments
            )
        elif intent == ProfessorIntent.HELP_ME_REVIEW:
            reviews = self.db.execute(
                select(ReviewAttempt, Concept)
                .join(Concept, Concept.id == ReviewAttempt.concept_id)
                .where(ReviewAttempt.user_id == user.id)
                .order_by(ReviewAttempt.started_at.desc())
                .limit(40)
            ).all()
            options.extend(
                ProfessorTargetOption(
                    type=ProfessorTargetType.REVIEW_ATTEMPT,
                    id=attempt.id,
                    label=concept.name,
                    subtitle=f"Review {attempt.status.value}",
                )
                for attempt, concept in reviews
            )
        elif intent == ProfessorIntent.WHY_DOES_THIS_MATTER:
            developments = self.db.execute(
                select(Development)
                .where(Development.status == DevelopmentStatus.ACTIVE)
                .order_by(Development.first_seen_at.desc())
                .limit(40)
            ).scalars().all()
            options.extend(
                ProfessorTargetOption(
                    type=ProfessorTargetType.DEVELOPMENT,
                    id=item.id,
                    label=item.title,
                    subtitle=item.development_type,
                )
                for item in developments
            )
        return ProfessorTargetOptionsRead(intent=intent, options=options[:40])

    def create_and_execute(self, user: User, request: ProfessorContextRequest) -> ProfessorInteractionRead:
        project = ensure_ail_system_project(self.db, user)
        context = ProfessorContextAssembler(self.db).assemble(user.id, request)
        context, truncated = self._bound_context(context)
        agent_version = self._ensure_professor_agent(project)
        budget_id = self._authorized_budget(user, project, request.budget_id)

        question = request.question or self._default_question(request.intent)
        requirements = {
            PROFESSOR_CONTEXT_KEY: context.model_dump(mode="json"),
            "context_truncated": truncated,
            "professor_user_id": user.id,
            "_professor_max_output_tokens": settings.professor_max_output_tokens,
        }
        task = Task(
            project_id=project.id,
            title=f"AI Professor: {request.intent.value}",
            description=question,
            requirements=requirements,
            execution_mode=ExecutionMode.SINGLE_AGENT,
            created_by=user.id,
        )
        self.db.add(task)
        self.db.flush()
        self.db.commit()

        task_run = TaskService(self.db).start_task_run(
            task_id=task.id,
            agent_version_id=agent_version.id,
            frozen_task_snapshot={
                "title": task.title,
                "description": question,
                "requirements": requirements,
            },
            agent_run_role=AgentRunRole.PROFESSOR,
            budget_id=budget_id,
            model_policy_override=self._professor_model_policy(agent_version),
            enqueue=False,
        )
        agent_run = self.db.execute(
            select(AgentRun).where(AgentRun.task_run_id == task_run.id)
        ).scalar_one()

        # This is the ordinary execution service, invoked synchronously so a
        # local learner receives a usable answer without requiring a second
        # queue process. The durable lineage remains unchanged.
        try:
            self._executor().execute(agent_run.id, worker_id="professor-api")
        except Exception as exc:  # noqa: BLE001 - execution service persists categorized failure state
            self.db.rollback()
            return self._read_interaction(task_run.id, user, fallback_error=("execution_error", str(exc)))
        return self._read_interaction(task_run.id, user)

    def read_interaction(self, user: User, interaction_id: str) -> ProfessorInteractionRead:
        return self._read_interaction(interaction_id, user)

    def continue_interaction(
        self, user: User, interaction_id: str, request: ProfessorContextRequest
    ) -> ProfessorInteractionRead:
        prior = self._get_owned_task_run(user, interaction_id)
        task = self.db.get(Task, prior.task_id)
        requirements = task.requirements if task is not None else {}
        if requirements.get("professor_user_id") != user.id:
            raise ForbiddenError("Professor interaction belongs to another learner.")
        prior_context = requirements
        if not prior_context.get(PROFESSOR_CONTEXT_KEY):
            raise NotFoundError("Professor interaction context is unavailable.")
        # The continuation is explicit and bounded: only this immediate
        # interaction's answer is made available, never an old transcript.
        artifact = self._artifact_for(prior)
        prior_text = self._read_artifact(artifact) if artifact is not None else ""
        follow_up = request.model_copy(update={"previous_interaction_id": interaction_id})
        context = ProfessorContext.model_validate(prior_context[PROFESSOR_CONTEXT_KEY])
        context.deterministic_facts = {
            **context.deterministic_facts,
            "previous_professor_response": prior_text[:12000],
        }
        follow_up.question = request.question or "Please explain the previous answer more simply."
        # Reuse the normal path while keeping the previous answer in the
        # explicit request context; no conversation table is introduced.
        return self._create_from_context(user, follow_up, context)

    def _create_from_context(
        self, user: User, request: ProfessorContextRequest, context: ProfessorContext
    ) -> ProfessorInteractionRead:
        project = ensure_ail_system_project(self.db, user)
        context, truncated = self._bound_context(context)
        agent_version = self._ensure_professor_agent(project)
        budget_id = self._authorized_budget(user, project, request.budget_id)
        requirements = {
            PROFESSOR_CONTEXT_KEY: context.model_dump(mode="json"),
            "context_truncated": truncated,
            "professor_user_id": user.id,
            "_professor_max_output_tokens": settings.professor_max_output_tokens,
        }
        task = Task(
            project_id=project.id,
            title=f"AI Professor: {request.intent.value}",
            description=request.question or "Please explain the previous answer more simply.",
            requirements=requirements,
            execution_mode=ExecutionMode.SINGLE_AGENT,
            created_by=user.id,
        )
        self.db.add(task)
        self.db.flush()
        self.db.commit()
        task_run = TaskService(self.db).start_task_run(
            task_id=task.id,
            agent_version_id=agent_version.id,
            frozen_task_snapshot={"title": task.title, "description": task.description, "requirements": requirements},
            agent_run_role=AgentRunRole.PROFESSOR,
            budget_id=budget_id,
            model_policy_override=self._professor_model_policy(agent_version),
            enqueue=False,
        )
        agent_run = self.db.execute(select(AgentRun).where(AgentRun.task_run_id == task_run.id)).scalar_one()
        try:
            self._executor().execute(agent_run.id, worker_id="professor-api")
        except Exception:  # noqa: BLE001 - execution service persists categorized failure state
            self.db.rollback()
        return self._read_interaction(task_run.id, user)

    def _bound_context(self, context: ProfessorContext) -> Tuple[ProfessorContext, bool]:
        encoded = context.model_dump(mode="json")
        if len(json.dumps(encoded, sort_keys=True, default=str)) <= settings.professor_max_context_chars:
            context.deterministic_facts = {
                **context.deterministic_facts,
                "context_truncated": False,
            }
            return context, False

        records = list(context.records)
        truncated = False
        while records and len(json.dumps({**encoded, "records": [r.model_dump(mode="json") for r in records]}, default=str)) > settings.professor_max_context_chars:
            removable = next((idx for idx, item in enumerate(records) if not item.conflict_group), None)
            if removable is None:
                break
            records.pop(removable)
            truncated = True
        context.records = records
        context.deterministic_facts = {
            **context.deterministic_facts,
            "context_truncated": truncated,
            "context_record_count": len(records),
        }
        return context, truncated

    def _ensure_professor_agent(self, project: Project) -> AgentVersion:
        agent = self.db.get(Agent, PROFESSOR_AGENT_ID)
        if agent is None:
            agent = Agent(
                id=PROFESSOR_AGENT_ID,
                project_id=project.id,
                name="AI Professor",
                role="professor",
                description="Evidence-grounded AIL learning explanation and coaching.",
                current_status=VersionStatus.ACTIVE,
            )
            self.db.add(agent)
            self.db.flush()
            prompt = PromptVersion(agent_id=agent.id, version=1, content=self._professor_prompt())
            self.db.add(prompt)
            self.db.flush()
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                name="AI Professor",
                role="professor",
                description=agent.description,
                prompt_version_id=prompt.id,
                model_policy={
                    "mode": "auto",
                    "auto_policy": "prefer_free",
                    "required_capabilities": {"structured_output_support": True},
                    # A high reasoning tier is a poor fit for this bounded
                    # structured response. Unknown remains eligible so the
                    # existing registry can be used before capability data is
                    # enriched; response_format is only sent when support is
                    # explicitly confirmed.
                    "excluded_capabilities": {"reasoning_tier": ["high"]},
                },
                context_policy={"source": "ail_professor_context", "max_context_chars": settings.professor_max_context_chars},
                budget_policy={"max_output_tokens": settings.professor_max_output_tokens},
                status=VersionStatus.ACTIVE,
                published_at=prompt.created_at,
            )
            self.db.add(version)
            self.db.commit()
            return version
        if agent.project_id != project.id or agent.role != "professor":
            raise ConflictError("The reserved AI Professor agent is not attached to the AIL system project.")
        version = self.db.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent.id,
                AgentVersion.status == VersionStatus.ACTIVE,
            ).order_by(AgentVersion.version.desc())
        ).scalars().first()
        if version is None:
            raise ConflictError("AI Professor has no active Agent Version.")
        return version

    @staticmethod
    def _professor_prompt() -> str:
        return """You are AI Professor. Use only the supplied AIL context. Return only the bounded JSON Professor response contract. Never invent evidence or IDs. Preserve conflict groups and the learner's conclusion. AI explanations are not canonical evidence. All actions are advisory. Never claim mastery or change learning, review, experiment, Radar, or plan state. Do not reveal hidden reasoning."""

    @staticmethod
    def _professor_model_policy(agent_version: AgentVersion) -> dict:
        """Carry the registered policy forward with Professor constraints.

        This is a run-level snapshot, so existing published Professor
        versions receive the correction without mutating an immutable Agent
        Version or introducing a migration.
        """
        if settings.environment == "staging" and settings.professor_staging_provider_model_id:
            return {
                "mode": "manual",
                "manual_provider_model_id": settings.professor_staging_provider_model_id,
            }
        policy = dict(agent_version.model_policy or {})
        required = dict(policy.get("required_capabilities") or {})
        required.setdefault("structured_output_support", True)
        excluded = dict(policy.get("excluded_capabilities") or {})
        excluded.setdefault("reasoning_tier", ["high"])
        policy["required_capabilities"] = required
        policy["excluded_capabilities"] = excluded
        return policy

    @staticmethod
    def _default_question(intent: ProfessorIntent) -> str:
        return {
            ProfessorIntent.WHAT_SHOULD_I_LEARN_NEXT: "What should I learn next based on my current AIL state?",
            ProfessorIntent.WHY_DOES_THIS_MATTER: "Why does this matter to my learning?",
            ProfessorIntent.HELP_ME_REVIEW: "Help me understand this review without changing its result.",
            ProfessorIntent.UNDERSTAND_MY_EXPERIMENT: "Help me understand my experiment and preserve my conclusion.",
            ProfessorIntent.EXPLAIN_THIS: "Explain this selected AIL item.",
        }.get(intent, "Answer my learning question using my authorized AIL context.")

    def _get_owned_task_run(self, user: User, interaction_id: str) -> TaskRun:
        task_run = self.db.get(TaskRun, interaction_id)
        if task_run is None:
            raise NotFoundError("Professor interaction not found.")
        task = self.db.get(Task, task_run.task_id)
        if task is None:
            raise NotFoundError("Professor interaction not found.")
        require_ail_system_access(self.db, user, task.project_id)
        if (task.requirements or {}).get("professor_user_id") != user.id:
            raise ForbiddenError("Professor interaction belongs to another learner.")
        return task_run

    def _read_interaction(
        self, interaction_id: str, user: User, fallback_error: Optional[Tuple[str, str]] = None
    ) -> ProfessorInteractionRead:
        task_run = self._get_owned_task_run(user, interaction_id)
        task = self.db.get(Task, task_run.task_id)
        agent_run = self.db.execute(select(AgentRun).where(AgentRun.task_run_id == task_run.id)).scalars().first()
        artifact = self._artifact_for(task_run)
        response = None
        error_kind, error_message = fallback_error or (None, None)
        if task_run.status == TaskRunStatus.COMPLETED and artifact is not None:
            try:
                response = self._validate_artifact(artifact, task.requirements or {})
            except (ValueError, ProfessorResponseValidationError) as exc:
                error_kind, error_message = "validation_error", str(exc)
                self._mark_invalid(agent_run, task_run, str(exc))
        elif task_run.status == TaskRunStatus.FAILED:
            attempt = agent_run.attempts[-1] if agent_run is not None and agent_run.attempts else None
            error_kind = error_kind or ((attempt.error or {}).get("category") if attempt else "execution_error")
            error_kind, error_message = self._safe_provider_error(
                error_kind,
                error_message or ((attempt.error or {}).get("message") if attempt else None),
                attempt.error if attempt else None,
            )
        return ProfessorInteractionRead(
            interaction_id=task_run.id,
            task_run_id=task_run.id,
            agent_run_id=agent_run.id if agent_run else None,
            artifact_id=artifact.id if artifact else None,
            status="complete" if response else (error_kind or task_run.status.value),
            intent=ProfessorIntent((task.requirements or {}).get(PROFESSOR_CONTEXT_KEY, {}).get("intent", ProfessorIntent.ASK_PROFESSOR.value)),
            direct_answer=response.direct_answer if response else None,
            explanation=response.explanation if response else None,
            evidence=response.evidence if response else [],
            grounded_assertions=response.grounded_assertions if response else [],
            uncertainties=response.uncertainties if response else [],
            suggested_next_actions=response.suggested_next_actions if response else [],
            attachment_references=response.attachment_references if response else [],
            error_kind=error_kind,
            error_message=error_message,
        )

    @staticmethod
    def _safe_provider_error(kind: str, message: Optional[str], error: Optional[dict]):
        """Keep provider details internal while returning learner-safe text."""
        status_code = error.get("status_code") if isinstance(error, dict) else None
        if kind == "provider_connection_error" and status_code == 429:
            return "provider_rate_limited", "The AI provider is temporarily busy. Please try again shortly."
        if kind in {"provider_connection_error", "provider_timeout", "provider_authentication_error"}:
            return "provider_unavailable", "The AI provider is temporarily unavailable. Please try again."
        if kind == "provider_invalid_response":
            return kind, "The AI provider returned an invalid response. Please try again."
        return kind, "Professor execution was unsuccessful. Please try again."

    def _validate_artifact(self, artifact: Artifact, requirements: dict) -> ProfessorResponse:
        text = self._read_artifact(artifact).strip()
        fenced = _JSON_FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()
        payload = json.loads(text)
        response = ProfessorResponse.model_validate(payload)
        context = ProfessorContext.model_validate(requirements[PROFESSOR_CONTEXT_KEY])
        return validate_professor_response(response, context)

    @staticmethod
    def _read_artifact(artifact: Artifact) -> str:
        return Path(artifact.storage_ref).read_text(encoding="utf-8")

    def _artifact_for(self, task_run: TaskRun) -> Optional[Artifact]:
        agent_run = self.db.execute(select(AgentRun).where(AgentRun.task_run_id == task_run.id)).scalars().first()
        if agent_run is None:
            return None
        return self.db.execute(
            select(Artifact).where(Artifact.agent_run_id == agent_run.id).order_by(Artifact.created_at.desc())
        ).scalars().first()

    def _mark_invalid(self, agent_run: Optional[AgentRun], task_run: TaskRun, message: str) -> None:
        if agent_run is not None and agent_run.status == AgentRunStatus.COMPLETED:
            agent_run.status = AgentRunStatus.FAILED
        if task_run.status == TaskRunStatus.COMPLETED:
            task_run.status = TaskRunStatus.FAILED
        self.db.commit()
        FlightRecorderService(self.db).record(
            task_id=task_run.task_id,
            task_run_id=task_run.id,
            agent_run_id=agent_run.id if agent_run else None,
            event_type="professor.response_rejected",
            decision_summary="Structured Professor output failed deterministic validation.",
            error={"category": "validation_error", "message": message},
        )

    def _authorized_budget(self, user: User, project: Project, budget_id: Optional[str]) -> Optional[str]:
        if budget_id is None:
            return None
        budget = self.db.get(Budget, budget_id)
        if budget is None or budget.project_id != project.id:
            raise ForbiddenError("Professor budget is not authorized for this AIL project.")
        require_ail_system_access(self.db, user, project.id)
        return budget.id

    def _executor(self):
        from app.services.execution_service import AgentExecutionService

        return (
            AgentExecutionService(self.db, adapter_factory=self.adapter_factory)
            if self.adapter_factory is not None
            else AgentExecutionService(self.db)
        )
