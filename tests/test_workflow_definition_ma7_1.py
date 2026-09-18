"""MA7.1 Workflow Definition CRUD, validation, and lifecycle tests.

Tests cover:
- Workflow creation and listing
- Version creation and history
- DRAFT mutation and published immutability
- DAG structure validation (cycles, reachability)
- Node type constraints
- Referenced entity validation (agents, evaluations)
- Project isolation
- Lifecycle transitions (DRAFT → ACTIVE → DEPRECATED → RETIRED)
"""

import pytest
from sqlalchemy.orm import Session

from app.db.enums import VersionStatus, WorkflowNodeType
from app.models.agents import Agent, AgentVersion
from app.models.evaluation_definitions import EvaluationDefinition, EvaluationDefinitionVersion
from app.models.workflow import Workflow, WorkflowVersion, WorkflowNode, WorkflowEdge
from app.services.workflow_definition_service import WorkflowDefinitionService
from app.services.workflow_validation_service import DAGValidationError, WorkflowValidator
from tests.conftest import make_project, make_agent, make_agent_version


class TestWorkflowCRUD:
    """Test workflow creation, reading, listing."""

    def test_create_workflow(self, db: Session, project_id: str):
        """Create a new workflow."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test Workflow")

        assert workflow.id is not None
        assert workflow.project_id == project_id
        assert workflow.name == "Test Workflow"
        assert workflow.current_status is None

        # Should have initial DRAFT version
        versions = service.list_workflow_versions(workflow.id)
        assert len(versions) == 1
        assert versions[0].version == 1
        assert versions[0].status == VersionStatus.DRAFT

    def test_get_workflow(self, db: Session, project_id: str):
        """Fetch a workflow by ID."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")

        fetched = service.get_workflow(workflow.id)
        assert fetched.id == workflow.id
        assert fetched.name == "Test"

    def test_list_workflows(self, db: Session, project_id: str):
        """List workflows in a project."""
        service = WorkflowDefinitionService(db)
        w1 = service.create_workflow(project_id, "Workflow 1")
        w2 = service.create_workflow(project_id, "Workflow 2")

        workflows = service.list_workflows(project_id)
        assert len(workflows) >= 2
        assert any(w.id == w1.id for w in workflows)
        assert any(w.id == w2.id for w in workflows)

    def test_get_latest_version(self, db: Session, project_id: str):
        """Get the latest version of a workflow."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")

        latest = service.get_latest_version(workflow.id)
        assert latest.version == 1

        # Create another version
        new_version = WorkflowVersion(
            workflow_id=workflow.id, version=2, status=VersionStatus.DRAFT
        )
        db.add(new_version)
        db.commit()

        latest = service.get_latest_version(workflow.id)
        assert latest.version == 2


class TestWorkflowNodeManagement:
    """Test node addition, update, deletion."""

    def test_add_node_to_draft(self, db: Session, project_id: str):
        """Add a node to a DRAFT version."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node = service.add_node(
            workflow.id, latest.version, "entry", WorkflowNodeType.AGENT, config={"agent_version_id": "test"}
        )

        assert node.id is not None
        assert node.node_key == "entry"
        assert node.node_type == WorkflowNodeType.AGENT

    def test_cannot_add_node_to_published(self, db: Session, project_id: str, agent_version_id: str):
        """Cannot add nodes to an ACTIVE version."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        # Add a node and publish
        service.add_node(
            workflow.id, latest.version, "entry", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        service.publish_version(workflow.id, latest.version)

        # Try to add another node
        with pytest.raises(ValueError, match="Cannot add nodes to published"):
            service.add_node(
                workflow.id, latest.version, "another", WorkflowNodeType.AGENT,
                config={"agent_version_id": agent_version_id}
            )

    def test_duplicate_node_key_rejected(self, db: Session, project_id: str):
        """Cannot create nodes with duplicate keys in same version."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        service.add_node(workflow.id, latest.version, "node1", WorkflowNodeType.AGENT)

        with pytest.raises(ValueError, match="already exists"):
            service.add_node(workflow.id, latest.version, "node1", WorkflowNodeType.AGENT)

    def test_delete_node(self, db: Session, project_id: str):
        """Delete a node from DRAFT version."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node = service.add_node(workflow.id, latest.version, "node1", WorkflowNodeType.AGENT)
        service.delete_node(workflow.id, latest.version, node.id)

        # Verify deletion
        deleted = db.query(WorkflowNode).filter(WorkflowNode.id == node.id).first()
        assert deleted is None

    def test_cannot_delete_node_with_edges(self, db: Session, project_id: str):
        """Cannot delete a node if edges reference it."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node1 = service.add_node(workflow.id, latest.version, "node1", WorkflowNodeType.AGENT)
        node2 = service.add_node(workflow.id, latest.version, "node2", WorkflowNodeType.AGENT)
        service.add_edge(workflow.id, latest.version, node1.id, node2.id)

        with pytest.raises(ValueError, match="edges reference"):
            service.delete_node(workflow.id, latest.version, node1.id)


class TestWorkflowEdgeManagement:
    """Test edge addition and deletion."""

    def test_add_edge(self, db: Session, project_id: str):
        """Add an edge between two nodes."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node1 = service.add_node(workflow.id, latest.version, "node1", WorkflowNodeType.AGENT)
        node2 = service.add_node(workflow.id, latest.version, "node2", WorkflowNodeType.AGENT)

        edge = service.add_edge(workflow.id, latest.version, node1.id, node2.id)
        assert edge.from_node_id == node1.id
        assert edge.to_node_id == node2.id

    def test_self_edge_rejected(self, db: Session, project_id: str):
        """Self-edges are not allowed."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node = service.add_node(workflow.id, latest.version, "node1", WorkflowNodeType.AGENT)

        with pytest.raises(ValueError, match="Self-edges"):
            service.add_edge(workflow.id, latest.version, node.id, node.id)

    def test_duplicate_edge_rejected(self, db: Session, project_id: str):
        """Duplicate edges between same nodes are not allowed."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node1 = service.add_node(workflow.id, latest.version, "node1", WorkflowNodeType.AGENT)
        node2 = service.add_node(workflow.id, latest.version, "node2", WorkflowNodeType.AGENT)

        service.add_edge(workflow.id, latest.version, node1.id, node2.id)

        with pytest.raises(ValueError, match="already exists"):
            service.add_edge(workflow.id, latest.version, node1.id, node2.id)

    def test_delete_edge(self, db: Session, project_id: str):
        """Delete an edge from DRAFT version."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node1 = service.add_node(workflow.id, latest.version, "node1", WorkflowNodeType.AGENT)
        node2 = service.add_node(workflow.id, latest.version, "node2", WorkflowNodeType.AGENT)
        edge = service.add_edge(workflow.id, latest.version, node1.id, node2.id)

        service.delete_edge(workflow.id, latest.version, edge.id)

        deleted = db.query(WorkflowEdge).filter(WorkflowEdge.id == edge.id).first()
        assert deleted is None


class TestDAGValidation:
    """Test DAG structure validation."""

    def test_empty_workflow_rejected(self, db: Session, project_id: str):
        """Cannot publish a workflow with no nodes."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        with pytest.raises(DAGValidationError) as exc:
            service.publish_version(workflow.id, latest.version)
        assert "no nodes" in str(exc.value)

    def test_cycle_detection(self, db: Session, project_id: str, agent_version_id: str):
        """Cycles in the DAG are rejected."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        n1 = service.add_node(
            workflow.id, latest.version, "n1", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        n2 = service.add_node(
            workflow.id, latest.version, "n2", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        n3 = service.add_node(
            workflow.id, latest.version, "n3", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )

        service.add_edge(workflow.id, latest.version, n1.id, n2.id)
        service.add_edge(workflow.id, latest.version, n2.id, n3.id)
        service.add_edge(workflow.id, latest.version, n3.id, n1.id)  # Creates cycle

        with pytest.raises(DAGValidationError) as exc:
            service.publish_version(workflow.id, latest.version)
        assert "Cycle" in str(exc.value)

    def test_unreachable_node_rejected(self, db: Session, project_id: str, agent_version_id: str):
        """Nodes unreachable from entry are rejected."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        n1 = service.add_node(
            workflow.id, latest.version, "n1", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        n2 = service.add_node(
            workflow.id, latest.version, "n2", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        n3 = service.add_node(
            workflow.id, latest.version, "n3", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )

        # n1 → n2, but n3 is isolated
        service.add_edge(workflow.id, latest.version, n1.id, n2.id)

        with pytest.raises(DAGValidationError) as exc:
            service.publish_version(workflow.id, latest.version)
        assert "Unreachable" in str(exc.value) or "no path to terminal" in str(exc.value) or "not connected" in str(exc.value)

    def test_valid_linear_dag_publishes(self, db: Session, project_id: str, agent_version_id: str):
        """A simple linear DAG publishes successfully."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        n1 = service.add_node(
            workflow.id, latest.version, "n1", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        n2 = service.add_node(
            workflow.id, latest.version, "n2", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )

        service.add_edge(workflow.id, latest.version, n1.id, n2.id)

        published = service.publish_version(workflow.id, latest.version)
        assert published.status == VersionStatus.ACTIVE
        assert published.published_at is not None

    def test_valid_fan_out_fan_in_dag_publishes(self, db: Session, project_id: str, agent_version_id: str):
        """A DAG with fan-out/fan-in publishes successfully."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        # Create fan-out/fan-in pattern:
        # n1 → n2, n3 → n4
        n1 = service.add_node(
            workflow.id, latest.version, "n1", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        n2 = service.add_node(
            workflow.id, latest.version, "n2", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        n3 = service.add_node(
            workflow.id, latest.version, "n3", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        n4 = service.add_node(
            workflow.id, latest.version, "n4", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )

        service.add_edge(workflow.id, latest.version, n1.id, n2.id)
        service.add_edge(workflow.id, latest.version, n1.id, n3.id)
        service.add_edge(workflow.id, latest.version, n2.id, n4.id)
        service.add_edge(workflow.id, latest.version, n3.id, n4.id)

        published = service.publish_version(workflow.id, latest.version)
        assert published.status == VersionStatus.ACTIVE


class TestNodeTypeConstraints:
    """Test node-type-specific validation."""

    def test_repair_loop_requires_max_iterations(self, db: Session, project_id: str):
        """REPAIR_LOOP nodes must have max_iterations (enforced at DB level)."""
        from sqlalchemy.exc import IntegrityError

        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        # DB CHECK constraint prevents creating REPAIR_LOOP without max_iterations
        with pytest.raises(IntegrityError):
            node = service.add_node(
                workflow.id, latest.version, "loop", WorkflowNodeType.REPAIR_LOOP
                # No max_iterations - violates CHECK constraint
            )

    def test_repair_loop_with_max_iterations_passes(self, db: Session, project_id: str):
        """REPAIR_LOOP with max_iterations is valid."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node = service.add_node(
            workflow.id, latest.version, "loop", WorkflowNodeType.REPAIR_LOOP,
            max_iterations=3
        )

        published = service.publish_version(workflow.id, latest.version)
        assert published.status == VersionStatus.ACTIVE

    def test_agent_requires_agent_version_id(self, db: Session, project_id: str):
        """AGENT nodes must have config.agent_version_id."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node = service.add_node(
            workflow.id, latest.version, "agent", WorkflowNodeType.AGENT
            # No config
        )

        with pytest.raises(DAGValidationError) as exc:
            service.publish_version(workflow.id, latest.version)
        assert "agent_version_id" in str(exc.value)

    def test_human_approval_requires_approval_group(self, db: Session, project_id: str):
        """HUMAN_APPROVAL nodes must have config.approval_group."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node = service.add_node(
            workflow.id, latest.version, "approval", WorkflowNodeType.HUMAN_APPROVAL
            # No config
        )

        with pytest.raises(DAGValidationError) as exc:
            service.publish_version(workflow.id, latest.version)
        assert "approval_group" in str(exc.value)


class TestReferencedEntityValidation:
    """Test validation of referenced agents and evaluations."""

    def test_invalid_agent_version_rejected(self, db: Session, project_id: str):
        """Publishing with invalid agent_version_id is rejected."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        node = service.add_node(
            workflow.id, latest.version, "agent", WorkflowNodeType.AGENT,
            config={"agent_version_id": "nonexistent"}
        )

        with pytest.raises(DAGValidationError) as exc:
            service.publish_version(workflow.id, latest.version)
        assert "unknown agent version" in str(exc.value)

    def test_cross_project_agent_rejected(self, db: Session):
        """Publishing with agent from different project is rejected."""
        service = WorkflowDefinitionService(db)

        # Create agents in different projects
        proj1 = make_project(db, name="proj1")
        proj2 = make_project(db, name="proj2")

        agent = make_agent(db, project=proj2, name="agent")
        version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)

        workflow = service.create_workflow(proj1.id, "Test")
        latest = service.get_latest_version(workflow.id)

        node = service.add_node(
            workflow.id, latest.version, "agent", WorkflowNodeType.AGENT,
            config={"agent_version_id": version.id}
        )

        with pytest.raises(DAGValidationError) as exc:
            service.publish_version(workflow.id, latest.version)
        assert "different project" in str(exc.value)


class TestVersionLifecycle:
    """Test version status transitions."""

    def test_draft_to_published_transition(self, db: Session, project_id: str, agent_version_id: str):
        """DRAFT versions can transition to PUBLISHED."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        service.add_node(
            workflow.id, latest.version, "agent", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )

        published = service.publish_version(workflow.id, latest.version)
        assert published.status == VersionStatus.ACTIVE

    def test_published_version_is_immutable(self, db: Session, project_id: str, agent_version_id: str):
        """Cannot mutate a PUBLISHED version."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        service.add_node(
            workflow.id, latest.version, "agent", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        service.publish_version(workflow.id, latest.version)

        with pytest.raises(ValueError, match="Cannot add nodes to published"):
            service.add_node(workflow.id, latest.version, "another", WorkflowNodeType.AGENT)

    def test_deprecate_published_version(self, db: Session, project_id: str, agent_version_id: str):
        """PUBLISHED versions can transition to DEPRECATED."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        service.add_node(
            workflow.id, latest.version, "agent", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        service.publish_version(workflow.id, latest.version)

        deprecated = service.deprecate_version(workflow.id, latest.version)
        assert deprecated.status == VersionStatus.DEPRECATED

    def test_retire_deprecated_version(self, db: Session, project_id: str, agent_version_id: str):
        """DEPRECATED versions can transition to RETIRED."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        latest = service.get_latest_version(workflow.id)

        service.add_node(
            workflow.id, latest.version, "agent", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        service.publish_version(workflow.id, latest.version)
        service.deprecate_version(workflow.id, latest.version)

        retired = service.retire_version(workflow.id, latest.version)
        assert retired.status == VersionStatus.RETIRED


class TestWorkflowVersionHistory:
    """Test version history and independent versions."""

    def test_multiple_versions_independent(self, db: Session, project_id: str, agent_version_id: str):
        """Multiple versions of same workflow are independent."""
        service = WorkflowDefinitionService(db)
        workflow = service.create_workflow(project_id, "Test")
        v1 = service.get_latest_version(workflow.id)

        # Add node to v1 and publish
        service.add_node(
            workflow.id, v1.version, "n1", WorkflowNodeType.AGENT,
            config={"agent_version_id": agent_version_id}
        )
        service.publish_version(workflow.id, v1.version)

        # Create v2
        new_version = WorkflowVersion(
            workflow_id=workflow.id, version=2, status=VersionStatus.DRAFT
        )
        db.add(new_version)
        db.commit()

        # v2 should be empty
        v2_nodes = db.query(WorkflowNode).filter(
            WorkflowNode.workflow_version_id == new_version.id
        ).all()
        assert len(v2_nodes) == 0

        # v1 should still have its node
        v1_nodes = db.query(WorkflowNode).filter(
            WorkflowNode.workflow_version_id == v1.id
        ).all()
        assert len(v1_nodes) == 1


# Fixtures
@pytest.fixture
def project_id(db) -> str:
    """Create a test project."""
    project = make_project(db)
    db.commit()
    return project.id


@pytest.fixture
def agent_version_id(db, project_id: str) -> str:
    """Create a test agent version."""
    from app.models.identity import Project
    project = db.query(Project).filter(Project.id == project_id).first()
    agent = make_agent(db, project=project)
    version = make_agent_version(db, agent=agent, status=VersionStatus.ACTIVE)
    db.commit()
    return version.id
