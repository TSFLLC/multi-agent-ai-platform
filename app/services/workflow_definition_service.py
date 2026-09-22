"""Workflow Definition Service — MA7.1 DAG Authoring & Validation.

Handles CRUD for Workflow/WorkflowVersion/WorkflowNode/WorkflowEdge.
Enforces lifecycle (DRAFT → PUBLISHED → immutable).
Validates DAG structure and configuration at publish time.
"""

import copy
from typing import Dict, List, Optional
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.enums import VersionStatus, WorkflowNodeType
from app.errors import ConflictError
from app.models.agents import AgentVersion
from app.models.evaluation_definitions import EvaluationDefinitionVersion
from app.models.workflow import Workflow, WorkflowVersion, WorkflowNode, WorkflowEdge
from app.services.base import BaseService
from app.services.workflow_validation_service import DAGValidationError, WorkflowValidator


class WorkflowDefinitionService(BaseService):
    """Workflow authoring: create, read, list, publish workflows."""

    def create_workflow(self, project_id: str, name: str, description: Optional[str] = None) -> Workflow:
        """Create a new workflow with initial DRAFT version."""
        workflow = Workflow(project_id=project_id, name=name)
        self.db.add(workflow)
        self.db.flush()

        # Create initial version
        version = WorkflowVersion(workflow_id=workflow.id, version=1, status=VersionStatus.DRAFT)
        self.db.add(version)
        self.db.commit()

        return workflow

    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        """Fetch workflow by ID."""
        return self.db.query(Workflow).filter(Workflow.id == workflow_id).first()

    def list_workflows(self, project_id: str, status: Optional[VersionStatus] = None) -> List[Workflow]:
        """List workflows in a project, optionally filtered by latest version status."""
        query = self.db.query(Workflow).filter(Workflow.project_id == project_id)
        workflows = query.all()

        if status:
            workflows = [w for w in workflows if w.current_status == status]

        return workflows

    def get_workflow_version(self, workflow_id: str, version: int) -> Optional[WorkflowVersion]:
        """Fetch a specific workflow version."""
        return (
            self.db.query(WorkflowVersion)
            .filter(and_(WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))
            .first()
        )

    def list_workflow_versions(self, workflow_id: str) -> List[WorkflowVersion]:
        """List all versions of a workflow."""
        return (
            self.db.query(WorkflowVersion)
            .filter(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
            .all()
        )

    def get_latest_version(self, workflow_id: str) -> Optional[WorkflowVersion]:
        """Get the latest (highest version number) version of a workflow."""
        return (
            self.db.query(WorkflowVersion)
            .filter(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
            .first()
        )

    def add_node(
        self,
        workflow_id: str,
        version: int,
        node_key: str,
        node_type: WorkflowNodeType,
        config: Optional[dict] = None,
        max_iterations: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
    ) -> WorkflowNode:
        """Add a node to a DRAFT workflow version.

        Raises ValueError if version is not DRAFT or node_key already exists.
        """
        wv = self.get_workflow_version(workflow_id, version)
        if not wv:
            raise ValueError(f"Workflow version {workflow_id}:{version} not found")

        if wv.status != VersionStatus.DRAFT:
            raise ValueError(f"Cannot add nodes to published version {workflow_id}:{version}")

        # Check for duplicate node_key
        existing = (
            self.db.query(WorkflowNode)
            .filter(
                and_(
                    WorkflowNode.workflow_version_id == wv.id,
                    WorkflowNode.node_key == node_key,
                )
            )
            .first()
        )
        if existing:
            raise ValueError(f"Node key '{node_key}' already exists in version {version}")

        node = WorkflowNode(
            workflow_version_id=wv.id,
            node_key=node_key,
            node_type=node_type,
            config=config,
            max_iterations=max_iterations,
            timeout_seconds=timeout_seconds,
        )
        self.db.add(node)
        self.db.commit()

        return node

    def update_node(
        self, workflow_id: str, version: int, node_id: str, config: Optional[dict] = None
    ) -> WorkflowNode:
        """Update node configuration (DRAFT versions only).

        Raises ValueError if version is not DRAFT.
        """
        wv = self.get_workflow_version(workflow_id, version)
        if not wv:
            raise ValueError(f"Workflow version {workflow_id}:{version} not found")

        if wv.status != VersionStatus.DRAFT:
            raise ValueError(f"Cannot update nodes in published version {workflow_id}:{version}")

        node = self.db.query(WorkflowNode).filter(WorkflowNode.id == node_id).first()
        if not node or node.workflow_version_id != wv.id:
            raise ValueError(f"Node {node_id} not found in version {workflow_id}:{version}")

        if config is not None:
            node.config = config

        self.db.commit()
        return node

    def delete_node(self, workflow_id: str, version: int, node_id: str) -> None:
        """Delete a node from a DRAFT workflow version.

        Raises ValueError if version is not DRAFT or edges reference this node.
        """
        wv = self.get_workflow_version(workflow_id, version)
        if not wv:
            raise ValueError(f"Workflow version {workflow_id}:{version} not found")

        if wv.status != VersionStatus.DRAFT:
            raise ValueError(f"Cannot delete nodes from published version {workflow_id}:{version}")

        node = self.db.query(WorkflowNode).filter(WorkflowNode.id == node_id).first()
        if not node or node.workflow_version_id != wv.id:
            raise ValueError(f"Node {node_id} not found in version {workflow_id}:{version}")

        # Check if any edges reference this node
        edge_count = (
            self.db.query(WorkflowEdge)
            .filter(
                and_(
                    WorkflowEdge.workflow_version_id == wv.id,
                    or_(
                        WorkflowEdge.from_node_id == node_id,
                        WorkflowEdge.to_node_id == node_id,
                    ),
                )
            )
            .count()
        )

        if edge_count > 0:
            raise ValueError(f"Cannot delete node {node_id}: edges reference this node")

        self.db.delete(node)
        self.db.commit()

    def add_edge(
        self, workflow_id: str, version: int, from_node_id: str, to_node_id: str, condition: Optional[dict] = None
    ) -> WorkflowEdge:
        """Add an edge between two nodes in a DRAFT workflow version.

        Raises ValueError if version is not DRAFT, nodes not found, or edge already exists.
        """
        wv = self.get_workflow_version(workflow_id, version)
        if not wv:
            raise ValueError(f"Workflow version {workflow_id}:{version} not found")

        if wv.status != VersionStatus.DRAFT:
            raise ValueError(f"Cannot add edges to published version {workflow_id}:{version}")

        # Verify both nodes exist in this version
        from_node = (
            self.db.query(WorkflowNode)
            .filter(and_(WorkflowNode.id == from_node_id, WorkflowNode.workflow_version_id == wv.id))
            .first()
        )
        to_node = (
            self.db.query(WorkflowNode)
            .filter(and_(WorkflowNode.id == to_node_id, WorkflowNode.workflow_version_id == wv.id))
            .first()
        )

        if not from_node:
            raise ValueError(f"From node {from_node_id} not found in version {workflow_id}:{version}")
        if not to_node:
            raise ValueError(f"To node {to_node_id} not found in version {workflow_id}:{version}")

        # Check for self-edge
        if from_node_id == to_node_id:
            raise ValueError("Self-edges are not allowed")

        # Check for duplicate edge
        existing = (
            self.db.query(WorkflowEdge)
            .filter(
                and_(
                    WorkflowEdge.workflow_version_id == wv.id,
                    WorkflowEdge.from_node_id == from_node_id,
                    WorkflowEdge.to_node_id == to_node_id,
                )
            )
            .first()
        )

        if existing:
            raise ValueError(f"Edge {from_node_id} -> {to_node_id} already exists")

        edge = WorkflowEdge(
            workflow_version_id=wv.id,
            from_node_id=from_node_id,
            to_node_id=to_node_id,
            condition=condition,
        )
        self.db.add(edge)
        self.db.commit()

        return edge

    def delete_edge(self, workflow_id: str, version: int, edge_id: str) -> None:
        """Delete an edge from a DRAFT workflow version.

        Raises ValueError if version is not DRAFT.
        """
        wv = self.get_workflow_version(workflow_id, version)
        if not wv:
            raise ValueError(f"Workflow version {workflow_id}:{version} not found")

        if wv.status != VersionStatus.DRAFT:
            raise ValueError(f"Cannot delete edges from published version {workflow_id}:{version}")

        edge = self.db.query(WorkflowEdge).filter(WorkflowEdge.id == edge_id).first()
        if not edge or edge.workflow_version_id != wv.id:
            raise ValueError(f"Edge {edge_id} not found in version {workflow_id}:{version}")

        self.db.delete(edge)
        self.db.commit()

    def publish_version(self, workflow_id: str, version: int) -> WorkflowVersion:
        """Publish a DRAFT workflow version after validation.

        Runs WorkflowValidator to ensure DAG is valid.
        Transitions status from DRAFT to ACTIVE.
        Sets published_at timestamp.

        Raises ValueError if version is not DRAFT or validation fails.
        """
        wv = self.get_workflow_version(workflow_id, version)
        if not wv:
            raise ValueError(f"Workflow version {workflow_id}:{version} not found")

        if wv.status != VersionStatus.DRAFT:
            raise ValueError(f"Version {workflow_id}:{version} is already {wv.status}, cannot publish")

        # Get the parent workflow to verify project
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")

        # Validate the DAG
        validator = WorkflowValidator(self.db)
        validator.validate(wv, workflow.project_id)

        # Transition to ACTIVE
        wv.status = VersionStatus.ACTIVE
        wv.published_at = wv.published_at or __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )

        # Update workflow's current_status to ACTIVE
        workflow.current_status = VersionStatus.ACTIVE

        self.db.commit()

        return wv

    def validate_version(self, workflow_id: str, version: int) -> List[str]:
        """Dry-run of the publish-time validation (MA7.6A): runs the SAME
        ``WorkflowValidator`` publish uses and returns its issues (empty when
        the DRAFT version is valid). Changes nothing -- never publishes.

        Raises ValueError if the version does not exist or is not DRAFT."""
        wv = self.get_workflow_version(workflow_id, version)
        if not wv:
            raise ValueError(f"Workflow version {workflow_id}:{version} not found")
        if wv.status != VersionStatus.DRAFT:
            raise ValueError(f"Only DRAFT versions can be validated; {workflow_id}:{version} is {wv.status.value}")
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")
        try:
            WorkflowValidator(self.db).validate(wv, workflow.project_id)
        except DAGValidationError as exc:
            return list(exc.issues)
        return []

    def clone_version(self, workflow_id: str, source_version: int) -> WorkflowVersion:
        """Creates a new DRAFT version of the same Workflow from an existing
        version (MA7.6A "edit as new version"). Atomic: the new version, every
        node copy and every edge copy commit together or not at all.

        * The version number is the next one after the highest existing.
        * Node definitions (key, type, config, max_iterations, timeout) are
          copied; edges are copied with their endpoints remapped to the new
          node ids, so the graph is structurally identical.
        * The source version is never modified (an ACTIVE version stays
          immutable) and no WorkflowRun is re-bound.

        Raises ValueError if the source version does not exist and
        ConflictError if a concurrent clone claimed the same version number."""
        source = self.get_workflow_version(workflow_id, source_version)
        if not source:
            raise ValueError(f"Workflow version {workflow_id}:{source_version} not found")
        latest = self.get_latest_version(workflow_id)
        next_version = (latest.version + 1) if latest else 1
        try:
            new_version = WorkflowVersion(workflow_id=workflow_id, version=next_version, status=VersionStatus.DRAFT)
            self.db.add(new_version)
            self.db.flush()

            id_map: Dict[str, str] = {}
            for node in sorted(source.nodes, key=lambda n: n.node_key):
                node_copy = WorkflowNode(
                    workflow_version_id=new_version.id,
                    node_key=node.node_key,
                    node_type=node.node_type,
                    config=copy.deepcopy(node.config),
                    max_iterations=node.max_iterations,
                    timeout_seconds=node.timeout_seconds,
                )
                self.db.add(node_copy)
                self.db.flush()
                id_map[node.id] = node_copy.id

            for edge in sorted(source.edges, key=lambda e: (id_map.get(e.from_node_id, ""), id_map.get(e.to_node_id, ""))):
                if edge.from_node_id not in id_map or edge.to_node_id not in id_map:
                    raise ValueError(f"Edge {edge.id} references a node outside version {source_version}")
                self.db.add(
                    WorkflowEdge(
                        workflow_version_id=new_version.id,
                        from_node_id=id_map[edge.from_node_id],
                        to_node_id=id_map[edge.to_node_id],
                        condition=copy.deepcopy(edge.condition),
                    )
                )
            self.db.flush()
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError(
                f"Version {next_version} of workflow {workflow_id} was created concurrently; retry the copy."
            ) from exc
        except Exception:
            self.db.rollback()
            raise
        return new_version

    def deprecate_version(self, workflow_id: str, version: int) -> WorkflowVersion:
        """Deprecate an ACTIVE version (ACTIVE → DEPRECATED).

        Raises ValueError if version is not ACTIVE.
        """
        wv = self.get_workflow_version(workflow_id, version)
        if not wv:
            raise ValueError(f"Workflow version {workflow_id}:{version} not found")

        if wv.status != VersionStatus.ACTIVE:
            raise ValueError(f"Can only deprecate ACTIVE versions; {workflow_id}:{version} is {wv.status}")

        wv.status = VersionStatus.DEPRECATED
        self.db.commit()

        return wv

    def retire_version(self, workflow_id: str, version: int) -> WorkflowVersion:
        """Retire a DEPRECATED version (DEPRECATED → RETIRED).

        Raises ValueError if version is not DEPRECATED.
        """
        wv = self.get_workflow_version(workflow_id, version)
        if not wv:
            raise ValueError(f"Workflow version {workflow_id}:{version} not found")

        if wv.status != VersionStatus.DEPRECATED:
            raise ValueError(f"Can only retire DEPRECATED versions; {workflow_id}:{version} is {wv.status}")

        wv.status = VersionStatus.RETIRED
        self.db.commit()

        return wv


# Fix missing import
from sqlalchemy import or_
