"""AIL.3A user-owned Test Kits and draft Experiment service.

This service configures durable, reproducible work only. It never queues a
job, calls a provider, launches MA5, writes MA6 results, or mutates learning
state.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.enums import (
    CostEstimateKind,
    EvalSetVersionStatus,
    ExecutionMode,
    ExperimentStatus,
    ExperimentType,
    ProjectKind,
    TaskStatus,
    VersionStatus,
)
from app.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.agents import Agent, AgentVersion
from app.models.concepts import Concept, LearningItem
from app.models.identity import Project, ProjectMembership, User
from app.models.lab import (
    EvalSet,
    EvalSetVersion,
    EvalSetVersionTask,
    Experiment,
    ExperimentAgentVersion,
    ExperimentModel,
)
from app.models.providers import Model, ProviderModelSnapshot
from app.models.radar import Development
from app.models.tasks import Task
from app.models.taxonomy import TaxonomyTerm

_STARTER_KITS = (
    (
        "Software Engineer",
        "Six controlled tasks covering implementation, debugging, testing, and design tradeoffs.",
        (
            ("Bug diagnosis", "Diagnose a reproducible defect and identify its root cause."),
            ("Small implementation change", "Implement a bounded behavior change without unrelated edits."),
            ("API contract preservation", "Change an API while preserving its documented contract."),
            ("Test addition/refactor", "Add focused regression coverage and keep existing behavior stable."),
            ("Error handling and edge cases", "Handle invalid input and boundary conditions explicitly."),
            ("Performance/design tradeoff", "Explain and implement a measured design tradeoff."),
        ),
    ),
    (
        "Code Reviewer",
        "Six controlled review tasks covering correctness, security, compatibility, and maintainability.",
        (
            ("Correctness defect detection", "Identify a behaviorally incorrect change and explain the failure."),
            ("Security issue detection", "Identify an unsafe boundary or data exposure risk."),
            ("Maintainability/readability", "Review structure, clarity, and long-term maintenance risk."),
            ("Test coverage and regression risk", "Assess whether the change is adequately verified."),
            ("API compatibility", "Check callers, schemas, and compatibility implications."),
            ("Performance/resource concern", "Identify material performance or resource risks."),
        ),
    ),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LabService:
    def __init__(self, db: Session):
        self.db = db

    def _owned_kit(self, user_id: str, kit_id: str) -> EvalSet:
        kit = self.db.execute(
            select(EvalSet).where(EvalSet.id == kit_id, EvalSet.user_id == user_id)
        ).scalar_one_or_none()
        if kit is None:
            raise NotFoundError("Test Kit not found.")
        return kit

    def _owned_version(self, user_id: str, version_id: str) -> EvalSetVersion:
        version = self.db.execute(
            select(EvalSetVersion)
            .join(EvalSet, EvalSet.id == EvalSetVersion.eval_set_id)
            .where(EvalSetVersion.id == version_id, EvalSet.user_id == user_id)
        ).scalar_one_or_none()
        if version is None:
            raise NotFoundError("Test Kit Version not found.")
        return version

    def _ail_project(self, user: User) -> Project:
        project = self.db.execute(
            select(Project).where(
                Project.org_id == user.org_id,
                Project.kind == ProjectKind.SYSTEM_AIL,
                Project.name == "AIL Personal Lab",
            )
        ).scalar_one_or_none()
        if project is None:
            project = Project(
                org_id=user.org_id,
                name="AIL Personal Lab",
                kind=ProjectKind.SYSTEM_AIL,
                ail_evidence_opt_in=False,
            )
            self.db.add(project)
            self.db.flush()
        return project

    @staticmethod
    def _task_snapshot(task: Task) -> dict:
        return {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "requirements": task.requirements,
            "execution_mode": task.execution_mode.value,
            "status": task.status.value,
        }

    def starter_kits(self, user: User) -> Tuple[List[EvalSet], List[EvalSetVersion]]:
        kits: List[EvalSet] = []
        versions: List[EvalSetVersion] = []
        project = self._ail_project(user)
        for name, description, cases in _STARTER_KITS:
            kit = self.db.execute(
                select(EvalSet).where(EvalSet.user_id == user.id, EvalSet.name == name)
            ).scalar_one_or_none()
            if kit is None:
                role_term = self.db.execute(
                    select(TaxonomyTerm).where(TaxonomyTerm.vocabulary == "role", TaxonomyTerm.key == name.lower().replace(" ", "_"))
                ).scalar_one_or_none()
                kit = EvalSet(user_id=user.id, name=name, description=description, role_term_id=role_term.id if role_term else None)
                self.db.add(kit)
                self.db.flush()
                version = EvalSetVersion(
                    eval_set_id=kit.id,
                    version=1,
                    status=EvalSetVersionStatus.PUBLISHED,
                    published_at=_utcnow(),
                )
                self.db.add(version)
                self.db.flush()
                for position, (title, task_description) in enumerate(cases):
                    task = Task(
                        project_id=project.id,
                        title=f"{name}: {title}",
                        description=task_description,
                        requirements={"starter_case": title, "controlled_template": True},
                        execution_mode=ExecutionMode.SINGLE_AGENT,
                        created_by=user.id,
                        status=TaskStatus.READY,
                    )
                    self.db.add(task)
                    self.db.flush()
                    self.db.add(
                        EvalSetVersionTask(
                            eval_set_version_id=version.id,
                            task_id=task.id,
                            position=position,
                            task_snapshot=self._task_snapshot(task),
                        )
                    )
            else:
                version = self.db.execute(
                    select(EvalSetVersion)
                    .where(EvalSetVersion.eval_set_id == kit.id)
                    .order_by(EvalSetVersion.version.desc())
                ).scalars().first()
                if version is None:
                    raise ConflictError(f"Test Kit {name!r} has no version.")
            kits.append(kit)
            versions.append(version)
        self.db.commit()
        return kits, versions

    def list_kits(self, user_id: str) -> List[EvalSet]:
        return list(self.db.execute(select(EvalSet).where(EvalSet.user_id == user_id).order_by(EvalSet.name)).scalars())

    def _accessible_tasks(self, user_id: str, task_ids: List[str]) -> List[Task]:
        if len(set(task_ids)) != len(task_ids):
            raise ConflictError("A Test Kit cannot contain the same Task twice.")
        rows = list(self.db.execute(
            select(Task)
            .join(ProjectMembership, ProjectMembership.project_id == Task.project_id)
            .where(ProjectMembership.user_id == user_id, Task.id.in_(task_ids))
        ).scalars())
        by_id = {task.id: task for task in rows}
        if len(by_id) != len(task_ids):
            raise ForbiddenError("One or more Tasks are not accessible to this user.")
        return [by_id[task_id] for task_id in task_ids]

    def create_test_kit(self, user: User, *, name: str, description: Optional[str], task_ids: List[str]) -> EvalSet:
        if self.db.execute(select(EvalSet).where(EvalSet.user_id == user.id, EvalSet.name == name)).scalar_one_or_none():
            raise ConflictError("A Test Kit with this name already exists.")
        tasks = self._accessible_tasks(user.id, task_ids)
        kit = EvalSet(user_id=user.id, name=name, description=description)
        self.db.add(kit)
        self.db.flush()
        version = EvalSetVersion(eval_set_id=kit.id, version=1, status=EvalSetVersionStatus.DRAFT)
        self.db.add(version)
        self.db.flush()
        self._add_version_tasks(version, tasks)
        self.db.commit()
        self.db.refresh(kit)
        return kit

    def _add_version_tasks(self, version: EvalSetVersion, tasks: List[Task]) -> None:
        for position, task in enumerate(tasks):
            self.db.add(
                EvalSetVersionTask(
                    eval_set_version_id=version.id,
                    task_id=task.id,
                    position=position,
                    task_snapshot=self._task_snapshot(task),
                )
            )
        self.db.flush()

    def create_version(self, user: User, kit_id: str, task_ids: List[str]) -> EvalSetVersion:
        kit = self._owned_kit(user.id, kit_id)
        tasks = self._accessible_tasks(user.id, task_ids)
        latest = self.db.execute(
            select(EvalSetVersion).where(EvalSetVersion.eval_set_id == kit.id).order_by(EvalSetVersion.version.desc())
        ).scalars().first()
        version = EvalSetVersion(
            eval_set_id=kit.id,
            version=(latest.version + 1) if latest else 1,
            status=EvalSetVersionStatus.DRAFT,
        )
        self.db.add(version)
        self.db.flush()
        self._add_version_tasks(version, tasks)
        self.db.commit()
        self.db.refresh(version)
        return version

    def publish_version(self, user: User, kit_id: str, version_id: str) -> EvalSetVersion:
        self._owned_kit(user.id, kit_id)
        version = self._owned_version(user.id, version_id)
        if version.eval_set_id != kit_id:
            raise NotFoundError("Test Kit Version not found.")
        if version.status != EvalSetVersionStatus.DRAFT:
            raise ConflictError("Only draft Test Kit Versions can be published.")
        self._validate_version_tasks(version)
        version.status = EvalSetVersionStatus.PUBLISHED
        version.published_at = _utcnow()
        self.db.commit()
        self.db.refresh(version)
        return version

    def _validate_version_tasks(self, version: EvalSetVersion) -> None:
        count = self.db.execute(
            select(func.count(EvalSetVersionTask.id)).where(EvalSetVersionTask.eval_set_version_id == version.id)
        ).scalar_one()
        if count < 1:
            raise ConflictError("A Test Kit Version must contain at least one task.")

    def _estimate(self, model_snapshots: Iterable[ProviderModelSnapshot], config: dict, repetitions: int):
        snapshots = list(model_snapshots)
        token_in = config.get("estimated_tokens_in")
        token_out = config.get("estimated_tokens_out")
        if not snapshots or not isinstance(token_in, int) or not isinstance(token_out, int) or token_in < 0 or token_out < 0:
            return None, CostEstimateKind.UNKNOWN
        if any(s.pricing_input_per_mtok is None or s.pricing_output_per_mtok is None for s in snapshots):
            return None, CostEstimateKind.UNKNOWN
        total = Decimal(0)
        for snapshot in snapshots:
            total += (Decimal(token_in) / Decimal(1_000_000)) * snapshot.pricing_input_per_mtok
            total += (Decimal(token_out) / Decimal(1_000_000)) * snapshot.pricing_output_per_mtok
        return total * repetitions, CostEstimateKind.ESTIMATED

    def create_experiment(self, user: User, data, *, development_id: Optional[str] = None) -> Experiment:
        version = self._owned_version(user.id, data.eval_set_version_id)
        self._validate_version_tasks(version)
        if version.status == EvalSetVersionStatus.DRAFT:
            raise ConflictError("Only published Test Kit Versions may be used.")
        if data.experiment_type == ExperimentType.MODEL_COMPARISON and len(data.models) < 2:
            raise ConflictError("Model Comparison requires at least two selected Models.")
        if data.experiment_type == ExperimentType.PROMPT_COMPARISON and len(data.agent_version_ids) < 2:
            raise ConflictError("Prompt Comparison requires at least two Agent Versions.")
        if data.experiment_type == ExperimentType.VARIANCE and data.repetitions < 2:
            raise ConflictError("Variance experiments require at least two repetitions.")

        if not data.agent_version_ids:
            raise ConflictError("At least one Agent Version is required.")
        agent_versions = []
        for agent_version_id in data.agent_version_ids:
            agent_version = self.db.execute(
                select(AgentVersion)
                .join(Agent, Agent.id == AgentVersion.agent_id)
                .join(ProjectMembership, ProjectMembership.project_id == Agent.project_id)
                .where(
                    AgentVersion.id == agent_version_id,
                    ProjectMembership.user_id == user.id,
                )
            ).scalar_one_or_none()
            if agent_version is None or agent_version.status != VersionStatus.ACTIVE:
                raise ConflictError("Every selected Agent Version must be accessible and active.")
            agent_versions.append(agent_version)

        snapshots = []
        for selection in data.models:
            model = self.db.get(Model, selection.model_id)
            snapshot = self.db.get(ProviderModelSnapshot, selection.provider_model_snapshot_id)
            if model is None or snapshot is None:
                raise NotFoundError("Selected Model or Provider Model Snapshot not found.")
            if snapshot.model_id != model.id:
                raise ConflictError("Provider Model Snapshot does not belong to selected Model.")
            snapshots.append(snapshot)

        development = self.db.get(Development, development_id or data.development_id) if (development_id or data.development_id) else None
        if (development_id or data.development_id) and development is None:
            raise NotFoundError("Radar Development not found.")
        concept = self.db.get(Concept, data.concept_id) if data.concept_id else None
        if data.concept_id and concept is None:
            raise NotFoundError("Concept not found.")

        # Freeze current ConceptVersion at experiment creation for learning provenance
        concept_version = None
        if concept:
            from app.services.concept_graph_service import ConceptGraphService
            concept_version = ConceptGraphService(self.db).get_current_version(concept.id)
            if not concept_version:
                raise ConflictError(f"Concept {concept.id} has no current published version")

        if data.learning_item_id and self.db.get(LearningItem, data.learning_item_id) is None:
            raise NotFoundError("Learning Item not found.")

        if data.budget_id:
            # Budget approval/authorization remains an existing-project concern;
            # this foundation only accepts a real budget reference owned by an
            # accessible project, without reserving or spending.
            from app.models.governance import Budget
            budget = self.db.get(Budget, data.budget_id)
            if budget is None:
                raise NotFoundError("Budget not found.")
            if self.db.execute(
                select(ProjectMembership.id).where(
                    ProjectMembership.project_id == budget.project_id,
                    ProjectMembership.user_id == user.id,
                )
            ).scalar_one_or_none() is None:
                raise ForbiddenError("Budget is not accessible to this user.")

        estimated_cost, estimate_kind = self._estimate(snapshots, data.config, data.repetitions)
        version.status = EvalSetVersionStatus.FROZEN
        version.frozen_at = version.frozen_at or _utcnow()
        experiment = Experiment(
            user_id=user.id,
            experiment_type=data.experiment_type,
            status=ExperimentStatus.DRAFT,
            hypothesis=data.hypothesis,
            eval_set_version_id=version.id,
            development_id=development.id if development else None,
            concept_id=concept.id if concept else None,
            concept_version_id=concept_version.id if concept_version else None,
            learning_item_id=data.learning_item_id,
            repetitions=data.repetitions,
            config_snapshot={**data.config, "repetitions": data.repetitions, "execution_enabled": False},
            estimated_cost=estimated_cost,
            estimated_currency="USD",
            cost_estimate_kind=estimate_kind,
            budget_id=data.budget_id,
        )
        self.db.add(experiment)
        self.db.flush()
        for position, agent_version in enumerate(agent_versions):
            self.db.add(ExperimentAgentVersion(experiment_id=experiment.id, agent_version_id=agent_version.id, position=position))
        for position, selection in enumerate(data.models):
            self.db.add(
                ExperimentModel(
                    experiment_id=experiment.id,
                    model_id=selection.model_id,
                    provider_model_snapshot_id=selection.provider_model_snapshot_id,
                    position=position,
                )
            )
        self.db.commit()
        self.db.refresh(experiment)
        return experiment

    def get_experiment(self, user_id: str, experiment_id: str) -> Experiment:
        experiment = self.db.execute(
            select(Experiment).where(Experiment.id == experiment_id, Experiment.user_id == user_id)
        ).scalar_one_or_none()
        if experiment is None:
            raise NotFoundError("Experiment not found.")
        return experiment

    def experiment_read(self, experiment: Experiment) -> dict:
        agent_ids = list(self.db.execute(
            select(ExperimentAgentVersion.agent_version_id)
            .where(ExperimentAgentVersion.experiment_id == experiment.id)
            .order_by(ExperimentAgentVersion.position)
        ).scalars())
        models = list(self.db.execute(
            select(ExperimentModel.model_id, ExperimentModel.provider_model_snapshot_id)
            .where(ExperimentModel.experiment_id == experiment.id)
            .order_by(ExperimentModel.position)
        ).all())
        return {
            "id": experiment.id,
            "user_id": experiment.user_id,
            "experiment_type": experiment.experiment_type,
            "status": experiment.status,
            "hypothesis": experiment.hypothesis,
            "eval_set_version_id": experiment.eval_set_version_id,
            "development_id": experiment.development_id,
            "concept_id": experiment.concept_id,
            "learning_item_id": experiment.learning_item_id,
            "repetitions": experiment.repetitions,
            "config_snapshot": experiment.config_snapshot,
            "estimated_cost": experiment.estimated_cost,
            "estimated_currency": experiment.estimated_currency,
            "cost_estimate_kind": experiment.cost_estimate_kind,
            "budget_id": experiment.budget_id,
            "agent_version_ids": agent_ids,
            "models": [{"model_id": model_id, "provider_model_snapshot_id": snapshot_id} for model_id, snapshot_id in models],
            "frozen_at": experiment.frozen_at,
            "created_at": experiment.created_at,
        }
