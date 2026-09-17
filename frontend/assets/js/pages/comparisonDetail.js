// Comparison results — the most important screen (Section 6/7/8). Reads
// the real MA5 comparison detail, polls while candidates are in flight
// (no SSE stream exists yet for comparisons — app.api.routers.events is a
// 501 stub — so this uses bounded polling per Section 5), and calls the
// real POST /comparisons/{id}/select-winner for human canonical selection.
// Never computes or displays a "best"/"winning" candidate itself
// (Section 7 invariant: NO automatic winner).

import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import {
  candidateStatusBadgeClass,
  candidateStatusLabel,
  formatCostForClassification,
  formatDateTime,
  formatLatency,
  formatTokens,
  isTerminalPhase,
  phaseLabel,
  pricingBadgeClass,
  pricingLabel,
} from "../format.js";

const POLL_INTERVAL_MS = 1500;
const POLL_BOUND_MS = 20 * 60 * 1000; // stop auto-polling after 20 minutes; manual refresh still works

export async function renderComparisonDetail(root, params) {
  const comparisonId = params.id;
  const state = {
    comparison: null,
    task: null,
    pricingByModel: new Map(), // model_id -> ProviderModelRead[] (all providers)
    artifactContent: new Map(), // artifact_id -> text
    attempts: new Map(), // agent_run_id -> attempts[]
    error: null,
    selecting: null, // candidate id currently being selected
    startedAt: Date.now(),
  };

  let pollHandle = null;

  async function load(showSpinner) {
    if (showSpinner) mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading comparison…"]));
    try {
      state.comparison = await api.get(`/comparisons/${comparisonId}`);
      if (!state.task) {
        state.task = await api.get(`/tasks/${state.comparison.task_id}`).catch(() => null);
      }
      await hydrateCandidateExtras(state);
      draw();
      managePolling();
    } catch (err) {
      mount(root, el("div", { class: "error-banner" }, err.message || "Failed to load this comparison."));
    }
  }

  function managePolling() {
    const terminal = isTerminalPhase(state.comparison.phase);
    const withinBound = Date.now() - state.startedAt < POLL_BOUND_MS;
    if (!terminal && withinBound && pollHandle === null) {
      pollHandle = setInterval(() => load(false), POLL_INTERVAL_MS);
    }
    if ((terminal || !withinBound) && pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  function draw() {
    clear(root);
    root.appendChild(buildPage(state, comparisonId, () => load(false)));
  }

  await load(true);

  return () => {
    if (pollHandle !== null) clearInterval(pollHandle);
  };
}

async function hydrateCandidateExtras(state) {
  const candidates = state.comparison.candidates;
  await Promise.all(
    candidates.map(async (c) => {
      if (c.model_id && !state.pricingByModel.has(c.model_id)) {
        try {
          const rows = await api.get(`/models/${c.model_id}/provider-models`);
          state.pricingByModel.set(c.model_id, rows);
        } catch {
          state.pricingByModel.set(c.model_id, []);
        }
      }
      if (c.artifact_id && !state.artifactContent.has(c.artifact_id)) {
        try {
          const content = await api.getText(`/artifacts/${c.artifact_id}/content`);
          state.artifactContent.set(c.artifact_id, content);
        } catch {
          state.artifactContent.set(c.artifact_id, null);
        }
      }
      if (c.status === "failed" && c.agent_run_id && !state.attempts.has(c.agent_run_id)) {
        try {
          const attempts = await api.get(`/agent-runs/${c.agent_run_id}/attempts`);
          state.attempts.set(c.agent_run_id, attempts);
        } catch {
          state.attempts.set(c.agent_run_id, []);
        }
      }
    })
  );
}

function buildPage(state, comparisonId, refresh) {
  const { comparison, task } = state;
  const container = el("div", { class: "stack" });

  container.appendChild(
    el("div", { class: "page-header" }, [
      el("a", { href: "#/comparisons", class: "hint" }, "← All comparisons"),
      el("h1", {}, task ? task.title : "Comparison"),
      el("div", { class: "subtitle" }, task && task.description && task.description !== task.title ? task.description : `Comparison ${comparisonId}`),
    ])
  );

  if (state.error) container.appendChild(el("div", { class: "error-banner" }, state.error));

  container.appendChild(buildSummaryCard(state, comparisonId, refresh));
  container.appendChild(
    el(
      "div",
      { class: "candidate-grid" },
      comparison.candidates.map((c) => buildCandidateCard(state, c, comparisonId, refresh))
    )
  );

  return container;
}

function buildSummaryCard(state, comparisonId, refresh) {
  const { comparison } = state;
  const terminal = isTerminalPhase(comparison.phase);
  const cancellable = !terminal && !comparison.cancellation_requested_at;

  const actions = [];
  if (cancellable) {
    actions.push(
      el(
        "button",
        {
          class: "danger small",
          onclick: async (e) => {
            e.target.disabled = true;
            try {
              await api.post(`/comparisons/${comparisonId}/cancel`);
              await refresh();
            } catch (err) {
              state.error = err.message;
              e.target.disabled = false;
            }
          },
        },
        "Cancel comparison"
      )
    );
  }
  actions.push(
    el(
      "button",
      { class: "small", onclick: () => refresh() },
      "Refresh"
    )
  );

  return el("div", { class: "card" }, [
    el("div", { class: "row between" }, [
      el("div", { class: "row" }, [
        el("span", { class: phaseBadgeClass(comparison.phase) }, phaseLabel(comparison.phase)),
        !terminal ? el("span", { class: "hint" }, [el("span", { class: "spinner" }), " watching for updates…"]) : null,
        comparison.cancellation_requested_at ? el("span", { class: "hint" }, "cancellation requested") : null,
      ]),
      el("div", { class: "row" }, actions),
    ]),
    el("div", { class: "totals-bar", style: "margin-top:16px" }, [
      metric("Candidates", String(comparison.candidates.length)),
      metric("Total tokens in", formatTokens(comparison.total_tokens_in)),
      metric("Total tokens out", formatTokens(comparison.total_tokens_out)),
      metric("Total cost", `$${Number(comparison.total_cost || 0).toFixed(4)}`),
      metric("Created", formatDateTime(comparison.created_at)),
    ]),
  ]);
}

function metric(label, value) {
  return el("div", {}, [el("div", { class: "metric-label" }, label), el("div", { class: "metric-value" }, value)]);
}

function phaseBadgeClass(phase) {
  if (phase === "completed" || phase === "ready_for_selection") return "badge badge-done";
  if (phase === "failed") return "badge badge-failed";
  if (phase === "cancelled") return "badge badge-neutral";
  return "badge badge-running";
}

function buildCandidateCard(state, candidate, comparisonId, refresh) {
  const classification = resolveClassification(state, candidate);
  const isWinner = candidate.is_winner;
  const canSelect =
    state.comparison.phase === "ready_for_selection" &&
    candidate.status === "completed" &&
    candidate.artifact_hash &&
    state.comparison.status !== "completed";

  const card = el("div", { class: `candidate-card${isWinner ? " winner" : ""}` }, [
    el("div", { class: "row between" }, [
      el("div", { class: "candidate-title" }, candidate.label),
      el("span", { class: candidateStatusBadgeClass(candidate.status) }, candidateStatusLabel(candidate.status)),
    ]),
    isWinner ? el("div", { class: "selected-note" }, "✓ Selected Result") : null,
    buildAnswerBlock(state, candidate),
    buildEvidenceGrid(candidate, classification),
    buildFailureBlock(state, candidate),
    canSelect
      ? el(
          "button",
          {
            class: "primary small",
            disabled: state.selecting === candidate.id,
            onclick: async (e) => {
              state.selecting = candidate.id;
              e.target.disabled = true;
              e.target.textContent = "Selecting…";
              try {
                await api.post(`/comparisons/${comparisonId}/select-winner`, {
                  comparison_candidate_id: candidate.id,
                  artifact_hash: candidate.artifact_hash,
                });
                state.selecting = null;
                await refresh();
              } catch (err) {
                state.selecting = null;
                state.error = err.message || "Failed to select this result.";
                await refresh();
              }
            },
          },
          "Select This Result"
        )
      : null,
  ]);
  return card;
}

function buildAnswerBlock(state, candidate) {
  if (!candidate.artifact_id) {
    if (candidate.status === "failed" || candidate.status === "cancelled") {
      return el("div", { class: "candidate-answer" }, "No result was produced.");
    }
    return el("div", { class: "candidate-answer" }, "Waiting for a result…");
  }
  const content = state.artifactContent.get(candidate.artifact_id);
  if (content === undefined) return el("div", { class: "candidate-answer" }, [el("span", { class: "spinner" }), " Loading answer…"]);
  if (content === null) return el("div", { class: "candidate-answer" }, "Could not load this answer's content.");
  return el("div", { class: "candidate-answer" }, content);
}

function buildEvidenceGrid(candidate, classification) {
  return el("div", { class: "candidate-evidence" }, [
    evidence("Input tokens", formatTokens(candidate.tokens_in)),
    evidence("Output tokens", formatTokens(candidate.tokens_out)),
    evidence("Total tokens", formatTokens((candidate.tokens_in || 0) + (candidate.tokens_out || 0))),
    evidence("Latency", formatLatency(candidate.latency_ms)),
    el("div", {}, [
      el("div", { class: "metric-label" }, "Pricing"),
      el("span", { class: pricingBadgeClass(classification) }, pricingLabel(classification)),
    ]),
    evidence("Cost", formatCostForClassification(candidate.cost_amount, classification)),
  ]);
}

function evidence(label, value) {
  return el("div", {}, [el("div", { class: "metric-label" }, label), el("div", { class: "metric-value" }, value)]);
}

function buildFailureBlock(state, candidate) {
  if (candidate.status !== "failed") return null;
  const attempts = state.attempts.get(candidate.agent_run_id) || [];
  if (!attempts.length) return el("p", { class: "hint" }, "This candidate failed. No attempt detail is available yet.");
  const lines = attempts.map((a) => {
    const errMsg = a.error && (a.error.message || JSON.stringify(a.error));
    return el("div", { class: "hint" }, `Attempt ${a.attempt_number}: ${a.status}${errMsg ? ` — ${errMsg}` : ""}`);
  });
  return el("div", { class: "stack", style: "gap:4px" }, [
    el("div", { class: "metric-label" }, `Attempts (${attempts.length})`),
    ...lines,
  ]);
}

function resolveClassification(state, candidate) {
  if (!candidate.model_id) return "unknown";
  const rows = state.pricingByModel.get(candidate.model_id) || [];
  const match = rows.find((r) => r.provider_id === candidate.provider_id) || rows[0];
  return match ? match.pricing_classification : "unknown";
}
