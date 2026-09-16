"""Tool Bus / Permission Broker — Section 11, 15. The mandatory choke point
for every tool call (ADR-7) — no direct tool-invocation path from the
Agent Runtime is ever added. Default posture is deny-all; deny always wins
over allow. Real implementation: MA3 (repo/terminal tools first)."""

from app.services.base import BaseService


class ToolBus(BaseService):
    def check_permission(self, *args, **kwargs):
        raise NotImplementedError("ToolBus lands in MA3")

    def invoke(self, *args, **kwargs):
        """Records the call (inputs, outputs, permission decision, latency)
        to the Flight Recorder on every invocation — allowed or denied."""
        raise NotImplementedError("ToolBus lands in MA3")
