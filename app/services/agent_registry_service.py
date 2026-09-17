"""Agent Registry Service — Section 11, 12, MA2.

Agent != Model remains structural: nothing here stores a permanent
Agent->Model binding (ADR-1). ``AgentVersion.model_policy`` expresses
*preference/constraint* only (manual pick or an auto policy name) — the
actual (Model, Provider) resolution happens per Agent Run, later, by the
Router (MA3+).

Published AgentVersion content is immutable (Section 12.3): publish only
flips ``status``/``published_at``; nothing here ever mutates a published
version's name/role/description/prompt_version_id/model_policy/etc.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import DefaultModelStrategy, VersionStatus
from app.errors import InvalidStateTransitionError, NotFoundError
from app.models.agents import Agent, AgentVersion, PromptVersion

# Section 26.7's corrected lifecycle table.
_PUBLISH_ALLOWED_FROM = {VersionStatus.DRAFT}
_DEPRECATE_ALLOWED_FROM = {VersionStatus.ACTIVE}
_RETIRE_ALLOWED_FROM = {VersionStatus.ACTIVE, VersionStatus.DEPRECATED}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentRegistryService:
    def __init__(self, db: Session):
        self.db = db

    # -- Agents -----------------------------------------------------------

    def create_agent(
        self, *, project_id: str, name: str, role: str, description: Optional[str] = None
    ) -> Agent:
        agent = Agent(project_id=project_id, name=name, role=role, description=description)
        self.db.add(agent)
        self.db.commit()
        self.db.refresh(agent)
        return agent

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        return self.db.get(Agent, agent_id)

    def list_agents(self, *, project_id: str) -> List[Agent]:
        stmt = select(Agent).where(Agent.project_id == project_id)
        return list(self.db.execute(stmt).scalars().all())

    # -- Prompt Versions ----------------------------------------------------

    def create_prompt_version(
        self, *, agent_id: str, content: str, created_by: Optional[str] = None
    ) -> PromptVersion:
        next_version = self._next_prompt_version_number(agent_id)
        prompt_version = PromptVersion(
            agent_id=agent_id, version=next_version, content=content, created_by=created_by
        )
        self.db.add(prompt_version)
        self.db.commit()
        self.db.refresh(prompt_version)
        return prompt_version

    def list_prompt_versions(self, *, agent_id: str) -> List[PromptVersion]:
        stmt = select(PromptVersion).where(PromptVersion.agent_id == agent_id).order_by(PromptVersion.version)
        return list(self.db.execute(stmt).scalars().all())

    def _next_prompt_version_number(self, agent_id: str) -> int:
        existing = self.list_prompt_versions(agent_id=agent_id)
        return (existing[-1].version + 1) if existing else 1

    # -- Agent Versions -----------------------------------------------------

    def create_agent_version(
        self,
        *,
        agent_id: str,
        name: str,
        role: str,
        description: Optional[str] = None,
        prompt_version_id: Optional[str] = None,
        capabilities: Optional[list] = None,
        model_policy: Optional[dict] = None,
        default_model_strategy: DefaultModelStrategy = DefaultModelStrategy.MANUAL_REQUIRED,
        timeout_seconds: Optional[int] = None,
    ) -> AgentVersion:
        """Always inserts a new row — this is what "editing" a published
        Agent means (Section 12.3), never an UPDATE of an existing one."""
        next_version = self._next_agent_version_number(agent_id)
        agent_version = AgentVersion(
            agent_id=agent_id,
            version=next_version,
            name=name,
            role=role,
            description=description,
            prompt_version_id=prompt_version_id,
            capabilities=capabilities,
            model_policy=model_policy,
            default_model_strategy=default_model_strategy,
            timeout_seconds=timeout_seconds,
            status=VersionStatus.DRAFT,
        )
        self.db.add(agent_version)
        self.db.commit()
        self.db.refresh(agent_version)
        return agent_version

    def get_agent_version(self, agent_version_id: str) -> Optional[AgentVersion]:
        return self.db.get(AgentVersion, agent_version_id)

    def list_agent_versions(self, *, agent_id: str) -> List[AgentVersion]:
        stmt = select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.version)
        return list(self.db.execute(stmt).scalars().all())

    def _next_agent_version_number(self, agent_id: str) -> int:
        existing = self.list_agent_versions(agent_id=agent_id)
        return (existing[-1].version + 1) if existing else 1

    # -- Lifecycle transitions (Section 26.7) --------------------------------

    def publish_agent_version(self, agent_version_id: str) -> AgentVersion:
        version = self._require_agent_version(agent_version_id)
        self._require_transition(version, _PUBLISH_ALLOWED_FROM, "publish")
        version.status = VersionStatus.ACTIVE
        version.published_at = _utcnow()
        self._sync_agent_current_status(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def deprecate_agent_version(self, agent_version_id: str) -> AgentVersion:
        version = self._require_agent_version(agent_version_id)
        self._require_transition(version, _DEPRECATE_ALLOWED_FROM, "deprecate")
        version.status = VersionStatus.DEPRECATED
        self._sync_agent_current_status(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def retire_agent_version(self, agent_version_id: str) -> AgentVersion:
        version = self._require_agent_version(agent_version_id)
        self._require_transition(version, _RETIRE_ALLOWED_FROM, "retire")
        version.status = VersionStatus.RETIRED
        self._sync_agent_current_status(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def _require_agent_version(self, agent_version_id: str) -> AgentVersion:
        version = self.get_agent_version(agent_version_id)
        if version is None:
            raise NotFoundError(f"Agent version {agent_version_id} not found.")
        return version

    def _require_transition(self, version: AgentVersion, allowed_from: set, action: str) -> None:
        if version.status not in allowed_from:
            raise InvalidStateTransitionError(
                f"Cannot {action} agent version {version.id} from status "
                f"{version.status.value!r} (allowed from: "
                f"{sorted(s.value for s in allowed_from)})."
            )

    def _sync_agent_current_status(self, version: AgentVersion) -> None:
        # Denormalized convenience mirror (Section 24.2 note) — kept in
        # sync here, in the one place status ever changes, rather than
        # trusted to be recomputed by every caller.
        agent = self.db.get(Agent, version.agent_id)
        if agent is not None:
            agent.current_status = version.status
