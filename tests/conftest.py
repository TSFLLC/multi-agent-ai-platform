import pytest
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401 — populates Base.metadata
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
from app.models.agents import Agent, AgentVersion
from app.models.artifacts_eval import Artifact
from app.models.governance import Budget
from app.models.identity import Organization, Project
from app.models.tasks import AgentRun, Task, TaskRun


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


def make_agent_run(db, task_run=None, agent_version=None, status=AgentRunStatus.CREATED):
    task_run = task_run or make_task_run(db)
    agent_version = agent_version or make_agent_version(db)
    run = AgentRun(task_run_id=task_run.id, agent_version_id=agent_version.id, status=status)
    db.add(run)
    db.flush()
    return run


def make_artifact(db, agent_run=None, type_=ArtifactType.DIFF):
    agent_run = agent_run or make_agent_run(db)
    artifact = Artifact(agent_run_id=agent_run.id, type=type_, storage_ref="data/artifacts/x.diff")
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
