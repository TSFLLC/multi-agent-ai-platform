"""Workflow / DAG resource boundary — Section 25.2, 25.5.

MA7.1: Workflow authoring, versioning, DAG validation, publish.
No execution (WorkflowRun/WorkflowNodeRun execution is MA7.2+).
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.enums import VersionStatus, WorkflowNodeType
from app.models.workflow import Workflow, WorkflowVersion, WorkflowRun
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowNodeCreate,
    WorkflowNodeRunRead,
    WorkflowRead,
    WorkflowRunRead,
    WorkflowVersionRead,
)
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_validation_service import DAGValidationError

router = APIRouter(tags=["workflows"])


@router.get("/workflows", response_model=List[WorkflowRead])
def list_workflows(
    project_id: str, status: Optional[VersionStatus] = None, db: Session = Depends(get_db)
):
    """List workflows in a project, optionally filtered by status."""
    service = WorkflowDefinitionService(db)
    workflows = service.list_workflows(project_id, status=status)
    return [
        WorkflowRead(
            id=w.id,
            project_id=w.project_id,
            name=w.name,
            current_status=w.current_status,
        )
        for w in workflows
    ]


@router.post("/workflows", response_model=WorkflowRead, status_code=201)
def create_workflow(body: WorkflowCreate, db: Session = Depends(get_db)):
    """Create a new workflow with initial DRAFT version."""
    service = WorkflowDefinitionService(db)
    workflow = service.create_workflow(body.project_id, body.name)
    return WorkflowRead(
        id=workflow.id,
        project_id=workflow.project_id,
        name=workflow.name,
        current_status=workflow.current_status,
    )


@router.get("/workflows/{workflow_id}", response_model=WorkflowRead)
def get_workflow(workflow_id: str, db: Session = Depends(get_db)):
    """Get a workflow by ID."""
    service = WorkflowDefinitionService(db)
    workflow = service.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowRead(
        id=workflow.id,
        project_id=workflow.project_id,
        name=workflow.name,
        current_status=workflow.current_status,
    )


@router.post("/workflows/{workflow_id}/versions", response_model=WorkflowVersionRead, status_code=201)
def create_workflow_version(workflow_id: str, db: Session = Depends(get_db)):
    """Create a new DRAFT version for a workflow."""
    service = WorkflowDefinitionService(db)
    workflow = service.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Get latest version number
    latest = service.get_latest_version(workflow_id)
    next_version = (latest.version + 1) if latest else 1

    new_version = WorkflowVersion(workflow_id=workflow_id, version=next_version, status=VersionStatus.DRAFT)
    db.add(new_version)
    db.commit()

    return WorkflowVersionRead(id=new_version.id, workflow_id=workflow_id, version=next_version, status=VersionStatus.DRAFT)


@router.post("/workflows/{workflow_id}/versions/{version}/publish", response_model=WorkflowVersionRead)
def publish_workflow_version(workflow_id: str, version: int, db: Session = Depends(get_db)):
    """Publish a DRAFT workflow version after DAG validation.

    Validates:
    - DAG structure (no cycles, reachability, valid entry/terminal)
    - Node type constraints (max_iterations, required config fields)
    - Referenced agent/evaluation versions exist and are in same project
    - Node configuration is well-formed

    Raises 400 if validation fails with detailed issues.
    """
    service = WorkflowDefinitionService(db)

    try:
        published_version = service.publish_version(workflow_id, version)
        return WorkflowVersionRead(
            id=published_version.id,
            workflow_id=workflow_id,
            version=version,
            status=published_version.status,
        )
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except DAGValidationError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "DAG validation failed",
                "issues": e.issues,
            },
        )


@router.get("/workflows/{workflow_id}/versions/{version}", response_model=WorkflowVersionRead)
def get_workflow_version(workflow_id: str, version: int, db: Session = Depends(get_db)):
    """Get a specific workflow version."""
    service = WorkflowDefinitionService(db)
    wv = service.get_workflow_version(workflow_id, version)
    if not wv:
        raise HTTPException(status_code=404, detail="Workflow version not found")
    return WorkflowVersionRead(id=wv.id, workflow_id=workflow_id, version=version, status=wv.status)


@router.get("/workflows/{workflow_id}/versions", response_model=List[WorkflowVersionRead])
def list_workflow_versions(workflow_id: str, db: Session = Depends(get_db)):
    """List all versions of a workflow."""
    service = WorkflowDefinitionService(db)
    versions = service.list_workflow_versions(workflow_id)
    return [
        WorkflowVersionRead(id=v.id, workflow_id=workflow_id, version=v.version, status=v.status) for v in versions
    ]


@router.post("/workflows/{workflow_id}/versions/{version}/nodes", status_code=201)
def add_workflow_node(
    workflow_id: str,
    version: int,
    body: WorkflowNodeCreate,
    db: Session = Depends(get_db),
):
    """Add a node to a DRAFT workflow version."""
    service = WorkflowDefinitionService(db)

    try:
        node = service.add_node(
            workflow_id,
            version,
            body.node_key,
            body.node_type,
            config=body.config,
            max_iterations=body.max_iterations,
        )
        return {"id": node.id, "node_key": node.node_key, "node_type": node.node_type}
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/workflows/{workflow_id}/versions/{version}/nodes/{node_id}")
def delete_workflow_node(
    workflow_id: str, version: int, node_id: str, db: Session = Depends(get_db)
):
    """Delete a node from a DRAFT workflow version."""
    service = WorkflowDefinitionService(db)

    try:
        service.delete_node(workflow_id, version, node_id)
        return {"status": "deleted"}
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/workflows/{workflow_id}/versions/{version}/edges", status_code=201)
def add_workflow_edge(
    workflow_id: str,
    version: int,
    from_node_id: str,
    to_node_id: str,
    condition: Optional[dict] = None,
    db: Session = Depends(get_db),
):
    """Add an edge between two nodes in a DRAFT workflow version."""
    service = WorkflowDefinitionService(db)

    try:
        edge = service.add_edge(workflow_id, version, from_node_id, to_node_id, condition=condition)
        return {"id": edge.id, "from_node_id": from_node_id, "to_node_id": to_node_id}
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/workflows/{workflow_id}/versions/{version}/edges/{edge_id}")
def delete_workflow_edge(
    workflow_id: str, version: int, edge_id: str, db: Session = Depends(get_db)
):
    """Delete an edge from a DRAFT workflow version."""
    service = WorkflowDefinitionService(db)

    try:
        service.delete_edge(workflow_id, version, edge_id)
        return {"status": "deleted"}
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workflows/{workflow_id}/versions/{version}/graph")
def get_workflow_graph(workflow_id: str, version: int, db: Session = Depends(get_db)):
    """Get the complete graph structure (nodes + edges) for a workflow version."""
    service = WorkflowDefinitionService(db)
    wv = service.get_workflow_version(workflow_id, version)
    if not wv:
        raise HTTPException(status_code=404, detail="Workflow version not found")

    nodes = [
        {
            "id": n.id,
            "key": n.node_key,
            "type": n.node_type,
            "config": n.config,
            "max_iterations": n.max_iterations,
        }
        for n in wv.nodes
    ]

    edges = [
        {
            "id": e.id,
            "from": e.from_node_id,
            "to": e.to_node_id,
            "condition": e.condition,
        }
        for e in wv.edges
    ]

    return {"version": version, "status": wv.status, "nodes": nodes, "edges": edges}


@router.post("/workflows/{workflow_id}/versions/{version}/runs", response_model=WorkflowRunRead, status_code=201)
def start_workflow_run(workflow_id: str, version: int, body: dict, db: Session = Depends(get_db)):
    """Start execution of an ACTIVE workflow version.

    Body: {"task_run_id": "<task_run_id>"}

    Creates WorkflowRun with initial node runs, schedules entry nodes.

    Returns 400 if version not ACTIVE.
    Returns 201 on success.
    """
    from app.services.workflow_execution_service import WorkflowExecutionService, WorkflowExecutionError

    service = WorkflowExecutionService(db)

    try:
        task_run_id = body.get("task_run_id")
        if not task_run_id:
            raise HTTPException(status_code=400, detail="task_run_id required")

        wv = db.query(WorkflowVersion).filter(
            and_(WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version)
        ).first()
        workflow_version_id = wv.id if wv else None

        if not workflow_version_id:
            raise HTTPException(status_code=404, detail="Workflow version not found")

        workflow_run = service.start_workflow_run(workflow_version_id, task_run_id)
        return WorkflowRunRead(
            id=workflow_run.id,
            workflow_version_id=workflow_run.workflow_version_id,
            task_run_id=workflow_run.task_run_id,
            status=workflow_run.status,
        )
    except WorkflowExecutionError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workflows/{workflow_id}/versions/{version}/runs", response_model=List[WorkflowRunRead])
def list_workflow_runs(workflow_id: str, version: int, db: Session = Depends(get_db)):
    """List all WorkflowRuns for a specific version.

    MA7.2: execution endpoint.
    """
    wv = db.query(WorkflowVersion).filter(
        and_(WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version)
    ).first()

    if not wv:
        raise HTTPException(status_code=404, detail="Workflow version not found")

    runs = db.query(WorkflowRun).filter(WorkflowRun.workflow_version_id == wv.id).all()
    return [
        WorkflowRunRead(
            id=r.id,
            workflow_version_id=r.workflow_version_id,
            task_run_id=r.task_run_id,
            status=r.status,
        )
        for r in runs
    ]


@router.get("/workflow-runs/{workflow_run_id}", response_model=WorkflowRunRead)
def get_workflow_run(workflow_run_id: str, db: Session = Depends(get_db)):
    """Get a workflow run by ID.

    MA7.2: execution endpoint.
    """
    from app.models.workflow import WorkflowRun

    run = db.query(WorkflowRun).filter(WorkflowRun.id == workflow_run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")

    return WorkflowRunRead(
        id=run.id,
        workflow_version_id=run.workflow_version_id,
        task_run_id=run.task_run_id,
        status=run.status,
    )


@router.get("/workflow-runs/{workflow_run_id}/nodes", response_model=List[WorkflowNodeRunRead])
def list_workflow_node_runs(workflow_run_id: str, db: Session = Depends(get_db)):
    """List all node runs in a workflow run.

    MA7.2: execution endpoint.
    """
    from app.models.workflow import WorkflowNodeRun

    node_runs = db.query(WorkflowNodeRun).filter(
        WorkflowNodeRun.workflow_run_id == workflow_run_id
    ).all()

    return [
        WorkflowNodeRunRead(
            id=nr.id,
            workflow_run_id=nr.workflow_run_id,
            workflow_node_id=nr.workflow_node_id,
            iteration=nr.iteration,
            status=nr.status,
            agent_run_id=nr.agent_run_id,
        )
        for nr in node_runs
    ]


@router.post("/workflow-runs/{workflow_run_id}/cancel")
def cancel_workflow_run(workflow_run_id: str, db: Session = Depends(get_db)):
    """Cancel a running workflow.

    Cancellation is cooperative: pending nodes marked CANCELLED,
    running agents receive cancellation_requested signal.

    MA7.2: execution endpoint.
    """
    from app.services.workflow_execution_service import WorkflowExecutionService

    service = WorkflowExecutionService(db)
    service.cancel_workflow_run(workflow_run_id, cancelled_by_user_id=None)

    return {"status": "cancellation_requested"}
