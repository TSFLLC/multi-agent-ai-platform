"""Workflow / DAG resource boundary — Section 25.2, 25.5.

MA7.1: Workflow authoring, versioning, DAG validation, publish.
No execution (WorkflowRun/WorkflowNodeRun execution is MA7.2+).
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.authz import ProjectAction, check_project_access
from app.db.enums import ApprovalScope, VersionStatus
from app.errors import NotFoundError
from app.models.governance import Approval
from app.models.identity import User
from app.models.workflow import Workflow, WorkflowNodeRun, WorkflowRun, WorkflowVersion
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowNodeCreate,
    WorkflowNodeRunRead,
    WorkflowRead,
    WorkflowRunRead,
    WorkflowVersionRead,
)
from app.services.approval_service import WORKFLOW_HUMAN_APPROVAL_OPERATION
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_validation_service import DAGValidationError

router = APIRouter(tags=["workflows"])


def _get_workflow_version_or_404(db: Session, workflow_id: str, version: int) -> WorkflowVersion:
    workflow_version = db.query(WorkflowVersion).filter(
        and_(WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version)
    ).first()
    if workflow_version is None:
        raise NotFoundError("Workflow version not found")
    return workflow_version


def _workflow_project_id(db: Session, workflow_version: WorkflowVersion) -> str:
    workflow = db.get(Workflow, workflow_version.workflow_id)
    if workflow is None:
        raise NotFoundError("Workflow not found")
    return workflow.project_id


def _get_workflow_or_404(db: Session, workflow_id: str) -> Workflow:
    workflow = db.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError("Workflow not found")
    return workflow


def _require_workflow_access(db: Session, user: User, workflow_id: str, action: ProjectAction) -> Workflow:
    """Resolve the Workflow, then authorize against its owning project --
    same resource-then-check_project_access order used throughout this
    router and app.api.routers.tasks."""
    workflow = _get_workflow_or_404(db, workflow_id)
    check_project_access(db, user=user, project_id=workflow.project_id, action=action)
    return workflow


def _get_workflow_run_or_404(db: Session, workflow_run_id: str) -> WorkflowRun:
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise NotFoundError("Workflow run not found")
    return workflow_run


def _require_workflow_run_access(
    db: Session, user: User, workflow_run: WorkflowRun, action: ProjectAction
) -> None:
    workflow_version = db.get(WorkflowVersion, workflow_run.workflow_version_id)
    if workflow_version is None:
        raise NotFoundError("Workflow version not found")
    check_project_access(
        db,
        user=user,
        project_id=_workflow_project_id(db, workflow_version),
        action=action,
    )


@router.get("/workflows", response_model=List[WorkflowRead])
def list_workflows(
    project_id: str,
    status: Optional[VersionStatus] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List workflows in a project, optionally filtered by status."""
    check_project_access(db, user=user, project_id=project_id, action=ProjectAction.READ)
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
def create_workflow(
    body: WorkflowCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Create a new workflow with initial DRAFT version."""
    check_project_access(db, user=user, project_id=body.project_id, action=ProjectAction.MODIFY)
    service = WorkflowDefinitionService(db)
    workflow = service.create_workflow(body.project_id, body.name)
    return WorkflowRead(
        id=workflow.id,
        project_id=workflow.project_id,
        name=workflow.name,
        current_status=workflow.current_status,
    )


@router.get("/workflows/{workflow_id}", response_model=WorkflowRead)
def get_workflow(
    workflow_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Get a workflow by ID."""
    _require_workflow_access(db, user, workflow_id, ProjectAction.READ)
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
def create_workflow_version(
    workflow_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Create a new DRAFT version for a workflow."""
    _require_workflow_access(db, user, workflow_id, ProjectAction.MODIFY)
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
def publish_workflow_version(
    workflow_id: str, version: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Publish a DRAFT workflow version after DAG validation.

    Validates:
    - DAG structure (no cycles, reachability, valid entry/terminal)
    - Node type constraints (max_iterations, required config fields)
    - Referenced agent/evaluation versions exist and are in same project
    - Node configuration is well-formed

    Raises 400 if validation fails with detailed issues.
    """
    _require_workflow_access(db, user, workflow_id, ProjectAction.MODIFY)
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
def get_workflow_version(
    workflow_id: str, version: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Get a specific workflow version."""
    _require_workflow_access(db, user, workflow_id, ProjectAction.READ)
    service = WorkflowDefinitionService(db)
    wv = service.get_workflow_version(workflow_id, version)
    if not wv:
        raise HTTPException(status_code=404, detail="Workflow version not found")
    return WorkflowVersionRead(id=wv.id, workflow_id=workflow_id, version=version, status=wv.status)


@router.get("/workflows/{workflow_id}/versions", response_model=List[WorkflowVersionRead])
def list_workflow_versions(
    workflow_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """List all versions of a workflow."""
    _require_workflow_access(db, user, workflow_id, ProjectAction.READ)
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
    user: User = Depends(get_current_user),
):
    """Add a node to a DRAFT workflow version."""
    _require_workflow_access(db, user, workflow_id, ProjectAction.MODIFY)
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
    workflow_id: str,
    version: int,
    node_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a node from a DRAFT workflow version."""
    _require_workflow_access(db, user, workflow_id, ProjectAction.MODIFY)
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
    user: User = Depends(get_current_user),
):
    """Add an edge between two nodes in a DRAFT workflow version."""
    _require_workflow_access(db, user, workflow_id, ProjectAction.MODIFY)
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
    workflow_id: str,
    version: int,
    edge_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete an edge from a DRAFT workflow version."""
    _require_workflow_access(db, user, workflow_id, ProjectAction.MODIFY)
    service = WorkflowDefinitionService(db)

    try:
        service.delete_edge(workflow_id, version, edge_id)
        return {"status": "deleted"}
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workflows/{workflow_id}/versions/{version}/graph")
def get_workflow_graph(
    workflow_id: str, version: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Get the complete graph structure (nodes + edges) for a workflow version."""
    _require_workflow_access(db, user, workflow_id, ProjectAction.READ)
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
def start_workflow_run(
    workflow_id: str,
    version: int,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Start execution of an ACTIVE workflow version.

    Body: {"task_run_id": "<task_run_id>"}

    Creates WorkflowRun with initial node runs, schedules entry nodes.

    Returns 400 if version not ACTIVE.
    Returns 201 on success.
    """
    from app.services.workflow_execution_service import WorkflowExecutionError, WorkflowExecutionService

    service = WorkflowExecutionService(db)

    try:
        task_run_id = body.get("task_run_id")
        if not task_run_id:
            raise HTTPException(status_code=400, detail="task_run_id required")

        wv = _get_workflow_version_or_404(db, workflow_id, version)
        check_project_access(
            db,
            user=user,
            project_id=_workflow_project_id(db, wv),
            action=ProjectAction.MODIFY,
        )

        workflow_run = service.start_workflow_run(wv.id, task_run_id)
        return WorkflowRunRead(
            id=workflow_run.id,
            workflow_version_id=workflow_run.workflow_version_id,
            task_run_id=workflow_run.task_run_id,
            status=workflow_run.status,
        )
    except WorkflowExecutionError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workflows/{workflow_id}/versions/{version}/runs", response_model=List[WorkflowRunRead])
def list_workflow_runs(
    workflow_id: str,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all WorkflowRuns for a specific version.

    MA7.2: execution endpoint.
    """
    wv = _get_workflow_version_or_404(db, workflow_id, version)
    check_project_access(
        db,
        user=user,
        project_id=_workflow_project_id(db, wv),
        action=ProjectAction.READ,
    )

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
def get_workflow_run(
    workflow_run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get a workflow run by ID.

    MA7.2: execution endpoint.
    """
    run = _get_workflow_run_or_404(db, workflow_run_id)
    _require_workflow_run_access(db, user, run, ProjectAction.READ)

    return WorkflowRunRead(
        id=run.id,
        workflow_version_id=run.workflow_version_id,
        task_run_id=run.task_run_id,
        status=run.status,
    )


@router.get("/workflow-runs/{workflow_run_id}/nodes", response_model=List[WorkflowNodeRunRead])
def list_workflow_node_runs(
    workflow_run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all node runs in a workflow run.

    MA7.2: execution endpoint.
    """
    workflow_run = _get_workflow_run_or_404(db, workflow_run_id)
    _require_workflow_run_access(db, user, workflow_run, ProjectAction.READ)
    node_runs = db.query(WorkflowNodeRun).filter(
        WorkflowNodeRun.workflow_run_id == workflow_run_id
    ).all()

    approval_ids: Dict[str, str] = {
        scope_ref_id: approval_id
        for scope_ref_id, approval_id in db.execute(
            select(Approval.scope_ref_id, Approval.id).where(
                Approval.scope == ApprovalScope.WORKFLOW_NODE_RUN,
                Approval.operation_type == WORKFLOW_HUMAN_APPROVAL_OPERATION,
                Approval.scope_ref_id.in_([nr.id for nr in node_runs]),
            )
        ).all()
    }

    return [
        WorkflowNodeRunRead(
            id=nr.id,
            workflow_run_id=nr.workflow_run_id,
            workflow_node_id=nr.workflow_node_id,
            iteration=nr.iteration,
            status=nr.status,
            agent_run_id=nr.agent_run_id,
            approval_id=approval_ids.get(nr.id),
        )
        for nr in node_runs
    ]


@router.get("/workflow-runs/{workflow_run_id}/nodes/{node_id}/attempts", response_model=List[WorkflowNodeRunRead])
def get_workflow_node_run_attempts(
    workflow_run_id: str,
    node_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get per-iteration node runs for a repair-loop node.

    MA7.2 preserves the MA7.1 contract placeholder; repair-loop execution
    remains outside this phase.
    """
    workflow_run = _get_workflow_run_or_404(db, workflow_run_id)
    _require_workflow_run_access(db, user, workflow_run, ProjectAction.READ)
    raise HTTPException(status_code=501, detail="Workflow execution (MA7.2+)")


@router.post("/workflow-runs/{workflow_run_id}/cancel")
def cancel_workflow_run(
    workflow_run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Cancel a running workflow.

    Cancellation is cooperative: pending nodes marked CANCELLED,
    running agents receive cancellation_requested signal.

    MA7.2: execution endpoint.
    """
    from app.services.workflow_execution_service import WorkflowExecutionService

    workflow_run = _get_workflow_run_or_404(db, workflow_run_id)
    _require_workflow_run_access(db, user, workflow_run, ProjectAction.MODIFY)

    service = WorkflowExecutionService(db)
    service.cancel_workflow_run(workflow_run_id, cancelled_by_user_id=user.id)

    return {"status": "cancellation_requested"}
