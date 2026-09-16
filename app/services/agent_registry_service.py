"""Agent Registry Service — Section 11. Agent + Agent Version CRUD,
activation/retirement, policy validation. Real implementation: MA2."""

from app.services.base import BaseService


class AgentRegistryService(BaseService):
    def create_agent(self, *args, **kwargs):
        raise NotImplementedError("AgentRegistryService lands in MA2")

    def publish_version(self, *args, **kwargs):
        """Must never mutate a published agent_versions row in place —
        Section 12.3's reproducibility invariant."""
        raise NotImplementedError("AgentRegistryService lands in MA2")
