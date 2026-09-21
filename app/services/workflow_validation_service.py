"""Workflow DAG Validation — MA7.1 Publish-Time Validation.

Validates DAG structure and configuration before publishing a WorkflowVersion.
Enforces Section 16 constraints: cycles, reachability, node type constraints,
project isolation, referenced agent/evaluation versions.
"""

from typing import Dict, List, Optional, Set, Any
from sqlalchemy.orm import Session

from app.config import settings
from app.db.enums import ModelSelectionMode, RouterFreePolicy, VersionStatus, WorkflowNodeType
from app.models.agents import Agent, AgentVersion
from app.models.evaluation_definitions import EvaluationDefinition, EvaluationDefinitionVersion
from app.models.providers import ProviderModel
from app.models.workflow import WorkflowVersion, WorkflowNode, WorkflowEdge

# MA7.5A -- the ONLY configuration an EVALUATION node may carry. There is
# deliberately no ``method`` (it is always the MA6 agent evaluator) and nothing
# that could turn an evaluation into a decision.
EVALUATION_REQUIRED_CONFIG_KEYS = ("evaluation_definition_version_id", "evaluator_agent_version_id")
EVALUATION_ALLOWED_CONFIG_KEYS = frozenset(
    EVALUATION_REQUIRED_CONFIG_KEYS + ("evaluator_model_policy_override",)
)
# Named separately only so the error can say WHY a key is refused: an
# evaluation is evidence, never authority (no judge, score, ranking, winner or
# automatic approval/rejection). Anything else unknown is refused too.
EVALUATION_FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "judge_agent_version_id",
        "decision_rule",
        "candidates",
        "auto_approve",
        "auto_approval",
        "auto_reject",
        "default_decision",
        "approver_agent_version_id",
        "approval_group",
        "winner",
        "rank",
        "ranking",
        "score",
        "threshold",
        "pass_threshold",
        "on_fail",
        "on_not_met",
        "method",
        "agent_version_id",
    }
)
_OVERRIDE_ALLOWED_KEYS = frozenset({"mode", "manual_provider_model_id", "auto_policy"})


def evaluation_dependency_problems(db: Session, node: WorkflowNode, project_id: str) -> List[str]:
    """Everything an EVALUATION node's frozen configuration points at, checked
    against CURRENT registry state: the Evaluation Definition Version and the
    evaluator Agent Version must exist, belong to ``project_id`` and be ACTIVE
    (MA6 refuses to run an evaluation against anything else), and the optional
    model-policy override must be well formed. One function used by publish
    validation AND by the WorkflowRun start-time preflight, so the two can
    never disagree about what "valid" means. Returns human-readable problems
    (empty = fine); never raises, never modifies anything."""
    problems: List[str] = []
    config = node.config or {}
    key = node.node_key

    version_id = config.get("evaluation_definition_version_id")
    if isinstance(version_id, str) and version_id:
        version = db.get(EvaluationDefinitionVersion, version_id)
        if version is None:
            problems.append(f"EVALUATION node {key} references unknown evaluation definition version {version_id}")
        else:
            definition = db.get(EvaluationDefinition, version.evaluation_definition_id)
            if definition is None or definition.project_id != project_id:
                problems.append(
                    f"EVALUATION node {key} references an evaluation definition version from a different "
                    f"project: {version_id}"
                )
            if version.status != VersionStatus.ACTIVE:
                problems.append(
                    f"EVALUATION node {key} evaluation definition version {version_id} is not ACTIVE "
                    f"(status={version.status.value!r})"
                )

    agent_version_id = config.get("evaluator_agent_version_id")
    if isinstance(agent_version_id, str) and agent_version_id:
        agent_version = db.get(AgentVersion, agent_version_id)
        if agent_version is None:
            problems.append(f"EVALUATION node {key} references unknown evaluator agent version {agent_version_id}")
        else:
            agent = db.get(Agent, agent_version.agent_id)
            if agent is None or agent.project_id != project_id:
                problems.append(
                    f"EVALUATION node {key} references an evaluator agent version from a different project: "
                    f"{agent_version_id}"
                )
            if agent_version.status != VersionStatus.ACTIVE:
                problems.append(
                    f"EVALUATION node {key} evaluator agent version {agent_version_id} is not ACTIVE "
                    f"(status={agent_version.status.value!r})"
                )

    override = config.get("evaluator_model_policy_override")
    if override is not None:
        problems.extend(_model_policy_override_problems(db, key, override))
    return problems


def _model_policy_override_problems(db: Session, node_key: str, override: object) -> List[str]:
    prefix = f"EVALUATION node {node_key} evaluator_model_policy_override"
    if not isinstance(override, dict) or not override:
        return [f"{prefix} must be a non-empty object"]
    unknown = sorted(set(override) - _OVERRIDE_ALLOWED_KEYS)
    if unknown:
        return [f"{prefix} has unsupported key(s): {', '.join(unknown)}"]
    mode = override.get("mode")
    if mode == ModelSelectionMode.MANUAL.value:
        model_id = override.get("manual_provider_model_id")
        if not isinstance(model_id, str) or not model_id:
            return [f"{prefix} mode 'manual' requires manual_provider_model_id"]
        if db.get(ProviderModel, model_id) is None:
            return [f"{prefix} references unknown provider model {model_id}"]
        return []
    if mode == ModelSelectionMode.AUTO.value:
        allowed = {policy.value for policy in RouterFreePolicy}
        if override.get("auto_policy") not in allowed:
            return [f"{prefix} mode 'auto' requires auto_policy in {sorted(allowed)}"]
        return []
    return [f"{prefix} mode must be 'manual' or 'auto'"]


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
        self._validate_no_conditional_edges(nodes, edges)
        self._validate_max_fan_out(nodes, edges)
        self._validate_no_cycles(nodes, edges)
        self._validate_connectivity(nodes, edges)
        self._validate_entry_structure(nodes, edges)
        self._validate_terminal_structure(nodes, edges)
        self._validate_all_nodes_reachable(nodes, edges)
        self._validate_all_paths_reach_terminal(nodes, edges)
        self._validate_node_type_constraints(nodes)
        self._validate_human_approval_nodes(nodes, edges)
        self._validate_evaluation_nodes(nodes, edges, project_id)
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

    def _validate_no_conditional_edges(self, nodes: Dict[str, WorkflowNode], edges: List[tuple]) -> None:
        """Conditional routing does not exist yet (MA7.4a). The engine has
        never evaluated an edge ``condition`` -- it would run the edge
        unconditionally -- so publishing a workflow whose edges carry one
        would silently do something other than what its author wrote. Reject
        any edge that has a condition (even an empty one) until routing is
        implemented.

        ``condition`` is a JSON column, so "no condition" is a Python ``None``
        after loading (an explicit None is stored as JSON null, not SQL NULL);
        the test is therefore ``is not None`` on the loaded value.
        """
        for from_id, to_id, edge in edges:
            if edge.condition is not None:
                source = nodes[from_id].node_key if from_id in nodes else from_id
                target = nodes[to_id].node_key if to_id in nodes else to_id
                self.issues.append(
                    f"Edge {source} -> {target} has a condition; conditional routing is not supported yet"
                )

    def _validate_max_fan_out(self, nodes: Dict[str, WorkflowNode], edges: List[tuple]) -> None:
        """MA7.4b: no node may fan out to more than
        ``settings.workflow_max_fan_out`` direct successors. Read at call
        time (not import time) so the operator-configured limit is what
        publishes are held to. Fan-IN is unbounded by this rule: it is the
        number of parallel branches a node *starts* that costs."""
        limit = settings.workflow_max_fan_out
        outgoing: Dict[str, int] = {}
        for from_id, _to_id, _edge in edges:
            outgoing[from_id] = outgoing.get(from_id, 0) + 1
        for from_id in sorted(outgoing, key=lambda node_id: nodes[node_id].node_key if node_id in nodes else node_id):
            if outgoing[from_id] > limit:
                key = nodes[from_id].node_key if from_id in nodes else from_id
                self.issues.append(
                    f"Node {key} fans out to {outgoing[from_id]} nodes; the maximum is {limit} "
                    "direct outgoing branches per node"
                )

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

    # Config keys that would make a human gate something other than an explicit
    # human decision. A HUMAN_APPROVAL node has no automatic, timed, or
    # agent-made outcome (MA7.3): only an authenticated human can resolve it.
    _FORBIDDEN_HUMAN_APPROVAL_CONFIG_KEYS = frozenset(
        {
            "auto_approve",
            "auto_approval",
            "auto_approve_after_seconds",
            "auto_reject",
            "default_decision",
            "timeout",
            "timeout_seconds",
            "expires_at",
            "expire_after_seconds",
            "agent_version_id",
            "approver_agent_version_id",
            "model_policy_override",
        }
    )

    def _validate_human_approval_nodes(self, nodes: Dict[str, WorkflowNode], edges: List[tuple]) -> None:
        """HUMAN_APPROVAL (MA7.3): a durable wait for an explicit human
        decision.

        - ``config.approval_group`` is a non-empty string label;
        - no auto-approve / timeout / agent-approver configuration, and no
          node-level ``timeout_seconds``: nothing but a human may resolve it;

        MA7.4c: a gate may have SEVERAL upstream dependencies. It is then the
        ALL-of barrier itself (ready only when every source is COMPLETED) and
        its Approval binds every source's output -- there is no separate JOIN
        node. Single-parent gates are unchanged.
        """
        for node in nodes.values():
            if node.node_type != WorkflowNodeType.HUMAN_APPROVAL:
                continue
            config = node.config or {}
            group = config.get("approval_group")
            # A missing/empty group is already reported by
            # _validate_node_type_constraints; only report a wrong type here.
            if group and not (isinstance(group, str) and group.strip()):
                self.issues.append(f"HUMAN_APPROVAL node {node.node_key} config.approval_group must be a non-empty string")
            forbidden = sorted(self._FORBIDDEN_HUMAN_APPROVAL_CONFIG_KEYS.intersection(config))
            if forbidden:
                self.issues.append(
                    f"HUMAN_APPROVAL node {node.node_key} must not configure automatic or timed approval: "
                    f"{', '.join(forbidden)}"
                )
            if node.timeout_seconds is not None:
                self.issues.append(f"HUMAN_APPROVAL node {node.node_key} must not set timeout_seconds")

    def _validate_evaluation_nodes(
        self, nodes: Dict[str, WorkflowNode], edges: List[tuple], project_id: str
    ) -> None:
        """EVALUATION (MA7.5A): one MA6 agent-evaluator run over the output of
        exactly ONE upstream AGENT node. Evidence, never a decision.

        - exactly one incoming edge, and its source is an AGENT node (the
          evaluated artifact must belong to a real Agent Run; evaluating a
          SET of outputs is not something MA6 supports);
        - configuration is an allow-list: the two frozen version ids and the
          optional model-policy override. Anything that could make the node a
          judge / scorer / approver, and anything unknown, is refused;
        - its dependencies exist, belong to this project and are ACTIVE
          (``evaluation_dependency_problems``, shared with the start preflight).
        """
        sources_of: Dict[str, List[str]] = {}
        for from_id, to_id, _edge in edges:
            sources_of.setdefault(to_id, []).append(from_id)

        for node_id, node in nodes.items():
            if node.node_type != WorkflowNodeType.EVALUATION:
                continue
            key = node.node_key
            sources = sources_of.get(node_id, [])
            if len(sources) != 1:
                self.issues.append(
                    f"EVALUATION node {key} must have exactly one incoming edge from an AGENT node "
                    f"(found {len(sources)})"
                )
            elif sources[0] in nodes and nodes[sources[0]].node_type != WorkflowNodeType.AGENT:
                self.issues.append(
                    f"EVALUATION node {key} must be fed by an AGENT node, not "
                    f"{nodes[sources[0]].node_type.value.upper()} node {nodes[sources[0]].node_key}"
                )

            config = node.config or {}
            forbidden = sorted(EVALUATION_FORBIDDEN_CONFIG_KEYS.intersection(config))
            if forbidden:
                self.issues.append(
                    f"EVALUATION node {key} must not configure a decision, judge, score or automatic "
                    f"approval (an evaluation is evidence only): {', '.join(forbidden)}"
                )
            unknown = sorted(set(config) - EVALUATION_ALLOWED_CONFIG_KEYS - EVALUATION_FORBIDDEN_CONFIG_KEYS)
            if unknown:
                self.issues.append(f"EVALUATION node {key} has unsupported configuration key(s): {', '.join(unknown)}")
            for required in EVALUATION_REQUIRED_CONFIG_KEYS:
                value = config.get(required)
                if not isinstance(value, str) or not value.strip():
                    self.issues.append(f"EVALUATION node {key} requires config.{required}")
            if node.max_iterations is not None or node.timeout_seconds is not None:
                self.issues.append(f"EVALUATION node {key} must not set max_iterations or timeout_seconds")

            self.issues.extend(evaluation_dependency_problems(self.db, node, project_id))

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
