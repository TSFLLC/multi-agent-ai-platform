import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401 — populates Base.metadata
from app.api.deps import get_db, get_session_factory
from app.db.base import Base
from app.db.enums import (
    AgentRunStatus,
    ArtifactType,
    ExecutionMode,
    TaskRunStatus,
    TaskStatus,
    VersionStatus,
)
from app.db.session import build_engine
from app.main import app as fastapi_app
from app.models.agents import Agent, AgentVersion
from app.models.artifacts_eval import Artifact
from app.models.governance import Budget
from app.models.identity import Organization, Project
from app.models.tasks import AgentRun, Task, TaskRun
from app.secrets_store import InMemorySecretStore, set_secret_store
from app.worker import Worker


@pytest.fixture(autouse=True)
def _isolated_secret_store():
    """Every test gets a fresh in-memory secret store — never the real OS
    keychain. Autouse so no test can accidentally leak a fixture "secret"
    into the operator's actual Windows Credential Manager / macOS
    Keychain / Secret Service."""
    set_secret_store(InMemorySecretStore())
    yield
    set_secret_store(InMemorySecretStore())


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def engine(db_path):
    eng = build_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(session_factory):
    """A TestClient wired to the same temp DB as the ``db``/``engine``
    fixtures via FastAPI's dependency_overrides — never the real
    data/multi_agent_platform.db. Deliberately not used as a context
    manager, so app startup/shutdown (lifespan) never fires against the
    real engine either; lifespan itself is tested separately with an
    explicitly monkeypatched engine."""

    def _override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    fastapi_app.dependency_overrides[get_session_factory] = lambda: session_factory
    try:
        yield TestClient(fastapi_app)
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def auth_token(tmp_path, monkeypatch):
    """Points app.config.settings.auth_token_path at a temp file so each
    test gets its own isolated local auth token, never the real
    data/local_auth_token."""
    from app.config import settings

    monkeypatch.setattr(settings, "auth_token_path", tmp_path / "local_auth_token")

    from app.auth import ensure_local_auth_token

    return ensure_local_auth_token()


@pytest.fixture()
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture()
def bootstrap(db):
    """Runs the real idempotent bootstrap against the temp DB, committing
    so it's visible to the client fixture's separate sessions too."""
    from app.bootstrap import ensure_local_bootstrap

    identities = ensure_local_bootstrap(db)
    db.commit()
    return identities


@pytest.fixture()
def fast_worker(session_factory):
    """A Worker tuned for fast, deterministic tests — short lease, tiny
    heartbeat/work intervals, few iterations — bound to the shared temp DB."""
    return Worker(
        session_factory=session_factory,
        poll_interval_seconds=0.02,
        lease_seconds=2,
        heartbeat_interval_seconds=0.05,
        internal_test_work_seconds=0.02,
        internal_test_iterations=2,
    )


# --- Minimal entity factories, shared across test modules -----------------


def make_org(db, name="Acme"):
    org = Organization(name=name)
    db.add(org)
    db.flush()
    return org


def make_project(db, org=None, name="Default Project"):
    org = org or make_org(db)
    project = Project(org_id=org.id, name=name)
    db.add(project)
    db.flush()
    return project


def make_task(db, project=None, execution_mode=ExecutionMode.SINGLE_AGENT, status=TaskStatus.READY):
    project = project or make_project(db)
    task = Task(project_id=project.id, title="Do the thing", execution_mode=execution_mode, status=status)
    db.add(task)
    db.flush()
    return task


def make_task_run(db, task=None, status=TaskRunStatus.CREATED):
    task = task or make_task(db)
    run = TaskRun(task_id=task.id, status=status)
    db.add(run)
    db.flush()
    return run


def make_agent(db, project=None, name="Backend Engineer"):
    project = project or make_project(db)
    agent = Agent(project_id=project.id, name=name, role="engineer")
    db.add(agent)
    db.flush()
    return agent


def make_agent_version(db, agent=None, version=1, status=VersionStatus.DRAFT):
    agent = agent or make_agent(db)
    av = AgentVersion(agent_id=agent.id, version=version, name=agent.name, role=agent.role, status=status)
    db.add(av)
    db.flush()
    return av


def make_runnable_agent_version(db, agent=None, version=1):
    """An ACTIVE Agent Version whose model policy names a real provider model
    -- what a workflow AGENT node needs to be publishable (MA7.6A: publish
    refuses a node that could never resolve a model)."""
    import uuid

    agent = agent or make_agent(db)
    av = make_agent_version(db, agent, version=version, status=VersionStatus.ACTIVE)
    provider = make_provider(db)
    model = make_model(db, canonical_model_id=f"test/runnable-{uuid.uuid4().hex[:12]}")
    provider_model = make_provider_model(db, model=model, provider=provider)
    av.model_policy = {"mode": "manual", "manual_provider_model_id": provider_model.id}
    db.flush()
    return av


def make_evaluation_definition(db, project=None, name="Software Engineer Correctness Rubric"):
    from app.models.evaluation_definitions import EvaluationDefinition

    project = project or make_project(db)
    definition = EvaluationDefinition(project_id=project.id, name=name)
    db.add(definition)
    db.flush()
    return definition


def make_evaluation_definition_version(db, definition=None, version=1, status=VersionStatus.DRAFT):
    from app.models.evaluation_definitions import EvaluationCriterion, EvaluationDefinitionVersion

    definition = definition or make_evaluation_definition(db)
    ver = EvaluationDefinitionVersion(evaluation_definition_id=definition.id, version=version, status=status)
    db.add(ver)
    db.flush()
    db.add(
        EvaluationCriterion(
            evaluation_definition_version_id=ver.id, key="correctness", label="Correctness", order_index=0
        )
    )
    db.flush()
    return ver


def make_agent_run(db, task_run=None, agent_version=None, status=AgentRunStatus.CREATED):
    task_run = task_run or make_task_run(db)
    agent_version = agent_version or make_agent_version(db)
    run = AgentRun(task_run_id=task_run.id, agent_version_id=agent_version.id, status=status)
    db.add(run)
    db.flush()
    return run


def make_artifact(db, agent_run=None, type_=ArtifactType.DIFF, content_hash=None):
    agent_run = agent_run or make_agent_run(db)
    artifact = Artifact(
        agent_run_id=agent_run.id, type=type_, storage_ref="data/artifacts/x.diff", content_hash=content_hash
    )
    db.add(artifact)
    db.flush()
    return artifact


def make_artifact_with_content(db, tmp_path, agent_run=None, content="hello world", type_=ArtifactType.FILE):
    """Like ``make_artifact``, but backed by a real file on disk with a
    real sha256 content_hash -- MA6 Slice 2's deterministic checkers
    (app.services.evaluation_execution_service) read actual artifact
    content, so a bare bookkeeping row (make_artifact's default) isn't
    enough for those tests."""
    import hashlib

    agent_run = agent_run or make_agent_run(db)
    path = tmp_path / f"artifact-{len(list(tmp_path.iterdir()))}.txt"
    path.write_text(content, encoding="utf-8")
    artifact = Artifact(
        agent_run_id=agent_run.id,
        type=type_,
        storage_ref=str(path),
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
    db.add(artifact)
    db.flush()
    return artifact


def make_budget(db, project=None, limit_amount="10.00"):
    from decimal import Decimal

    from app.db.enums import BudgetScope

    project = project or make_project(db)
    # expire_on_commit=False sessions never re-read this from the DB, so a
    # plain str would stay a str on the Python side forever — construct
    # the real Decimal the column represents.
    budget = Budget(project_id=project.id, scope=BudgetScope.TASK, limit_amount=Decimal(limit_amount))
    db.add(budget)
    db.flush()
    return budget


def make_provider(db, type_=None, name="OpenRouter"):
    from app.db.enums import ProviderType
    from app.models.providers import Provider

    provider = Provider(type=type_ or ProviderType.OPENROUTER, name=name)
    db.add(provider)
    db.flush()
    return provider


def make_model(db, canonical_model_id="test/model-1", **kwargs):
    from app.models.providers import Model

    model = Model(canonical_model_id=canonical_model_id, **kwargs)
    db.add(model)
    db.flush()
    return model


def make_provider_model(db, model=None, provider=None, **kwargs):
    from decimal import Decimal

    from app.models.providers import ProviderModel

    model = model or make_model(db)
    provider = provider or make_provider(db)
    kwargs.setdefault("cost_input_per_mtok", Decimal("1.00"))
    kwargs.setdefault("cost_output_per_mtok", Decimal("2.00"))
    pm = ProviderModel(
        model_id=model.id, provider_id=provider.id, provider_model_id=model.canonical_model_id, **kwargs
    )
    db.add(pm)
    db.flush()
    return pm
