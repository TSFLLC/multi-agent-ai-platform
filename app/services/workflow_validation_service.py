"""Workflow DAG Validation — MA7.1 Publish-Time Validation.

Validates DAG structure and configuration before publishing a WorkflowVersion.
Enforces Section 16 constraints: cycles, reachability, node type constraints,
project isolation, referenced agent/evaluation versions.
"""

from typing import Dict, List, Optional, Set, Any
from sqlalchemy.orm import Session

from app.db.enums import WorkflowNodeType
from app.models.agents import Agent, AgentVersion
from app.models.evaluation_definitions import EvaluationDefinition, EvaluationDefinitionVersion
from app.models.workflow import WorkflowVersion, WorkflowNode, WorkflowEdge


class DAGValidationError(ValueError):
    """Raised when DAG fails validation."""

    def __init__(self, issues: List[str]):
        self.issues = issues
        super().__init__(f"DAG validation failed: {'; '.join(issues)}")


class WorkflowValidator:
    """Validates a WorkflowVersion's DAG structure and configuration."""

    def __init__(self, db: Session):
        self.db = db
        self.issues: List[str] = []

    def validate(self, workflow_version: WorkflowVersion, project_id: str) -> None:
        """Run complete validation suite.

        Raises DAGValidationError if any validation fails.
        """
        self.issues = []

        # Load all nodes and edges for this version
        nodes = self._load_nodes(workflow_version.id)
        edges = self._load_edges(workflow_version.id)

        # Run all validation checks
        self._validate_has_nodes(nodes)
        self._validate_edges_reference_valid_nodes(edges, nodes)
        self._validate_no_self_edges(edges)
        self._validate_no_duplicate_edges(edges)
        self._validate_no_cycles(nodes, edges)
        self._validate_connectivity(nodes, edges)
        self._validate_entry_structure(nodes, edges)
        self._validate_terminal_structure(nodes, edges)
        self._validate_all_nodes_reachable(nodes, edges)
        self._validate_all_paths_reach_terminal(nodes, edges)
        self._validate_node_type_constraints(nodes)
        self._validate_referenced_agents(nodes, project_id)
        self._validate_referenced_evaluations(nodes, project_id)

        if self.issues:
            raise DAGValidationError(self.issues)

    def _load_nodes(self, workflow_version_id: str) -> Dict[str, WorkflowNode]:
        """Load all nodes indexed by ID."""
        nodes = self.db.query(WorkflowNode).filter(WorkflowNode.workflow_version_id == workflow_version_id).all()
        return {node.id: node for node in nodes}

    def _load_edges(self, workflow_version_id: str) -> List[tuple]:
        """Load all edges as (from_id, to_id, edge_obj) tuples."""
        edges = self.db.query(WorkflowEdge).filter(WorkflowEdge.workflow_version_id == workflow_version_id).all()
        return [(edge.from_node_id, edge.to_node_id, edge) for edge in edges]

    def _validate_has_nodes(self, nodes: Dict[str, WorkflowNode]) -> None:
        """Workflow must have at least one node."""
        if not nodes:
            self.issues.append("Workflow has no nodes")

    def _validate_edges_reference_valid_nodes(
        self, edges: List[tuple], nodes: Dict[str, WorkflowNode]
    ) -> None:
        """All edges must reference nodes in the same workflow version."""
        for from_id, to_id, edge in edges:
            if from_id not in nodes:
                self.issues.append(f"Edge references unknown from_node_id: {from_id}")
            if to_id not in nodes:
                self.issues.append(f"Edge references unknown to_node_id: {to_id}")

    def _validate_no_self_edges(self, edges: List[tuple]) -> None:
        """Self-edges are not allowed."""
        for from_id, to_id, edge in edges:
            if from_id == to_id:
                self.issues.append(f"Self-edge not allowed on node {from_id}")

    def _validate_no_duplicate_edges(self, edges: List[tuple]) -> None:
        """No duplicate edges between same nodes."""
        seen = set()
        for from_id, to_id, edge in edges:
            key = (from_id, to_id)
            if key in seen:
                self.issues.append(f"Duplicate edge: {from_id} -> {to_id}")
            seen.add(key)

    def _validate_connectivity(self, nodes: Dict[str, WorkflowNode], edges: List[tuple]) -> None:
        """All nodes must be in a single connected component.

        Detects isolated node clusters or components not connected to the main workflow.
        """
        if len(nodes) <= 1:
            return  # Single node or empty is trivially connected

        # Build undirected adjacency (union of forward and reverse edges)
        adj: Dict[str, Set[str]] = {node_id: set() for node_id in nodes}
        for from_id, to_id, edge in edges:
            adj[from_id].add(to_id)
            adj[to_id].add(from_id)

        # BFS from first node to find connected component
        first_node = next(iter(nodes.keys()))
        visited = set()
        queue = [first_node]

        while queue:
            node_id = queue.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)
            for neighbor in adj[node_id]:
                if neighbor not in visited:
                    queue.append(neighbor)

        # Check if all nodes were visited
        if len(visited) < len(nodes):
            unvisited = set(nodes.keys()) - visited
            for node_id in unvisited:
                self.issues.append(f"Node {nodes[node_id].node_key} is not connected to main workflow")

    def _validate_no_cycles(self, nodes: Dict[str, WorkflowNode], edges: List[tuple]) -> None:
        """Graph must be acyclic (DFS-based cycle detection)."""
        if not nodes:
            return

        # Build adjacency list
        adj: Dict[str, List[str]] = {node_id: [] for node_id in nodes}
        for from_id, to_id, edge in edges:
            adj[from_id].append(to_id)

        # DFS to detect cycles
        visited = set()
        rec_stack = set()

        def has_cycle(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)

            for neighbor in adj[node_id]:
                if neighbor not in visited:
                    if has_cycle(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True

            rec_stack.remove(node_id)
            return False

        for node_id in nodes:
            if node_id not in visited:
                if has_cycle(node_id):
                    self.issues.append("Cycle detected in workflow DAG")
                    return

    def _validate_entry_structure(self, nodes: Dict[str, WorkflowNode], edges: List[tuple]) -> None:
        """Validate entry node(s) structure.

        Entry node(s) are those with no incoming edges.
        At least one entry node must exist (and if there's only one node, it must be an entry).
        """
        if not nodes:
            return

        # Find nodes with incoming edges
        has_incoming = set()
        for from_id, to_id, edge in edges:
            has_incoming.add(to_id)

        # Entry nodes are those without incoming edges
        entry_nodes = [node_id for node_id in nodes if node_id not in has_incoming]

        if not entry_nodes:
            self.issues.append("No entry node found (all nodes have incoming edges)")

        # Single isolated node is valid entry/terminal but needs verification
        # A node with no edges at all (isolated) will be caught by connectivity check below

    def _validate_terminal_structure(self, nodes: Dict[str, WorkflowNode], edges: List[tuple]) -> None:
        """Validate terminal node(s) structure.

        Terminal nodes are those with type=TERMINAL or no outgoing edges.
        At least one terminal node must exist for a valid workflow.
        """
        if not nodes:
            return

        # Find nodes with outgoing edges
        has_outgoing = set()
        for from_id, to_id, edge in edges:
            has_outgoing.add(from_id)

        # Terminal candidates: no outgoing edges OR type=TERMINAL
        terminal_candidates = [
            node_id
            for node_id in nodes
            if node_id not in has_outgoing or nodes[node_id].node_type == WorkflowNodeType.TERMINAL
        ]

        if not terminal_candidates:
            self.issues.append("No terminal node found (all nodes have outgoing edges)")

    def _validate_all_nodes_reachable(self, nodes: Dict[str, WorkflowNode], edges: List[tuple]) -> None:
        """All nodes must be reachable from at least one entry node.

        Detects dead code (unreachable nodes).
        """
        if not nodes:
            return

        # Find entry nodes (no incoming edges)
        has_incoming = set()
        for from_id, to_id, edge in edges:
            has_incoming.add(to_id)

        entry_nodes = [node_id for node_id in nodes if node_id not in has_incoming]

        if not entry_nodes:
            return  # Will be caught by _validate_entry_structure

        # BFS from entry nodes
        adj: Dict[str, List[str]] = {node_id: [] for node_id in nodes}
        for from_id, to_id, edge in edges:
            adj[from_id].append(to_id)

        reachable = set()
        queue = entry_nodes[:]

        while queue:
            node_id = queue.pop(0)
            if node_id in reachable:
                continue
            reachable.add(node_id)
            for neighbor in adj[node_id]:
                if neighbor not in reachable:
                    queue.append(neighbor)

        # Check for unreachable nodes
        unreachable = set(nodes.keys()) - reachable
        for node_id in unreachable:
            self.issues.append(f"Unreachable node: {nodes[node_id].node_key}")

    def _validate_all_paths_reach_terminal(self, nodes: Dict[str, WorkflowNode], edges: List[tuple]) -> None:
        """All nodes must have a path to at least one terminal node.

        Detects incomplete execution paths.
        """
        if not nodes:
            return

        # Build reverse adjacency (to_node -> from_nodes)
        rev_adj: Dict[str, List[str]] = {node_id: [] for node_id in nodes}
        for from_id, to_id, edge in edges:
            rev_adj[to_id].append(from_id)

        # Find terminal nodes
        has_outgoing = set()
        for from_id, to_id, edge in edges:
            has_outgoing.add(from_id)

        terminal_nodes = [
            node_id
            for node_id in nodes
            if node_id not in has_outgoing or nodes[node_id].node_type == WorkflowNodeType.TERMINAL
        ]

        if not terminal_nodes:
            return  # Will be caught by _validate_terminal_structure

        # Reverse BFS from terminals
        reaches_terminal = set()
        queue = terminal_nodes[:]

        while queue:
            node_id = queue.pop(0)
            if node_id in reaches_terminal:
                continue
            reaches_terminal.add(node_id)
            for predecessor in rev_adj[node_id]:
                if predecessor not in reaches_terminal:
                    queue.append(predecessor)

        # Check for nodes that don't reach terminal
        no_path_to_terminal = set(nodes.keys()) - reaches_terminal
        for node_id in no_path_to_terminal:
            self.issues.append(f"Node {nodes[node_id].node_key} has no path to terminal")

    def _validate_node_type_constraints(self, nodes: Dict[str, WorkflowNode]) -> None:
        """Validate node-type-specific constraints.

        - REPAIR_LOOP requires max_iterations NOT NULL
        - AGENT requires config.agent_version_id
        - JUDGE requires config.judge_agent_version_id
        - HUMAN_APPROVAL requires config.approval_group
        """
        for node_id, node in nodes.items():
            if node.node_type == WorkflowNodeType.REPAIR_LOOP:
                if node.max_iterations is None:
                    self.issues.append(f"REPAIR_LOOP node {node.node_key} requires max_iterations")

            elif node.node_type == WorkflowNodeType.AGENT:
                if not node.config or not node.config.get("agent_version_id"):
                    self.issues.append(f"AGENT node {node.node_key} requires config.agent_version_id")

            elif node.node_type == WorkflowNodeType.JUDGE:
                if not node.config or not node.config.get("judge_agent_version_id"):
                    self.issues.append(f"JUDGE node {node.node_key} requires config.judge_agent_version_id")

            elif node.node_type == WorkflowNodeType.HUMAN_APPROVAL:
                if not node.config or not node.config.get("approval_group"):
                    self.issues.append(f"HUMAN_APPROVAL node {node.node_key} requires config.approval_group")

            elif node.node_type == WorkflowNodeType.PARALLEL_GROUP:
                if not node.config or not node.config.get("candidates"):
                    self.issues.append(f"PARALLEL_GROUP node {node.node_key} requires config.candidates")

            elif node.node_type == WorkflowNodeType.CONSENSUS:
                if not node.config or not node.config.get("decision_rule"):
                    self.issues.append(f"CONSENSUS node {node.node_key} requires config.decision_rule")

    def _validate_referenced_agents(self, nodes: Dict[str, WorkflowNode], project_id: str) -> None:
        """Validate that all referenced agent versions exist and belong to the project."""
        for node_id, node in nodes.items():
            if node.node_type == WorkflowNodeType.AGENT:
                agent_version_id = node.config.get("agent_version_id") if node.config else None
                if agent_version_id:
                    self._validate_agent_version_exists(agent_version_id, project_id, node.node_key)

            elif node.node_type in (WorkflowNodeType.JUDGE, WorkflowNodeType.PARALLEL_GROUP):
                config = node.config or {}
                if node.node_type == WorkflowNodeType.JUDGE:
                    judge_version_id = config.get("judge_agent_version_id")
                    if judge_version_id:
                        self._validate_agent_version_exists(judge_version_id, project_id, node.node_key)

                elif node.node_type == WorkflowNodeType.PARALLEL_GROUP:
                    candidates = config.get("candidates", [])
                    for i, candidate in enumerate(candidates):
                        candidate_version_id = candidate.get("agent_version_id")
                        if candidate_version_id:
                            self._validate_agent_version_exists(
                                candidate_version_id, project_id, f"{node.node_key}[{i}]"
                            )

    def _validate_agent_version_exists(self, agent_version_id: str, project_id: str, node_key: str) -> None:
        """Validate a specific agent version exists and is in the correct project."""
        agent_version = self.db.query(AgentVersion).filter(AgentVersion.id == agent_version_id).first()

        if not agent_version:
            self.issues.append(f"Node {node_key} references unknown agent version: {agent_version_id}")
            return

        agent = self.db.query(Agent).filter(Agent.id == agent_version.agent_id).first()
        if not agent or agent.project_id != project_id:
            self.issues.append(
                f"Node {node_key} references agent version from different project: {agent_version_id}"
            )

    def _validate_referenced_evaluations(self, nodes: Dict[str, WorkflowNode], project_id: str) -> None:
        """Validate that referenced evaluation definitions exist and belong to the project."""
        for node_id, node in nodes.items():
            if node.node_type == WorkflowNodeType.JUDGE:
                config = node.config or {}
                eval_version_id = config.get("evaluation_definition_version_id")
                if eval_version_id:
                    self._validate_evaluation_version_exists(eval_version_id, project_id, node.node_key)

    def _validate_evaluation_version_exists(self, eval_version_id: str, project_id: str, node_key: str) -> None:
        """Validate an evaluation version exists and is in the correct project."""
        eval_version = self.db.query(EvaluationDefinitionVersion).filter(
            EvaluationDefinitionVersion.id == eval_version_id
        ).first()

        if not eval_version:
            self.issues.append(f"Node {node_key} references unknown evaluation version: {eval_version_id}")
            return

        eval_def = self.db.query(EvaluationDefinition).filter(
            EvaluationDefinition.id == eval_version.evaluation_definition_id
        ).first()
        if not eval_def or eval_def.project_id != project_id:
            self.issues.append(
                f"Node {node_key} references evaluation from different project: {eval_version_id}"
            )
