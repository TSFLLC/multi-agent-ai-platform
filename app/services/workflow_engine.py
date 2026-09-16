"""Workflow / DAG Engine — Section 11, 16. Loads a Workflow Version, drives
node execution as a resumable state machine, enforces bounding rules
(repair_loop max_iterations, cycle rejection). Real implementation: MA7."""

from app.services.base import BaseService


class WorkflowEngine(BaseService):
    def validate_version(self, *args, **kwargs):
        """Publish-time validator (Section 16.3) — must reject a
        repair_loop node without max_iterations, and any cycle outside the
        repair_loop construct."""
        raise NotImplementedError("WorkflowEngine lands in MA7")

    def run(self, *args, **kwargs):
        raise NotImplementedError("WorkflowEngine lands in MA7")
