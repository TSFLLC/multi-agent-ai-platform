"""Workflow / DAG resource boundary — Section 25.2, 25.5."""

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowNodeRunRead,
    WorkflowRead,
    WorkflowRunRead,
    WorkflowVersionRead,
)

router = APIRouter(tags=["workflows"])


@router.get("/workflows", response_model=List[WorkflowRead])
def list_workflows(db: Session = Depends(get_db)):
    not_implemented()


@router.post("/workflows", response_model=WorkflowRead, status_code=201)
def create_workflow(body: WorkflowCreate, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/workflows/{workflow_id}/versions", response_model=WorkflowVersionRead, status_code=201)
def create_workflow_version(workflow_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/workflows/{workflow_id}/versions/{version}/publish", response_model=WorkflowVersionRead)
def publish_workflow_version(workflow_id: str, version: int, db: Session = Depends(get_db)):
    """The DAG validator (repair_loop max_iterations bounding, cycle
    rejection — Section 16.3) runs here in a later phase; MA0 only reserves
    the endpoint and the DB-level CHECK constraint backstop
    (ck_workflow_nodes_repair_loop_requires_max_iterations)."""
    not_implemented()


@router.get("/workflows/{workflow_id}/versions/{version}/graph")
def get_workflow_graph(workflow_id: str, version: int, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/workflows/{workflow_id}/versions/{version}/runs", response_model=List[WorkflowRunRead])
def list_workflow_runs(workflow_id: str, version: int, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/workflow-runs/{workflow_run_id}", response_model=WorkflowRunRead)
def get_workflow_run(workflow_run_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/workflow-runs/{workflow_run_id}/nodes", response_model=List[WorkflowNodeRunRead])
def list_workflow_node_runs(workflow_run_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.get(
    "/workflow-runs/{workflow_run_id}/nodes/{node_id}/attempts", response_model=List[WorkflowNodeRunRead]
)
def get_workflow_node_run_attempts(workflow_run_id: str, node_id: str, db: Session = Depends(get_db)):
    """Per-iteration workflow_node_runs rows for a repair-loop node
    (Section 25.5)."""
    not_implemented()
