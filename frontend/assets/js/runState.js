// Runtime state mapping and formatting for the Control Room (MA7.6B) -- pure,
// DOM-free, unit-tested directly (frontend/tests/runState.test.mjs).
//
// Everything here is a PRESENTATION of states the backend already persists
// (WorkflowRunStatus, WorkflowNodeRunStatus, AgentRunStatus, ApprovalStatus):
// no new state is invented or stored. A node "Queued" vs "Running" is read off
// the node's Agent Run status (created vs running), never guessed.
//
// Nothing here scores, ranks or recommends: an Evaluation finding is shown as the
// evidence it is, and an approval is only ever a person's decision.

// -- run / node status --------------------------------------------------------------------------------

export const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "cancelled"]);
export const TERMINAL_NODE_STATUSES = new Set(["completed", "failed", "cancelled"]);

export function isTerminalRun(status) {
  return TERMINAL_RUN_STATUSES.has(status);
}

export function isTerminalNode(status) {
  return TERMINAL_NODE_STATUSES.has(status);
}

// Tone drives colour only: neutral | running | attention | success | danger.
export const RUN_STATUS_INFO = {
  created: { label: "Starting", tone: "running", help: "The workflow is being set up." },
  running: { label: "Running", tone: "running", help: "The team is working through the workflow." },
  node_waiting_for_agent: { label: "Running", tone: "running", help: "A step is waiting for an Agent to pick it up." },
  node_waiting_for_approval: {
    label: "Waiting for your decision",
    tone: "attention",
    help: "The team has done everything it can for now. A person must approve or reject before the workflow continues.",
  },
  repair_loop_active: { label: "Running", tone: "running", help: "A repair step is in progress." },
  escalated: { label: "Escalated", tone: "attention", help: "The run needs attention." },
  cancelling: { label: "Cancelling", tone: "attention", help: "Cancellation was requested. Running work is being asked to stop." },
  cancelled: { label: "Cancelled", tone: "neutral", help: "The run was cancelled. Work already finished is kept." },
  completed: { label: "Completed", tone: "success", help: "Every step finished and the required decision was made." },
  failed: { label: "Failed", tone: "danger", help: "A step failed or a person rejected the approval. Work already finished is kept." },
};

export function runStatusInfo(status) {
  return RUN_STATUS_INFO[status] || { label: titleCase(status), tone: "neutral", help: "" };
}

function titleCase(value) {
  const text = String(value == null ? "" : value).replace(/_/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "Unknown";
}

const AGENT_ACTIVE = new Set(["running", "waiting_for_tool", "waiting_for_model", "sandbox_provisioning"]);

// The state to show for one node of a run snapshot (RunNodeRead).
// -> { key, label, tone, detail }
export function nodeState(node) {
  const agentStatus = node && node.agent ? node.agent.status : null;
  switch (node && node.status) {
    case "pending":
      return { key: "waiting", label: "Waiting", tone: "neutral", detail: "Starts when everything before it has finished." };
    case "running":
      if (agentStatus === "created") {
        return { key: "queued", label: "Queued", tone: "running", detail: "Assigned, waiting for a worker to pick it up." };
      }
      if (agentStatus === "cancelling") {
        return { key: "cancelling", label: "Cancelling", tone: "attention", detail: "Asked to stop." };
      }
      if (agentStatus === null || AGENT_ACTIVE.has(agentStatus)) {
        return { key: "running", label: "Running", tone: "running", detail: "Working now." };
      }
      return { key: "running", label: "Running", tone: "running", detail: "Wrapping up." };
    case "waiting_for_approval":
      return { key: "awaiting_decision", label: "Waiting for Human Approval", tone: "attention", detail: "A person needs to approve or reject." };
    case "completed":
      return { key: "completed", label: "Completed", tone: "success", detail: "Finished." };
    case "failed":
      if (node.approval_status === "rejected") {
        return { key: "failed", label: "Failed", tone: "danger", detail: "A person rejected this approval." };
      }
      return { key: "failed", label: "Failed", tone: "danger", detail: "This step failed." };
    case "cancelled":
      if (node.approval_status === "expired") {
        return { key: "cancelled", label: "Cancelled", tone: "neutral", detail: "Cancelled before a decision was made." };
      }
      return { key: "cancelled", label: "Cancelled", tone: "neutral", detail: "Cancelled before it finished." };
    default:
      return { key: String((node && node.status) || "unknown"), label: titleCase(node && node.status), tone: "neutral", detail: "" };
  }
}

export function runProgress(nodes) {
  const counts = { total: 0, completed: 0, running: 0, waiting: 0, awaiting_decision: 0, failed: 0, cancelled: 0 };
  for (const node of nodes || []) {
    counts.total += 1;
    const key = nodeState(node).key;
    if (key === "completed") counts.completed += 1;
    else if (key === "running" || key === "queued" || key === "cancelling") counts.running += 1;
    else if (key === "awaiting_decision") counts.awaiting_decision += 1;
    else if (key === "failed") counts.failed += 1;
    else if (key === "cancelled") counts.cancelled += 1;
    else counts.waiting += 1;
  }
  return counts;
}

// Gates that are waiting on a person RIGHT NOW.
export function pendingApprovalNodes(detail) {
  return ((detail && detail.nodes) || []).filter((node) => node.status === "waiting_for_approval" && node.approval_status === "pending");
}

// One plain sentence about what happens next.
export function nextStepText(detail) {
  if (!detail) return "";
  const waiting = pendingApprovalNodes(detail);
  switch (detail.status) {
    case "node_waiting_for_approval":
      return waiting.length > 1
        ? `${waiting.length} decisions are waiting for you. Nothing continues until you decide.`
        : "It is waiting for your decision. Nothing continues until you approve or reject.";
    case "completed":
      return "The workflow is finished. Open any step to see its output and evidence.";
    case "failed":
      return "The workflow stopped. Finished steps and their evidence are kept. Open the failed step to see why.";
    case "cancelled":
      return "The workflow was cancelled. Finished steps and their evidence are kept.";
    case "cancelling":
      return "Cancelling: running steps are being asked to stop. This page updates on its own.";
    case "created":
      return "Starting. The first steps are being queued.";
    default:
      return "The team is working. This page updates on its own. Steps that need a person will be flagged here.";
  }
}

// -- polling cadence ---------------------------------------------------------------------------------

export const LIVE_INTERVAL_MS = 2000;
export const APPROVAL_WAIT_INTERVAL_MS = 5000;
export const HIDDEN_TAB_INTERVAL_MS = 15000;

// Faster while the team is working, slower while it only waits for a person, and
// slow when the tab is hidden.
export function pollIntervalFor(detail, { hidden = false } = {}) {
  const base = detail && detail.status === "node_waiting_for_approval" ? APPROVAL_WAIT_INTERVAL_MS : LIVE_INTERVAL_MS;
  return hidden ? Math.max(base, HIDDEN_TAB_INTERVAL_MS) : base;
}

// -- formatting ---------------------------------------------------------------------------------------

export function shortId(id) {
  return id ? String(id).slice(0, 8) : "—";
}

export function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return "—";
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  if (minutes < 60) return `${minutes}m ${String(rest).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

// Seconds between two ISO timestamps (or from `start` to `nowMs`), or null.
export function secondsBetween(startIso, endIso, nowMs = null) {
  if (!startIso) return null;
  const start = Date.parse(startIso);
  const end = endIso ? Date.parse(endIso) : nowMs;
  if (!Number.isFinite(start) || end == null || !Number.isFinite(end)) return null;
  return Math.max(0, (end - start) / 1000);
}

// A Decimal arrives as a string (or, from some drivers, a number). Display only:
// arithmetic on money happens on the server in Decimal.
export function formatUsd(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "$0.00";
  if (n >= 1) return `$${n.toFixed(2)}`;
  if (n >= 0.01) return `$${n.toFixed(4).replace(/0+$/, "").replace(/(\.\d)$/, "$10")}`;
  return `$${n.toFixed(6).replace(/0+$/, "")}`;
}

export function formatTokenCount(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return Number(value).toLocaleString("en-US");
}

// { text, estimated, exact, unavailable } for a RunUsageRead. A total that
// includes ANY estimated call is labelled estimated -- never presented as exact.
export function costLabel(usage) {
  if (!usage) return { text: "—", estimated: false, exact: false, unavailable: true };
  if (usage.cost_amount === null || usage.cost_amount === undefined) {
    return { text: "mixed currencies", estimated: Boolean(usage.cost_is_estimated), exact: false, unavailable: true };
  }
  const amount = formatUsd(usage.cost_amount);
  if (usage.cost_is_estimated) return { text: `${amount} (estimated)`, estimated: true, exact: false, unavailable: false };
  return { text: `${amount} (exact)`, estimated: false, exact: true, unavailable: false };
}

// "1,234 tokens · $0.0150 (estimated)" -- or "No model calls yet".
export function usageLine(usage) {
  if (!usage) return "No model calls yet";
  return `${formatTokenCount(usage.total_tokens)} tokens · ${costLabel(usage).text}`;
}

// An evaluation finding as evidence (never a verdict).
export const FINDING_INFO = {
  met: { label: "MET", tone: "success" },
  partial: { label: "PARTIAL", tone: "attention" },
  not_met: { label: "NOT MET", tone: "danger" },
  not_applicable: { label: "NOT APPLICABLE", tone: "neutral" },
};

export function findingInfo(finding) {
  return FINDING_INFO[finding] || { label: titleCase(finding).toUpperCase(), tone: "neutral" };
}

// Counts of findings across criterion results: evidence, never a score.
export function findingCounts(results) {
  const counts = { met: 0, partial: 0, not_met: 0, not_applicable: 0 };
  for (const result of results || []) if (result.finding in counts) counts[result.finding] += 1;
  return counts;
}

export function approvalStatusInfo(status) {
  switch (status) {
    case "pending":
      return { label: "Waiting for your decision", tone: "attention" };
    case "approved":
      return { label: "Approved", tone: "success" };
    case "rejected":
      return { label: "Rejected", tone: "danger" };
    case "expired":
      return { label: "Cancelled before a decision", tone: "neutral" };
    default:
      return { label: titleCase(status), tone: "neutral" };
  }
}

// -- approval decision handling ------------------------------------------------------------------------

// Turns a resolve failure (ApiError) into a message for the person deciding.
export function decisionErrorMessage(err) {
  const status = err && err.status;
  const code = err && err.code;
  if (status === 403) return "You do not have permission to decide this. Only a project member with edit access can approve or reject.";
  if (status === 401) return "Your session is not authorized. Reload the page and try again.";
  if (code === "fingerprint_mismatch") {
    return "The evidence changed after this approval was requested, so it was not decided. Reload to see the current evidence.";
  }
  if (status === 409) return "This approval was already decided differently. Nothing was changed. Reload to see the recorded decision.";
  if (status === 404) return "This approval no longer exists.";
  return (err && err.message) || "The decision could not be recorded.";
}

// -- failed-step retry handling (MA7.6B) ---------------------------------------------------------------

// Turns a retry failure (ApiError) into a message for the operator. Only
// substitutes a friendlier message for the ONE case that is genuinely about
// the chosen model (the server's own "does not exist / not active / provider
// down" wording from resolve_manual) -- every other reason (wrong node type,
// not the latest attempt, run not FAILED, a real not-found, no permission) is
// shown as reported by the server, never replaced, so a real contract/ID bug
// is never hidden behind a reassuring model-availability message.
export function retryErrorMessage(err) {
  const status = err && err.status;
  const code = err && err.code;
  const message = (err && err.message) || "";
  if (status === 403) return "You do not have permission to retry this step. Only a project member with edit access can retry a failed step.";
  if (status === 401) return "Your session is not authorized. Reload the page and try again.";
  if (code === "retry_not_allowed" && /does not exist|is not active|is currently down/i.test(message)) {
    return "The selected model is no longer available or active. Refresh the model list and choose another model.";
  }
  if (status === 409) return "This step was already retried, or the run has since changed. Reload to see its current state.";
  if (status === 404) return "This step or workflow run could not be found. Reload the page and try again.";
  return message || "The retry could not be started.";
}
