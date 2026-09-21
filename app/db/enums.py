"""Shared status/lifecycle enums.

Every enum here is persisted via SQLAlchemy's ``Enum(..., native_enum=False)``
which renders as a plain ``VARCHAR`` + ``CHECK`` constraint under SQLite —
never a database-native enum type — per spec Section 10.5.5 / ADR-10:
portable to Postgres later as either a native enum or the same
check-constrained string column, and avoids a SQLite-specific shortcut.
"""

import enum


class OrgRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class ProjectRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


# --- Task / Task Run / Agent Run / Agent Run Attempt (Section 26) --------


class ExecutionMode(str, enum.Enum):
    """Section 7 — the four Core Execution Modes."""

    SINGLE_AGENT = "single_agent"
    BUILD_REVIEW = "build_review"
    PARALLEL_COMPARISON = "parallel_comparison"
    WORKFLOW = "workflow"


class TaskStatus(str, enum.Enum):
    """Template/lifecycle status only — Section 26.1, corrected v1.1 #20."""

    DRAFT = "draft"
    READY = "ready"
    ARCHIVED = "archived"


class TaskRunStatus(str, enum.Enum):
    """Section 26.2, with the v1.1 CANCELLING intermediate state (26.7)."""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_AGENT = "waiting_for_agent"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    VERIFYING = "verifying"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRunStatus(str, enum.Enum):
    """Section 26.3, with the v1.1 CANCELLING intermediate state (26.7)."""

    CREATED = "created"
    SANDBOX_PROVISIONING = "sandbox_provisioning"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_MODEL = "waiting_for_model"
    CANCELLING = "cancelling"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRunAttemptStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# --- Agent Version / Workflow Version lifecycle (Section 26.7) -----------


class VersionStatus(str, enum.Enum):
    """Shared by agent_versions and workflow_versions (Section 26.7 table)."""

    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class DefaultModelStrategy(str, enum.Enum):
    MANUAL_REQUIRED = "manual_required"
    AUTO_PREFERRED = "auto_preferred"
    AUTO_WITH_MANUAL_OVERRIDE = "auto_with_manual_override"


# --- Workflow Run / Workflow Node Run (Section 26.4) ----------------------


class WorkflowRunStatus(str, enum.Enum):
    CREATED = "created"
    RUNNING = "running"
    NODE_WAITING_FOR_AGENT = "node_waiting_for_agent"
    NODE_WAITING_FOR_APPROVAL = "node_waiting_for_approval"
    REPAIR_LOOP_ACTIVE = "repair_loop_active"
    ESCALATED = "escalated"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowNodeRunStatus(str, enum.Enum):
    """Per-node-instance status; one row per iteration (Section 24.4 #1-2)."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowNodeType(str, enum.Enum):
    """Section 16.2."""

    AGENT = "agent"
    PARALLEL_GROUP = "parallel_group"
    CONDITIONAL = "conditional"
    REPAIR_LOOP = "repair_loop"
    JUDGE = "judge"
    CONSENSUS = "consensus"
    HUMAN_APPROVAL = "human_approval"
    TERMINAL = "terminal"
    # MA7.5A: runs one MA6 agent-evaluator EvaluationRun over the output of
    # exactly one upstream AGENT node. Evidence, never a decision: it is not a
    # JUDGE (a decision-making node type that stays unsupported) and has no
    # score/rank/approve semantics.
    EVALUATION = "evaluation"


# --- Approval (Section 26.5) ----------------------------------------------


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalScope(str, enum.Enum):
    TASK_RUN = "task_run"
    AGENT_RUN = "agent_run"
    WORKFLOW_NODE_RUN = "workflow_node_run"
    ARTIFACT = "artifact"


# --- Providers / Models (Section 13) --------------------------------------


class ProviderType(str, enum.Enum):
    OPENROUTER = "openrouter"
    DIRECT = "direct"
    LOCAL = "local"
    ENTERPRISE = "enterprise"


class HealthStatus(str, enum.Enum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class ModelStatus(str, enum.Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    UNAVAILABLE = "unavailable"


class ToolCallingSupport(str, enum.Enum):
    NONE = "none"
    BASIC = "basic"
    PARALLEL = "parallel"
    STRUCTURED = "structured"


class PricingClassification(str, enum.Enum):
    """Derived, never stored redundantly — see app.domain.pricing."""

    FREE = "free"
    PAID = "paid"
    UNKNOWN = "unknown"


class RouterFreePolicy(str, enum.Enum):
    """Section 3/14 free-model support requirement (Owner instruction).

    Not wired into a live Router in MA0 — the column exists so the
    contract/data model can express the policy cleanly when MA8 builds the
    real scorer, per the Owner's explicit "make sure the contracts can
    support this cleanly" instruction.
    """

    FREE_ONLY = "free_only"
    PREFER_FREE = "prefer_free"
    ANY = "any"


class ModelSelectionMode(str, enum.Enum):
    MANUAL = "manual"
    AUTO = "auto"


# --- Tools (Section 15, 24.4 #5) ------------------------------------------


class ToolStatus(str, enum.Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class ToolGrantType(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"


class ToolCallStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


class PermissionDecision(str, enum.Enum):
    ALLOWED = "allowed"
    DENIED = "denied"


class PolicyRuleType(str, enum.Enum):
    APPROVAL_REQUIRED = "approval_required"
    TOOL_ALLOW = "tool_allow"
    TOOL_DENY = "tool_deny"


class PolicyRuleScope(str, enum.Enum):
    GLOBAL = "global"
    PROJECT = "project"
    AGENT_VERSION = "agent_version"
    TOOL = "tool"


# --- Model Calls (Section 6 "Model calls" contract) ------------------------


class ModelCallStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


# --- Idempotency / job queue (Section 24.4 #11, #12) ------------------------


class IdempotencyScope(str, enum.Enum):
    API_REQUEST = "api_request"
    TOOL_CALL = "tool_call"


class IdempotencyStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class JobQueueStatus(str, enum.Enum):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"


class JobType(str, enum.Enum):
    AGENT_RUN = "agent_run"
    WORKFLOW_NODE = "workflow_node"
    EVALUATION = "evaluation"
    # MA1 addition: a harmless, side-effect-free job used only to prove the
    # worker's claim/heartbeat/fencing/complete lifecycle end-to-end before
    # real Agent execution exists (MA3). Never dispatches to a real
    # provider, tool, or agent.
    INTERNAL_TEST = "internal_test"


# --- Budgets / Usage (Section 19, 24.4 #13) ---------------------------------


class BudgetScope(str, enum.Enum):
    EXECUTION = "execution"
    TASK = "task"
    USER = "user"
    PROJECT = "project"
    DAY = "day"
    MONTH = "month"


class BudgetReservationStatus(str, enum.Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    RELEASED = "released"


class UsageSourceType(str, enum.Enum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"


# --- Agent-to-Agent Review (Section: MA4 review contract) -------------------


class AgentRunRole(str, enum.Enum):
    """Tags an ``agent_runs`` row's place in a BUILD_REVIEW execution
    cycle, or (MA6 Slice 3A) an Evaluation Run's model-based evaluator
    execution. NULL/unset for a plain SINGLE_AGENT run (Agent Run predates
    MA4 or is not part of any review/evaluation cycle) — this column is
    purely additive and never required by MA0-MA3 code paths.

    ``EVALUATOR`` is never routed through ``ReviewOrchestrationService`` —
    an evaluator's Agent Run always lives under its own dedicated
    SINGLE_AGENT bookkeeping Task/Task Run (never the subject candidate's
    own), so ``AgentExecutionService._is_build_review`` never returns True
    for it and ``on_agent_run_succeeded`` never sees this role."""

    PRIMARY = "primary"
    REVIEWER = "reviewer"
    REPAIR = "repair"
    EVALUATOR = "evaluator"


class ReviewDecision(str, enum.Enum):
    """Minimum decision set (MA4 — MA6 owns the real Evaluation Engine,
    no scoring/ranking here). INVALID is a fail-safe sentinel for a
    reviewer response the platform could not parse/validate — never
    inferred as ACCEPT."""

    ACCEPT = "accept"
    REPAIR_REQUIRED = "repair_required"
    INVALID = "invalid"


# --- Artifacts / Evaluation / Comparison (Section 17, 18) -------------------


class ArtifactType(str, enum.Enum):
    DIFF = "diff"
    FILE = "file"
    REPORT = "report"
    LOG = "log"


class EvaluationHumanDecision(str, enum.Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class EvaluationJudgeSource(str, enum.Enum):
    LLM_JUDGE = "llm_judge"


class ComparisonRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # MA5 addition: every sibling lifecycle enum in this module already has
    # an explicit cancelled/stopped terminal value (TaskRunStatus.CANCELLED,
    # AgentRunStatus.STOPPED, WorkflowRunStatus.CANCELLED) — this one was
    # simply never exercised before MA5 made comparisons real. "Ready for
    # selection" (all candidates terminal, awaiting the human) is
    # deliberately *not* a stored status here — it's a derived read-time
    # fact (see app.services.comparison_service.compute_phase), matching
    # the "map to existing names, don't invent unnecessary statuses"
    # instruction: RUNNING already covers "still owns further work or is
    # awaiting the human," and only the human's selection (or a hard
    # failure/cancellation) ever moves it to a different stored value.
    CANCELLED = "cancelled"


# --- Evaluation Run execution (MA6 Slice 2) ---------------------------------


class EvaluationMethod(str, enum.Enum):
    """How an Evaluation Run's criterion results were produced.
    ``DETERMINISTIC`` (Slice 2) runs a narrow, honest checker against the
    subject artifact directly. ``AGENT_EVALUATOR`` (MA6 Slice 3) runs one
    evaluator Agent/model inference covering the complete rubric -- the
    evaluator provides evaluation *evidence*, never a Judge, and never
    chooses a winner (Section: MA6 non-negotiable invariant)."""

    DETERMINISTIC = "deterministic"
    AGENT_EVALUATOR = "agent_evaluator"


class EvaluationRunStatus(str, enum.Enum):
    """``CANCELLED`` (MA6 Slice 3) applies only to ``AGENT_EVALUATOR``
    method runs -- a real, potentially long-running model inference call a
    human can cooperatively cancel, same rationale as
    ``ComparisonRunStatus.CANCELLED``. A ``DETERMINISTIC`` run never
    reaches CANCELLED: it reads one artifact and returns almost
    immediately, with no long-running operation to interrupt."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvaluationFinding(str, enum.Enum):
    """Frozen V1 qualitative finding set (MA6 non-negotiable invariant):
    never a percentage, aggregate score, or ranking -- a criterion is
    either MET, PARTIAL, NOT_MET, or explicitly NOT_APPLICABLE (no
    deterministic checker exists for it yet, or its method requires an
    evaluator not available until Slice 3). NOT_APPLICABLE must never be
    conflated with NOT_MET -- one is "this failed," the other is "this was
    never actually checked."
    """

    MET = "met"
    PARTIAL = "partial"
    NOT_MET = "not_met"
    NOT_APPLICABLE = "not_applicable"


# --- Provider catalog refresh (Section 13.3, MA2 addition) ------------------


class CatalogRefreshStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
