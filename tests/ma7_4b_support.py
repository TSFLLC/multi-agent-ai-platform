"""Helpers for the MA7.4b (parallel fan-out / fan-in) tests. Not a test module.

* ``build_graph`` publishes ANY DAG (arbitrary node keys/kinds/edges) through
  the real WorkflowDefinitionService -- nothing here is specific to Test /
  Security / Code Review; those are just the labels the acceptance test uses.
* ``RoleProvider`` is the ONLY fake in the production-path tests: it stands in
  for the model provider (the network boundary). AgentExecutionService, the
  Worker, the queue, budget/model resolution, the Flight Recorder and the
  TaskRun/AgentRun lifecycle are all real. Each agent node resolves to its own
  provider model, which is how the fake tells the roles apart.
* ``running_workers`` runs several real Worker instances on threads -- the
  stand-in for "multiple Worker processes" (one Worker still processes one job
  at a time; thread concurrency INSIDE a Worker is MA7.4c, not tested here).
"""

import threading
import time
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from app.db.enums import TaskRunStatus, VersionStatus, WorkflowNodeType
from app.models.providers import Provider
from app.models.tasks import TaskRun
from app.providers.base import InvokeResponse
from app.services.execution_service import AgentExecutionService
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.worker import Worker
from tests.conftest import (
    make_agent,
    make_agent_version,
    make_model,
    make_provider,
    make_provider_model,
    make_task,
)
from tests.ma7_3b_support import AGENT, APPROVAL, TERMINAL, Built

Graph = namedtuple("Graph", "built role_of")

_KIND_TO_TYPE = {
    AGENT: WorkflowNodeType.AGENT,
    APPROVAL: WorkflowNodeType.HUMAN_APPROVAL,
    TERMINAL: WorkflowNodeType.TERMINAL,
}


def build_graph(db, project, nodes, edges, *, label, real=False, publish=True, approval_group="eng-leads"):
    """A workflow with the given ``nodes`` -- ordered ``(node_key, kind)`` --
    and ``edges`` -- ``(from_key, to_key)`` -- published through the real
    definition service (so publish-time validation runs). With ``real=True``
    every AGENT node's version is ACTIVE with a manual model policy pointing at
    its OWN provider model, so the real AgentExecutionService can execute it
    against ``RoleProvider``. Returns ``Graph(built, role_of)``; ``role_of``
    maps a provider-model id to the node key it belongs to."""
    definitions = WorkflowDefinitionService(db)
    workflow = definitions.create_workflow(project.id, f"{label}-workflow")
    version = definitions.get_latest_version(workflow.id)
    provider = db.query(Provider).first() or make_provider(db)

    created, agent_versions, role_of = {}, {}, {}
    for key, kind in nodes:
        if kind == AGENT:
            agent_version = make_agent_version(
                db,
                make_agent(db, project, f"{label}-{key}"),
                status=VersionStatus.ACTIVE if real else VersionStatus.DRAFT,
            )
            if real:
                canonical = f"fake/{label}-{key}"
                provider_model = make_provider_model(
                    db,
                    model=make_model(db, canonical_model_id=canonical),
                    provider=provider,
                    cost_input_per_mtok=Decimal(0),
                    cost_output_per_mtok=Decimal(0),
                )
                agent_version.model_policy = {"mode": "manual", "manual_provider_model_id": provider_model.id}
                role_of[canonical] = key
            agent_versions[key] = agent_version
            created[key] = definitions.add_node(
                workflow.id,
                version.version,
                key,
                WorkflowNodeType.AGENT,
                config={"agent_version_id": agent_version.id},
            )
        elif kind == APPROVAL:
            created[key] = definitions.add_node(
                workflow.id,
                version.version,
                key,
                WorkflowNodeType.HUMAN_APPROVAL,
                config={"approval_group": approval_group},
            )
        else:
            created[key] = definitions.add_node(workflow.id, version.version, key, _KIND_TO_TYPE[kind])
    for source, target in edges:
        definitions.add_edge(workflow.id, version.version, created[source].id, created[target].id)

    task = make_task(db, project)
    task_run = TaskRun(task_id=task.id, status=TaskRunStatus.CREATED, created_at=datetime.now(timezone.utc))
    db.add(task_run)
    db.commit()
    published = definitions.publish_version(workflow.id, version.version) if publish else version
    return Graph(Built(workflow, version, published, created, agent_versions, task_run, project), role_of)


# Planner -> Engineer -> {Test Engineer, Security Reviewer, Code Reviewer} -> Aggregator -> TERMINAL
ACCEPTANCE_NODES = [
    ("planner", AGENT),
    ("engineer", AGENT),
    ("test_engineer", AGENT),
    ("security_reviewer", AGENT),
    ("code_reviewer", AGENT),
    ("aggregator", AGENT),
    ("result", TERMINAL),
]
REVIEWERS = ("code_reviewer", "security_reviewer", "test_engineer")  # canonical (node_key) order
ACCEPTANCE_EDGES = (
    [("planner", "engineer")]
    + [("engineer", reviewer) for reviewer in REVIEWERS]
    + [(reviewer, "aggregator") for reviewer in REVIEWERS]
    + [("aggregator", "result")]
)


def build_acceptance(db, project, *, label="acc", real=True):
    return build_graph(db, project, ACCEPTANCE_NODES, ACCEPTANCE_EDGES, label=label, real=real)


def build_star(db, project, width, *, label="star"):
    """entry -> width branches -> join (a single fan-out of ``width``); every
    node an AGENT except the TERMINAL end. Published, so it is validated."""
    branches = [f"b{i:02d}" for i in range(width)]
    nodes = [("entry", AGENT)] + [(key, AGENT) for key in branches] + [("join", AGENT), ("end", TERMINAL)]
    edges = [("entry", key) for key in branches] + [(key, "join") for key in branches] + [("join", "end")]
    return build_graph(db, project, nodes, edges, label=label)


class RoleProvider:
    """The fake provider boundary. ``hooks[role]`` runs INSIDE ``invoke`` (on
    the calling worker's thread) and may block, raise a provider error, or
    record something -- that is how tests hold one branch in flight, fail
    another, or prove several are in flight at once."""

    def __init__(self, role_of, *, text=None):
        self.role_of = role_of
        self.text = text or (lambda role: f"OUTPUT-OF-{role}")
        self.hooks = {}
        self.calls = []  # (role, user_prompt) in call order
        self._lock = threading.Lock()

    def invoke(self, request):
        role = self.role_of[request.provider_model_id]
        with self._lock:
            self.calls.append((role, request.user_prompt))
        hook = self.hooks.get(role)
        if hook is not None:
            hook(request)
        return InvokeResponse(text=self.text(role), tokens_in=10, tokens_out=5)

    def roles(self):
        with self._lock:
            return [role for role, _prompt in self.calls]

    def count(self, role):
        return self.roles().count(role)

    def prompt_of(self, role):
        with self._lock:
            return next(prompt for r, prompt in self.calls if r == role)


def install_provider(monkeypatch, provider):
    """Routes every AgentExecutionService the Worker builds to ``provider``."""
    real_init = AgentExecutionService.__init__

    def init(self, db, adapter_factory=None):
        real_init(self, db, adapter_factory=lambda _db, _provider: provider)

    monkeypatch.setattr(AgentExecutionService, "__init__", init)
    return provider


def new_worker(session_factory):
    return Worker(
        session_factory=session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=60,
        heartbeat_interval_seconds=0.05,
        reconcile_interval_seconds=0,
    )


@contextmanager
def running_workers(session_factory, count=1):
    """``count`` real Worker instances polling the shared queue on their own
    threads (the multi-process stand-in). Always shut down and joined."""
    workers = [new_worker(session_factory) for _ in range(count)]
    threads = [threading.Thread(target=w.run_forever, daemon=True) for w in workers]
    for thread in threads:
        thread.start()
    try:
        yield workers
    finally:
        for worker in workers:
            worker.request_shutdown()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads), "a worker thread did not stop"


def wait_until(predicate, timeout=30.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
