"""Evaluation Definition Registry Service — MA6 Slice 1.

Structurally mirrors app.services.agent_registry_service's Agent/AgentVersion
lifecycle (create -> DRAFT version -> publish/deprecate/retire, immutable
once published) applied to evaluation rubrics. Deliberately NOT sharing a
lifecycle helper with AgentRegistryService: the transition logic here is a
handful of set-membership checks, and forcing a shared abstraction across
two independently-evolving registries for that little duplication would
cost more (an extra indirection both callers must reason through) than it
saves -- consistent with the bounded-scope instruction for this slice.

No evaluation *execution* concept exists here (no EvaluationRun, no
criterion results, no scoring) -- this service only ever answers "what
rubric versions exist and what criteria do they define," never "how did a
candidate score." Nothing in this module writes to comparison_candidates,
comparison_runs, or any winner/ranking column -- MA6's non-negotiable
invariant (Section 2) has no surface here to violate.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import VersionStatus
from app.errors import ConflictError, InvalidStateTransitionError, NotFoundError
from app.models.evaluation_definitions import EvaluationCriterion, EvaluationDefinition, EvaluationDefinitionVersion

_PUBLISH_ALLOWED_FROM = {VersionStatus.DRAFT}
_DEPRECATE_ALLOWED_FROM = {VersionStatus.ACTIVE}
_RETIRE_ALLOWED_FROM = {VersionStatus.ACTIVE, VersionStatus.DEPRECATED}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvaluationDefinitionService:
    def __init__(self, db: Session):
        self.db = db

    # -- Evaluation Definitions -----------------------------------------

    def create_definition(
        self, *, project_id: str, name: str, description: Optional[str] = None
    ) -> EvaluationDefinition:
        definition = EvaluationDefinition(project_id=project_id, name=name, description=description)
        self.db.add(definition)
        self.db.commit()
        self.db.refresh(definition)
        return definition

    def get_definition(self, evaluation_definition_id: str) -> Optional[EvaluationDefinition]:
        return self.db.get(EvaluationDefinition, evaluation_definition_id)

    def list_definitions(self, *, project_id: str) -> List[EvaluationDefinition]:
        stmt = select(EvaluationDefinition).where(EvaluationDefinition.project_id == project_id)
        return list(self.db.execute(stmt).scalars().all())

    # -- Evaluation Definition Versions -----------------------------------

    def create_version(
        self,
        *,
        evaluation_definition_id: str,
        description: Optional[str],
        criteria: List[dict],
    ) -> EvaluationDefinitionVersion:
        """``criteria``: list of ``{"key", "label", "description"?,
        "method_hint"?}``, in the order they should be evaluated. Always
        inserts a new version row -- this is what "editing" a published
        rubric means (same discipline as AgentRegistryService.
        create_agent_version), never an UPDATE of an existing one."""
        if not criteria:
            raise ConflictError("An evaluation definition version requires at least one criterion.")
        keys = [c["key"] for c in criteria]
        if len(set(keys)) != len(keys):
            raise ConflictError("Criterion keys must be unique within one evaluation definition version.")

        next_version = self._next_version_number(evaluation_definition_id)
        version = EvaluationDefinitionVersion(
            evaluation_definition_id=evaluation_definition_id,
            version=next_version,
            description=description,
            status=VersionStatus.DRAFT,
        )
        self.db.add(version)
        self.db.flush()

        for index, spec in enumerate(criteria):
            self.db.add(
                EvaluationCriterion(
                    evaluation_definition_version_id=version.id,
                    key=spec["key"],
                    label=spec["label"],
                    description=spec.get("description"),
                    method_hint=spec.get("method_hint"),
                    order_index=index,
                )
            )
        self.db.commit()
        self.db.refresh(version)
        return version

    def get_version(self, evaluation_definition_version_id: str) -> Optional[EvaluationDefinitionVersion]:
        return self.db.get(EvaluationDefinitionVersion, evaluation_definition_version_id)

    def list_versions(self, *, evaluation_definition_id: str) -> List[EvaluationDefinitionVersion]:
        stmt = (
            select(EvaluationDefinitionVersion)
            .where(EvaluationDefinitionVersion.evaluation_definition_id == evaluation_definition_id)
            .order_by(EvaluationDefinitionVersion.version)
        )
        return list(self.db.execute(stmt).scalars().all())

    def _next_version_number(self, evaluation_definition_id: str) -> int:
        existing = self.list_versions(evaluation_definition_id=evaluation_definition_id)
        return (existing[-1].version + 1) if existing else 1

    # -- Lifecycle transitions (same DRAFT->ACTIVE->DEPRECATED->RETIRED ---
    # -- contract as AgentVersion, Section 26.7) ---------------------------

    def publish_version(self, evaluation_definition_version_id: str) -> EvaluationDefinitionVersion:
        version = self._require_version(evaluation_definition_version_id)
        self._require_transition(version, _PUBLISH_ALLOWED_FROM, "publish")
        version.status = VersionStatus.ACTIVE
        version.published_at = _utcnow()
        self._sync_definition_current_status(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def deprecate_version(self, evaluation_definition_version_id: str) -> EvaluationDefinitionVersion:
        version = self._require_version(evaluation_definition_version_id)
        self._require_transition(version, _DEPRECATE_ALLOWED_FROM, "deprecate")
        version.status = VersionStatus.DEPRECATED
        self._sync_definition_current_status(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def retire_version(self, evaluation_definition_version_id: str) -> EvaluationDefinitionVersion:
        version = self._require_version(evaluation_definition_version_id)
        self._require_transition(version, _RETIRE_ALLOWED_FROM, "retire")
        version.status = VersionStatus.RETIRED
        self._sync_definition_current_status(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def _require_version(self, evaluation_definition_version_id: str) -> EvaluationDefinitionVersion:
        version = self.get_version(evaluation_definition_version_id)
        if version is None:
            raise NotFoundError(f"Evaluation definition version {evaluation_definition_version_id} not found.")
        return version

    def _require_transition(self, version: EvaluationDefinitionVersion, allowed_from: set, action: str) -> None:
        if version.status not in allowed_from:
            raise InvalidStateTransitionError(
                f"Cannot {action} evaluation definition version {version.id} from status "
                f"{version.status.value!r} (allowed from: {sorted(s.value for s in allowed_from)})."
            )

    def _sync_definition_current_status(self, version: EvaluationDefinitionVersion) -> None:
        definition = self.db.get(EvaluationDefinition, version.evaluation_definition_id)
        if definition is not None:
            definition.current_status = version.status
