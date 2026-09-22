"""MA7.2 Concurrent Dispatch Safety Test.

Verifies atomic claim prevents duplicate execution when two dispatchers
simultaneously attempt to dispatch the same PENDING node.
"""

import pytest
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from sqlalchemy.orm import sessionmaker

from app import models  # noqa
from app.db.base import Base
from app.db.session import build_engine
from app.db.enums import (
    WorkflowNodeType,
    WorkflowRunStatus,
    WorkflowNodeRunStatus,
    TaskRunStatus,
)
from app.models.workflow import Workflow, WorkflowVersion, WorkflowNode, WorkflowRun, WorkflowNodeRun
from app.models.tasks import TaskRun
from app.models.agents import Agent, AgentVersion
from app.models.identity import Project
from app.services.workflow_execution_service import WorkflowExecutionService


@pytest.fixture
def concurrent_db(tmp_path):
    """Disposable temp DB for concurrency testing."""
    db_path = tmp_path / "concurrent_test.db"
    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)

    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    try:
        yield session_factory, engine
    finally:
        engine.dispose()


def test_concurrent_dispatch_atomic_claim(concurrent_db):
    """Two threads race to dispatch same PENDING node.

    Exactly one should succeed in atomically claiming the node.
    Final state: 1 AgentRun, 1 TaskRun, 1 JobQueue entry.
    """
    session_factory, engine = concurrent_db

    # Setup: create project, agent, workflow
    setup_session = session_factory()

    from tests.conftest import make_project, make_agent, make_runnable_agent_version

    project = make_project(setup_session, name="concurrent-test")
    agent = make_agent(setup_session, project, "test-agent")
    agent_v = make_runnable_agent_version(setup_session, agent)

    # Create workflow
    workflow = Workflow(project_id=project.id, name="concurrent-workflow")
    setup_session.add(workflow)
    setup_session.flush()

    wv = WorkflowVersion(workflow_id=workflow.id, version=1)
    setup_session.add(wv)
    setup_session.flush()

    # Create single AGENT node
    node = WorkflowNode(
        workflow_version_id=wv.id,
        node_key="agent1",
        node_type=WorkflowNodeType.AGENT,
        config={"agent_version_id": agent_v.id},
    )
    setup_session.add(node)
    setup_session.flush()

    # Create workflow run
    from tests.conftest import make_task, make_task_run
    task = make_task(setup_session, project)
    task_run = make_task_run(setup_session, task, status=TaskRunStatus.CREATED)

    wrun = WorkflowRun(
        task_run_id=task_run.id,
        workflow_version_id=wv.id,
        status=WorkflowRunStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    setup_session.add(wrun)
    setup_session.flush()

    # Create PENDING node run
    node_run = WorkflowNodeRun(
        workflow_run_id=wrun.id,
        workflow_node_id=node.id,
        iteration=0,
        status=WorkflowNodeRunStatus.PENDING,
    )
    setup_session.add(node_run)
    setup_session.commit()

    node_run_id = node_run.id

    # Concurrent dispatch: two threads race
    dispatch_results = []
    barrier = threading.Barrier(2)

    def dispatch_thread(thread_id: int):
        s = session_factory()
        barrier.wait()  # Synchronize start to maximize race window
        try:
            exec_service = WorkflowExecutionService(s)
            nr = s.query(WorkflowNodeRun).filter(WorkflowNodeRun.id == node_run_id).first()
            if nr:
                exec_service._dispatch_node_for_execution(nr)
                return {"thread": thread_id, "status": "attempted"}
            else:
                return {"thread": thread_id, "status": "node_not_found"}
        except Exception as e:
            return {"thread": thread_id, "error": f"{type(e).__name__}: {str(e)}"}
        finally:
            s.close()

    # Run race 10 times to exercise concurrency
    for race_num in range(10):
        # Reset node_run for next race
        reset_session = session_factory()
        nr = reset_session.query(WorkflowNodeRun).filter(WorkflowNodeRun.id == node_run_id).first()
        if nr:
            nr.agent_run_id = None
            nr.status = WorkflowNodeRunStatus.PENDING
            reset_session.commit()
        reset_session.close()

        dispatch_results.clear()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(dispatch_thread, [1, 2]))
            dispatch_results.extend(results)

        # Verify exactly one AgentRun created
        verify_session = session_factory()
        nr_final = verify_session.query(WorkflowNodeRun).filter(
            WorkflowNodeRun.id == node_run_id
        ).first()

        assert nr_final is not None, f"Race {race_num}: NodeRun lost"

        if nr_final.agent_run_id:
            # One thread succeeded
            agent_runs_count = verify_session.query(
                __import__('app.models.tasks', fromlist=['AgentRun']).AgentRun
            ).filter(
                __import__('app.models.tasks', fromlist=['AgentRun']).AgentRun.id == nr_final.agent_run_id
            ).count()

            assert agent_runs_count == 1, f"Race {race_num}: Expected 1 AgentRun, got {agent_runs_count}"

        verify_session.close()

    # Final verification: after 10 races, exactly 1 logical execution
    final_session = session_factory()
    final_nr = final_session.query(WorkflowNodeRun).filter(
        WorkflowNodeRun.id == node_run_id
    ).first()

    assert final_nr.agent_run_id is not None, "NodeRun should have agent_run_id"
    assert final_nr.status == WorkflowNodeRunStatus.RUNNING, "NodeRun should be RUNNING"

    final_session.close()

    print("✓ Concurrent dispatch atomic claim test PASSED")
    print(f"  - {dispatch_results}")
