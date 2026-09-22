// Model Intelligence — pure view helpers (MA8.3). No DOM, no fetch of its own,
// no globals, so every label/decision the dashboard shows is unit-tested with
// Node's built-in runner (frontend/tests/modelIntelligence.test.mjs).
//
// Everything here only *describes* data the backend already recorded
// (app.services.model_intelligence_service). Quality stays structured
// evidence (MET / PARTIAL / NOT MET counts) — never a score — and a cost the
// platform does not know is never shown as $0.

import { formatTokenCount, formatUsd } from "./runState.js";

// -- labels ----------------------------------------------------------------------------------------------

export function roleLabel(role) {
  if (!role) return "Unknown role";
  return String(role)
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(" ");
}

const POLICY_LABELS = { free_only: "FREE ONLY", prefer_free: "PREFER FREE", any: "ANY" };

export function policyLabel(policy) {
  return POLICY_LABELS[policy] || (policy ? String(policy).toUpperCase() : "—");
}

const STRATEGY_LABELS = {
  "ma8.1-deterministic-v1": "Deterministic rules v1",
  "ma8.2-evidence-v1": "Evidence routing v1",
};

export function strategyLabel(strategy) {
  return STRATEGY_LABELS[strategy] || strategy || "—";
}

const EVIDENCE_STATUS_LABELS = {
  applied: "Evidence used",
  insufficient: "Not enough history yet — deterministic rules used",
  unavailable: "Evidence unavailable — deterministic rules used",
  disabled: "Evidence routing off — deterministic rules used",
};

export function evidenceStatusLabel(status, { isManual = false } = {}) {
  if (isManual) return "Not applicable — operator choice";
  return EVIDENCE_STATUS_LABELS[status] || "Not recorded";
}

const EXCLUSION_LABELS = {
  MODEL_NOT_FOUND: "Model not in the catalog",
  MODEL_INACTIVE: "Model not active",
  PROVIDER_UNAVAILABLE: "Provider down",
  PROVIDER_MODEL_UNAVAILABLE: "Model unavailable at this provider",
  NOT_FREE: "Not free (policy requires FREE)",
  PRICING_UNKNOWN: "Pricing unknown",
  MANUAL_MODEL_INVALID: "No model chosen",
  POLICY_INVALID: "Invalid model policy",
  NO_ELIGIBLE_MODEL: "No eligible model",
};

export function exclusionLabel(code) {
  return EXCLUSION_LABELS[code] || code || "Excluded";
}

export function exclusionText(candidate) {
  const reasons = (candidate && candidate.exclusion_reasons) || [];
  return reasons.length ? reasons.map((code) => `${exclusionLabel(code)} (${code})`).join(", ") : "Excluded";
}

// -- mode --------------------------------------------------------------------------------------------------

// MANUAL is always presented as the operator's own choice, never as evidence-selected.
export function modeView(decision) {
  if (!decision) return { mode: "—", title: "—", note: "" };
  if (decision.is_manual || decision.selection_mode === "manual") {
    return { mode: "MANUAL", title: "MANUAL", note: "Operator selected this model." };
  }
  return {
    mode: "AUTO",
    title: `AUTO · ${policyLabel(decision.requested_policy)}`,
    note: "The router chose this model under the Agent's policy.",
  };
}

export function outcomeLabel(decision) {
  if (!decision) return "—";
  if (decision.outcome === "failed") return `No model — ${exclusionLabel(decision.failure_code)}`;
  if (decision.outcome === "selected") return "Model selected";
  return "—";
}

export function yesNo(value) {
  if (value === true) return "YES";
  if (value === false) return "NO";
  return "—";
}

// "What would deterministic rules alone have picked, and did evidence change it?"
// Only meaningful when evidence was actually consulted for an AUTO decision.
export function choiceComparison(detail) {
  const evidence = detail && detail.evidence;
  if (!detail || detail.is_manual || !evidence || !evidence.deterministic_pick_provider_model_id) return null;
  const deterministic = evidence.deterministic_pick_canonical_model_id || "(model not in the recorded list)";
  return {
    deterministic,
    selected: detail.selected_canonical_model_id || "—",
    changed: yesNo(evidence.evidence_changed_selection),
    evidenceUsed: evidence.status === "applied",
  };
}

// -- cost / tokens ---------------------------------------------------------------------------------------

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

// Exact, estimated and unknown cost stay separate lines. Unknown is a count of
// calls — never a dollar figure, never $0.
export function costBreakdown(cost) {
  const c = cost || {};
  const lines = [];
  if (c.exact_calls) lines.push({ kind: "exact", text: `${formatUsd(c.exact_usd)} exact (${plural(c.exact_calls, "call")})` });
  if (c.estimated_calls) {
    lines.push({ kind: "estimated", text: `${formatUsd(c.estimated_usd)} estimated (${plural(c.estimated_calls, "call")})` });
  }
  if (c.unknown_calls) lines.push({ kind: "unknown", text: `Unknown cost for ${plural(c.unknown_calls, "call")}` });
  if (!lines.length) lines.push({ kind: "none", text: "No cost recorded" });
  return lines;
}

export function costShort(cost) {
  return costBreakdown(cost)
    .map((line) => line.text)
    .join(" · ");
}

export function tokenLine(usage) {
  const u = usage || {};
  return `${formatTokenCount(u.tokens_total ?? 0)} total · ${formatTokenCount(u.tokens_in ?? 0)} in · ${formatTokenCount(u.tokens_out ?? 0)} out`;
}

// -- evidence ------------------------------------------------------------------------------------------

export const NOT_ENOUGH_HISTORY = "Not enough history yet";

const RELIABILITY_STANDING = {
  insufficient: NOT_ENOUGH_HISTORY,
  ok: "No reliability concern",
  demoted: "Frequent provider failures",
};

const QUALITY_STANDING = {
  insufficient: NOT_ENOUGH_HISTORY,
  positive: "Mostly MET",
  mixed: "Mixed findings",
  negative: "Frequent NOT MET",
};

export function reliabilityView(entry) {
  const e = entry || {};
  const calls = e.model_calls ?? e.observations ?? 0;
  const parts = [`Calls ${calls}`, `Completed ${e.completed_calls ?? e.completed ?? 0}`, `Provider failures ${e.provider_failures ?? 0}`];
  if (e.other_failures) parts.push(`Other failures ${e.other_failures}`);
  return { text: parts.join(" · "), standing: RELIABILITY_STANDING[e.reliability] || NOT_ENOUGH_HISTORY };
}

export function qualityView(entry) {
  const e = entry || {};
  const f = e.findings || {};
  if (!e.evaluated_runs) return { text: "No evaluated runs", standing: NOT_ENOUGH_HISTORY };
  const text = [
    `Evaluated runs ${e.evaluated_runs}`,
    `MET ${f.met || 0}`,
    `PARTIAL ${f.partial || 0}`,
    `NOT MET ${f.not_met || 0}`,
    f.not_applicable ? `N/A ${f.not_applicable} (not counted)` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return { text, standing: QUALITY_STANDING[e.quality] || NOT_ENOUGH_HISTORY };
}

// Reliability and quality are different questions; the issue list must say so.
export function issueKindLabel(kind) {
  if (kind === "reliability") return "Reliability issue — not a quality judgement";
  if (kind === "configuration") return "Configuration issue (credentials)";
  return "Failed call";
}

// -- evidence policy -------------------------------------------------------------------------------------

export function policyView(status) {
  const s = status || {};
  if (!s.installed) {
    return {
      state: "not_installed",
      label: "NOT INSTALLED",
      badgeClass: "badge badge-neutral",
      actionLabel: null,
      summary: "The evidence routing policy is not installed on this database yet (run the database migrations).",
      facts: [],
    };
  }
  const facts = [
    ["Strategy", `${strategyLabel(s.strategy)} (policy v${s.version})`],
    ["History window", `${s.history_window_days} days`],
    ["Reliability minimum", `${s.min_observations} observed calls`],
    ["Quality minimum", `${s.min_evaluated_runs} evaluated runs`],
  ];
  if (s.enabled) {
    return {
      state: "active",
      label: "ACTIVE",
      badgeClass: "badge badge-done",
      actionLabel: "Disable evidence routing",
      summary: "AUTO routing may reorder the models its policy allows using this project's recorded evidence. MANUAL choices are never changed.",
      confirmTitle: "Disable evidence routing?",
      confirmText: "AUTO routing returns to the deterministic rules. All history and evidence are kept, and you can enable it again at any time.",
      facts,
    };
  }
  return {
    state: "off",
    label: "OFF",
    badgeClass: "badge badge-neutral",
    actionLabel: "Enable evidence routing",
    summary: "AUTO routing uses the deterministic rules. History is still recorded and shown below.",
    confirmTitle: "Enable evidence routing?",
    confirmText:
      "AUTO routing will use this project's recorded reliability and evaluation evidence to reorder the models its policy already allows (FREE ONLY / PREFER FREE stay respected). MANUAL choices are never changed.",
    facts,
  };
}

// Two-step, explicit toggle. Without ``confirmed`` it only asks for a
// confirmation — it never calls the API. With it, it asks the backend to flip
// the state and returns the backend's own resulting status.
export async function toggleEvidenceRouting({ api, current, confirmed }) {
  if (!current || !current.installed) return { ok: false, error: "The evidence routing policy is not installed." };
  if (!confirmed) return { ok: false, needsConfirm: true };
  try {
    const status = await api.post("/model-intelligence/evidence-policy", { enabled: !current.enabled });
    return { ok: true, status };
  } catch (err) {
    return { ok: false, error: toggleErrorMessage(err) };
  }
}

export function toggleErrorMessage(err) {
  if (err && err.status === 403) return "Only an organization owner or admin can change evidence routing.";
  if (err && err.status === 409) return (err && err.message) || "The evidence routing policy is not installed.";
  return (err && err.message) || "Could not change evidence routing.";
}

// -- page states -------------------------------------------------------------------------------------------

export function sectionState({ loading, error, items }) {
  if (loading) return { kind: "loading", text: "Loading…" };
  if (error) return { kind: "error", text: (error && error.message) || String(error) };
  if (!items || !items.length) return { kind: "empty" };
  return { kind: "ready" };
}

export const EMPTY_TEXT = {
  roles: "No model calls in the last 30 days yet. Run an Agent or workflow and its models will appear here.",
  issues: "No failed model calls in the last 30 days.",
  decisions: "No routing decisions recorded yet. They appear once Agents run on this version of the platform.",
};
