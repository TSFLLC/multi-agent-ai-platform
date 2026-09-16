"""Sandbox Manager — Section 11, 21. Creates/destroys isolated workspaces
(git worktree, optionally a container) per Agent Run under
``data/workspaces/``; enforces filesystem/env/secret boundaries. Runs
entirely on the local machine — no remote sandbox pool. Real
implementation: MA3."""

from app.services.base import BaseService


class SandboxManager(BaseService):
    def provision(self, *args, **kwargs):
        raise NotImplementedError("SandboxManager lands in MA3")

    def teardown(self, *args, **kwargs):
        """Forced immediately once cancellation is acknowledged (Section
        26.7 rule 2), even though the underlying run stops cooperatively."""
        raise NotImplementedError("SandboxManager lands in MA3")
