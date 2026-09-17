// Pure formatting/labeling helpers — no DOM, no fetch, no globals, so
// these can be unit-tested directly with Node's built-in test runner
// (see frontend/tests/format.test.mjs). Never infer FREE from a model
// name here — every classification passed in must already come from the
// backend's app.domain.pricing.classify_pricing (Section 8).

export function formatCostForClassification(costAmount, classification) {
  const cls = (classification || "unknown").toLowerCase();
  if (cls === "free") return "FREE — $0.0000";
  const amount = toNumber(costAmount);
  if (amount === null) {
    return cls === "unknown" ? "UNKNOWN pricing" : "—";
  }
  const formatted = `$${amount.toFixed(4)}`;
  return cls === "unknown" ? `${formatted} (unknown pricing)` : formatted;
}

export function formatCost(costAmount) {
  const amount = toNumber(costAmount);
  if (amount === null) return "—";
  return `$${amount.toFixed(4)}`;
}

export function pricingBadgeClass(classification) {
  const cls = (classification || "unknown").toLowerCase();
  if (cls === "free") return "badge badge-free";
  if (cls === "paid") return "badge badge-paid";
  return "badge badge-unknown";
}

export function pricingLabel(classification) {
  const cls = (classification || "unknown").toLowerCase();
  if (cls === "free") return "FREE";
  if (cls === "paid") return "PAID";
  return "UNKNOWN";
}

export function formatTokens(value) {
  const n = toNumber(value);
  if (n === null) return "—";
  return n.toLocaleString("en-US");
}

export function formatLatency(ms) {
  const n = toNumber(ms);
  if (n === null || n <= 0) return "—";
  if (n < 1000) return `${Math.round(n)} ms`;
  return `${(n / 1000).toFixed(2)} s`;
}

const CANDIDATE_STATUS_LABELS = {
  not_launched: "Not launched",
  created: "Queued",
  queued: "Queued",
  running: "Running",
  waiting_for_agent: "Running",
  waiting_for_tool: "Running",
  waiting_for_approval: "Waiting for approval",
  verifying: "Running",
  cancelling: "Cancelling",
  cancelled: "Cancelled",
  completed: "Completed",
  failed: "Failed",
};

export function candidateStatusLabel(status) {
  return CANDIDATE_STATUS_LABELS[status] || status || "Unknown";
}

export function candidateStatusBadgeClass(status) {
  const s = (status || "").toLowerCase();
  if (s === "completed") return "badge badge-done";
  if (s === "failed") return "badge badge-failed";
  if (s === "cancelled") return "badge badge-neutral";
  if (s === "not_launched") return "badge badge-neutral";
  return "badge badge-running";
}

const PHASE_LABELS = {
  pending: "Pending",
  running: "Running",
  ready_for_selection: "Ready for selection",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function phaseLabel(phase) {
  return PHASE_LABELS[phase] || phase || "Unknown";
}

export function isTerminalPhase(phase) {
  return phase === "ready_for_selection" || phase === "completed" || phase === "failed" || phase === "cancelled";
}

export function truncate(str, maxLength) {
  if (typeof str !== "string") return "";
  if (str.length <= maxLength) return str;
  return `${str.slice(0, Math.max(0, maxLength - 1)).trimEnd()}…`;
}

export function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}
