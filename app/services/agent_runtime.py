"""Agent Runtime (Orchestration Workers) — Section 11. Executes one Agent
Run: builds prompt/context, calls the Model Router, calls the Tool Bus,
writes artifacts, emits Flight Recorder events. Runs as a local in-process
worker leasing job_queue rows (Section 10.5.3) — never a remote worker
fleet. Real implementation: MA3."""

from app.services.base import BaseService


class AgentRuntime(BaseService):
    def execute(self, *args, **kwargs):
        """Never holds a DB transaction open across a model/tool call
        (Section 10.5.2) — claim (short tx) -> execute (no open tx) ->
        write result (short tx)."""
        raise NotImplementedError("AgentRuntime lands in MA3")
