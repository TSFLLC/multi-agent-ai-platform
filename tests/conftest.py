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


# --- AIL.5B Build With Me fixtures -----------------------------------------------


@pytest.fixture()
def ail5b_user(db):
    """Test user for AIL.5B tests."""
    from app.models.identity import Organization, User
    from uuid import uuid4

    org = Organization(
        id=str(uuid4()),
        name="AIL.5B Test Org",
    )
    db.add(org)
    db.flush()

    user = User(
        id=str(uuid4()),
        org_id=org.id,
        email=f"ail5b-test-{str(uuid4())[:8]}@example.com",
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def ail5b_author_user(db):
    """Template author user (id='user-1' for tests)."""
    from app.models.identity import Organization, User

    org = Organization(id="org-author", name="Author Org")
    db.add(org)
    db.flush()

    user = User(id="user-1", org_id=org.id, email="template-author@test.local")
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def ail5b_learner_user(db):
    """Project learner user (id='learner-1' for tests)."""
    from app.models.identity import Organization, User

    org = Organization(id="org-learner", name="Learner Org")
    db.add(org)
    db.flush()

    user = User(id="learner-1", org_id=org.id, email="project-learner@test.local")
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def ail5b_user_a(db):
    """User A for access isolation tests."""
    from app.models.identity import Organization, User

    org = Organization(id="org-user-a", name="User A Org")
    db.add(org)
    db.flush()

    user = User(id="user-a", org_id=org.id, email="user-a@test.local")
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def ail5b_user_b(db):
    """User B for access isolation tests."""
    from app.models.identity import Organization, User

    org = Organization(id="org-user-b", name="User B Org")
    db.add(org)
    db.flush()

    user = User(id="user-b", org_id=org.id, email="user-b@test.local")
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def ail5b_program_version(db, ail5b_user):
    """Program version for AIL.5B enrollment."""
    from app.models.academy import AcademyProgram, AcademyProgramVersion
    from app.db.enums import AcademyProgramVersionStatus
    from uuid import uuid4
    from datetime import datetime

    program = AcademyProgram(
        id=str(uuid4()),
        slug="ail5b-test-program",
        title="AIL.5B Test Program",
        author_user_id=ail5b_user.id,
    )
    db.add(program)
    db.flush()

    version = AcademyProgramVersion(
        id=str(uuid4()),
        program_id=program.id,
        version=1,
        status=AcademyProgramVersionStatus.PUBLISHED,
        duration_days=30,
        completion_rules={},
        published_at=datetime.utcnow(),
    )
    db.add(version)
    db.commit()
    return version


@pytest.fixture()
def ail5b_enrollment(db, ail5b_user, ail5b_program_version):
    """Enrollment for AIL.5B project attempts."""
    from app.models.academy import AcademyEnrollment
    from app.db.enums import AcademyPace, AcademyEnrollmentStatus
    from uuid import uuid4

    enrollment = AcademyEnrollment(
        id=str(uuid4()),
        user_id=ail5b_user.id,
        program_version_id=ail5b_program_version.id,
        pace=AcademyPace.SCHEDULED,
        status=AcademyEnrollmentStatus.ACTIVE,
    )
    db.add(enrollment)
    db.commit()
    return enrollment


@pytest.fixture()
def ail5b_template(db, ail5b_user):
    """Published project template for tests."""
    from app.models.academy import ProjectTemplate
    from app.db.enums import ProjectAudienceLevel, ProjectLadderLevel, ProjectTemplateBuildMode
    from uuid import uuid4
    from datetime import datetime

    template = ProjectTemplate(
        id=str(uuid4()),
        template_key="ail5b-test-template",
        version=1,
        status="published",
        title="AIL.5B Test Project",
        audience_level=ProjectAudienceLevel.BEGINNER,
        ladder_level=ProjectLadderLevel.L2,
        build_mode=ProjectTemplateBuildMode.NO_CODE,
        brief_md="# Test Brief",
        author_user_id=ail5b_user.id,
        published_at=datetime.utcnow(),
    )
    db.add(template)
    db.commit()
    return template


@pytest.fixture()
def ail5b_attempt(db, ail5b_user, ail5b_template, ail5b_enrollment):
    """Project attempt for milestone tests."""
    from app.models.academy import ProjectAttempt
    from app.db.enums import ProjectAttemptStatus
    from uuid import uuid4

    attempt = ProjectAttempt(
        id=str(uuid4()),
        user_id=ail5b_user.id,
        project_template_id=ail5b_template.id,
        enrollment_id=ail5b_enrollment.id,
        is_capstone=False,
        status=ProjectAttemptStatus.ACTIVE,
        brief_snapshot={},
    )
    db.add(attempt)
    db.commit()
    return attempt


@pytest.fixture()
def ail5b_milestone(db, ail5b_template):
    """Project milestone for attempt."""
    from app.models.academy import ProjectMilestone
    from uuid import uuid4

    milestone = ProjectMilestone(
        id=str(uuid4()),
        project_template_id=ail5b_template.id,
        position=1,
        title="Test Milestone",
        instructions_md="# Instructions",
        check_spec={"type": "test", "cases": []},
    )
    db.add(milestone)
    db.commit()
    return milestone


@pytest.fixture()
def ail5b_milestone_attempt(db, ail5b_attempt, ail5b_milestone):
    """Milestone attempt for state tests."""
    from app.models.academy import MilestoneAttempt
    from app.db.enums import MilestoneAttemptStatus, MilestoneAttemptMode
    from uuid import uuid4

    milestone_attempt = MilestoneAttempt(
        id=str(uuid4()),
        project_attempt_id=ail5b_attempt.id,
        project_milestone_id=ail5b_milestone.id,
        status=MilestoneAttemptStatus.NOT_STARTED,
        attempts_count=0,
        mode=MilestoneAttemptMode.NORMAL,
    )
    db.add(milestone_attempt)
    db.commit()
    return milestone_attempt


@pytest.fixture()
def ail5b_template_for_user_a(db, ail5b_author_user):
    """Project template for User A's attempts."""
    from app.models.academy import ProjectTemplate
    from app.db.enums import ProjectAudienceLevel, ProjectLadderLevel, ProjectTemplateBuildMode
    from uuid import uuid4
    from datetime import datetime

    template = ProjectTemplate(
        id=str(uuid4()),
        template_key="template-user-a",
        version=1,
        status="published",
        title="Template for User A",
        audience_level=ProjectAudienceLevel.BEGINNER,
        ladder_level=ProjectLadderLevel.L2,
        build_mode=ProjectTemplateBuildMode.NO_CODE,
        brief_md="# User A Template",
        author_user_id=ail5b_author_user.id,
        published_at=datetime.utcnow(),
    )
    db.add(template)
    db.commit()
    return template


@pytest.fixture()
def ail5b_template_for_user_b(db, ail5b_author_user):
    """Project template for User B's attempts."""
    from app.models.academy import ProjectTemplate
    from app.db.enums import ProjectAudienceLevel, ProjectLadderLevel, ProjectTemplateBuildMode
    from uuid import uuid4
    from datetime import datetime

    template = ProjectTemplate(
        id=str(uuid4()),
        template_key="template-user-b",
        version=1,
        status="published",
        title="Template for User B",
        audience_level=ProjectAudienceLevel.BEGINNER,
        ladder_level=ProjectLadderLevel.L2,
        build_mode=ProjectTemplateBuildMode.NO_CODE,
        brief_md="# User B Template",
        author_user_id=ail5b_author_user.id,
        published_at=datetime.utcnow(),
    )
    db.add(template)
    db.commit()
    return template
