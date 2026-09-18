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
import { renderMarkdown } from "../markdown.js";
import { openModal, closeModal } from "../modal.js";
import { buildVersionIndex, loadAgentCatalog, roleLabelFor } from "../agentDirectory.js";
import { getProjectId } from "../state.js";
import { clampIndex, hasNext, hasPrevious, nextIndex, previousIndex } from "../focusNav.js";
import {
  buildEvaluationSection,
  createEvaluationSectionState,
  loadEvaluationDefinitionCatalog,
  loadEvaluationRuns,
} from "./evaluationConfig.js";
import {
  createEvaluationPollState,
  startEvaluationPolling,
  stopEvaluationPolling,
} from "../evaluationPolling.js";

const POLL_INTERVAL_MS = 1500;
const POLL_BOUND_MS = 20 * 60 * 1000; // stop auto-polling after 20 minutes; manual refresh still works

export async function renderComparisonDetail(root, params) {
  const comparisonId = params.id;
  const state = {
    comparison: null,
    task: null,
    agentCatalog: null, // [{ agent, versions }] -- shared by candidate identity resolution AND the Evaluator Agent/Role pickers (MA6 Slice 4A), loaded once
    versionIndex: null, // agent_version_id -> { agent, version } -- Section 9 (Enhancement 1C)
    pricingByModel: new Map(), // model_id -> ProviderModelRead[] (all providers)
    artifactContent: new Map(), // artifact_id -> text
    attempts: new Map(), // agent_run_id -> attempts[]
    error: null,
    selecting: null, // candidate id currently being selected
    startedAt: Date.now(),
    // MA6 Slice 4A: the Evaluation & Analysis section's own state, kept
    // on the same page-level `state` object (not re-created per render)
    // so picker selections/in-flight status survive every redraw/poll
    // tick, exactly like `selecting`/`pricingByModel` already do above.
    // 4B will extend this with evaluationRuns/an independent poll handle
    // for RUNNING/COMPLETED/PARTIAL — not implemented yet.
    evaluation: createEvaluationSectionState(),
  };

  let pollHandle = null;
  let evaluationPollState = createEvaluationPollState();

  async function load(showSpinner) {
    if (showSpinner) mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading comparison…"]));
    try {
      state.comparison = await api.get(`/comparisons/${comparisonId}`);
      if (!state.task) {
        state.task = await api.get(`/tasks/${state.comparison.task_id}`).catch(() => null);
      }
      if (!state.agentCatalog) {
        state.agentCatalog = await getProjectId().then(loadAgentCatalog).catch(() => []);
        state.versionIndex = buildVersionIndex(state.agentCatalog);
      }
      if (!state.evaluation.loaded) {
        await loadEvaluationSectionData(state);
      }
      // MA6.4B: Load existing evaluation runs on page entry
      await loadEvaluationRuns(state, comparisonId);

      await hydrateCandidateExtras(state);
      draw();
      managePolling();
      manageEvaluationPolling();
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

  // MA6.4B: Manage evaluation runs polling independently
  function manageEvaluationPolling() {
    // Check if any evaluation runs are non-terminal
    const evalState = state.evaluation;
    const hasNonTerminalRuns =
      evalState.evaluationRuns &&
      evalState.evaluationRuns.candidates &&
      evalState.evaluationRuns.candidates.some((c) =>
        (c.evaluation_runs || []).some(
          (r) => r.status !== "completed" && r.status !== "failed" && r.status !== "cancelled"
        )
      );

    if (hasNonTerminalRuns && !evaluationPollState.isPolling) {
      startEvaluationPolling(evaluationPollState, comparisonId, () => {
        // When polling updates, redraw the evaluation section
        draw();
      });
    } else if (!hasNonTerminalRuns && evaluationPollState.isPolling) {
      stopEvaluationPolling(evaluationPollState);
    }
  }

  function draw() {
    clear(root);
    root.appendChild(buildPage(state, comparisonId, () => load(false)));
  }

  await load(true);

  return () => {
    if (pollHandle !== null) clearInterval(pollHandle);
    stopEvaluationPolling(evaluationPollState);
  };
}

// MA6 Slice 4A: loads the Evaluation & Analysis section's own data once
// (Evaluation Definitions + their versions, the model catalog, providers
// for the evaluator model picker's provider filter) -- guarded by
// state.evaluation.loaded the same way state.agentCatalog above is loaded
// exactly once, never re-fetched on every poll tick. A failure here must
// never break the rest of the page (candidate cards, polling, Focus View
// all stay fully functional) -- it only degrades the Evaluation & Analysis
// section to its own error banner.
async function loadEvaluationSectionData(state) {
  try {
    const projectId = await getProjectId();
    const [definitionCatalog, models, providers] = await Promise.all([
      loadEvaluationDefinitionCatalog(projectId),
      api.get("/models"),
      api.get("/providers").catch(() => []),
    ]);
    state.evaluation.definitionCatalog = definitionCatalog;
    state.evaluation.models = models;
    state.evaluation.providers = providers;
  } catch (err) {
    state.evaluation.loadError = (err && err.message) || "Failed to load Evaluation & Analysis data.";
  } finally {
    state.evaluation.loaded = true;
  }
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
      comparison.candidates.map((c, index) => buildCandidateCard(state, c, comparisonId, refresh, index))
    )
  );
  // MA6 Slice 4A: Evaluation & Analysis, immediately below the candidate
  // grid -- purely additive, never touches the candidate cards/summary
  // above. See frontend/assets/js/pages/evaluationConfig.js.
  container.appendChild(buildEvaluationSection(state, comparisonId));

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

function buildCandidateCard(state, candidate, comparisonId, refresh, index) {
  const { classification, modelName } = resolveModelInfo(state, candidate);
  const { agentName, roleLabel } = resolveAgentRole(state, candidate);
  const isWinner = candidate.is_winner;
  const content = candidate.artifact_id ? state.artifactContent.get(candidate.artifact_id) : undefined;
  const canSelect =
    state.comparison.phase === "ready_for_selection" &&
    candidate.status === "completed" &&
    candidate.artifact_hash &&
    state.comparison.status !== "completed";

  const actions = [
    // "View Large" (Final Readability Enhancement) cleanly supersedes the
    // old "Expand Answer" -- it shows everything Expand Answer did
    // (agent/role/model/status/answer/tokens/latency/pricing/cost) PLUS
    // failure info, PLUS Select This Result, PLUS Previous/Next
    // navigation across every candidate. Keeping a separate "Expand
    // Answer" button alongside it would just be two buttons opening
    // near-identical modals -- removed rather than duplicated. Shown on
    // every card (not only successful ones) so a failed/in-progress
    // candidate can also be read large.
    el(
      "button",
      {
        class: "small",
        "aria-label": `View ${agentName || candidate.label} large`,
        onclick: () => openCandidateFocusView(state, comparisonId, refresh, index),
      },
      "⤢ View Large"
    ),
  ];
  if (canSelect) {
    actions.push(
      el(
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
    );
  }

  const card = el("div", { class: `candidate-card${isWinner ? " winner" : ""}` }, [
    el("div", { class: "row between" }, [
      buildCandidateIdentity(agentName, roleLabel, modelName, candidate.label),
      el("span", { class: candidateStatusBadgeClass(candidate.status) }, candidateStatusLabel(candidate.status)),
    ]),
    isWinner ? el("div", { class: "selected-note" }, "✓ Selected Result") : null,
    buildAnswerBlock(candidate, content),
    buildEvidenceGrid(candidate, classification),
    buildFailureBlock(state, candidate),
    actions.length ? el("div", { class: "row" }, actions) : null,
  ]);
  return card;
}

// Model output is untrusted content -- the raw `content` string is NEVER
// assigned to innerHTML directly. It only ever passes through
// renderMarkdown() first, whose output is safe by construction (see
// frontend/assets/js/markdown.js): escaped text plus a small fixed set of
// tags the renderer builds itself, never raw HTML passthrough.
function buildRenderedAnswer(content) {
  const wrapper = el("div", { class: "rendered-answer" });
  wrapper.innerHTML = renderMarkdown(content);
  return wrapper;
}

function buildAnswerBlock(candidate, content) {
  return el("div", { class: "candidate-answer" }, [buildAnswerBlockContent(candidate, content)]);
}

// -- Candidate Focus View (MA5-UI Final Readability Enhancement) ------------
// A near-full-screen modal (reusing frontend/assets/js/modal.js's existing
// overlay -- never a second modal system) showing one candidate's complete
// evidence with a dominant, comfortably wide answer area, plus Previous/
// Next controls to move through every candidate without closing it. Never
// touches comparison/selection state itself -- Select This Result inside
// it calls the exact same POST /comparisons/{id}/select-winner as the
// card's own button, then closes and lets the underlying page refresh.

function openCandidateFocusView(state, comparisonId, refresh, startIndex) {
  const container = el("div", { class: "focus-view" });
  let index = clampIndex(startIndex, state.comparison.candidates.length);

  function renderAt(newIndex) {
    index = clampIndex(newIndex, state.comparison.candidates.length);
    clear(container);
    container.appendChild(buildFocusContent(state, comparisonId, refresh, index, renderAt));
  }

  renderAt(index);
  openModal(container, { label: "Candidate detail", size: "large" });
}

function buildFocusContent(state, comparisonId, refresh, index, goTo) {
  const candidates = state.comparison.candidates;
  const candidate = candidates[index];
  const { classification, modelName } = resolveModelInfo(state, candidate);
  const { agentName, roleLabel } = resolveAgentRole(state, candidate);
  const content = candidate.artifact_id ? state.artifactContent.get(candidate.artifact_id) : undefined;
  const canSelect =
    state.comparison.phase === "ready_for_selection" &&
    candidate.status === "completed" &&
    candidate.artifact_hash &&
    state.comparison.status !== "completed";

  const closeBtn = el("button", { class: "modal-close-btn", "aria-label": "Close", onclick: () => closeModal() }, "×");
  const subtitleParts = [roleLabel, modelName, `Status: ${candidateStatusLabel(candidate.status)}`].filter(Boolean);
  const header = el("div", { class: "modal-header" }, [
    el("div", {}, [
      el("h2", {}, agentName || candidate.label),
      el("div", { class: "modal-subtitle" }, subtitleParts.join(" · ")),
      candidate.is_winner ? el("div", { class: "selected-note" }, "✓ Selected Result") : null,
    ]),
    closeBtn,
  ]);

  const nav = el("div", { class: "focus-nav" }, [
    el(
      "button",
      { class: "small", disabled: !hasPrevious(index), onclick: () => goTo(previousIndex(index)) },
      "← Previous"
    ),
    el("span", {}, `Candidate ${index + 1} of ${candidates.length}`),
    el(
      "button",
      { class: "small", disabled: !hasNext(index, candidates.length), onclick: () => goTo(nextIndex(index)) },
      "Next →"
    ),
  ]);

  const metaRow = el("div", { class: "candidate-evidence", style: "margin:6px 0 14px" }, [
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

  const answerBox = el("div", { class: "focus-answer-wrap" }, [
    el("div", { class: "candidate-answer answer-focus" }, [buildAnswerBlockContent(candidate, content)]),
  ]);

  const failureBlock = buildFailureBlock(state, candidate);

  const selectRow = canSelect
    ? el(
        "div",
        { class: "row", style: "margin-top:14px" },
        el(
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
              } catch (err) {
                state.selecting = null;
                state.error = err.message || "Failed to select this result.";
              }
              closeModal();
              await refresh();
            },
          },
          "Select This Result"
        )
      )
    : null;

  return el("div", { class: "focus-content" }, [header, nav, metaRow, answerBox, failureBlock, selectRow]);
}

// Shared by the card's normal-size answer box and the Focus View's
// dominant one -- same safe-Markdown path (frontend/assets/js/markdown.js),
// never a second implementation.
function buildAnswerBlockContent(candidate, content) {
  if (!candidate.artifact_id) {
    if (candidate.status === "failed" || candidate.status === "cancelled") {
      return document.createTextNode("No result was produced.");
    }
    return document.createTextNode("Waiting for a result…");
  }
  if (content === undefined) return el("span", {}, [el("span", { class: "spinner" }), " Loading answer…"]);
  if (content === null) return document.createTextNode("Could not load this answer's content.");
  return buildRenderedAnswer(content);
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

function resolveModelInfo(state, candidate) {
  if (!candidate.model_id) return { classification: "unknown", modelName: null };
  const rows = state.pricingByModel.get(candidate.model_id) || [];
  const match = rows.find((r) => r.provider_id === candidate.provider_id) || rows[0];
  return {
    classification: match ? match.pricing_classification : "unknown",
    modelName: match ? match.provider_model_id : null,
  };
}

// "Role" is AgentVersion itself -- see frontend/assets/js/agentDirectory.js
// for the architecture note. Resolved via agent_version_id (always present
// on a configured candidate) against the project's Agent/AgentVersion
// directory, never by parsing the candidate's own label string apart.
// Falls back to the raw label for a candidate this lookup can't resolve
// (e.g. one created before this enhancement, or via a script/test with an
// arbitrary label) so nothing ever renders blank.
function resolveAgentRole(state, candidate) {
  const resolved = state.versionIndex ? state.versionIndex.get(candidate.agent_version_id) : null;
  if (resolved) {
    return { agentName: resolved.agent.name, roleLabel: roleLabelFor(resolved.version) };
  }
  return { agentName: candidate.label, roleLabel: null };
}

function buildCandidateIdentity(agentName, roleLabel, modelName, fallbackLabel) {
  if (!roleLabel) {
    // Resolution failed -- show the raw label rather than a broken/blank header.
    return el("div", { class: "candidate-title" }, fallbackLabel);
  }
  return el("div", {}, [
    el("div", { class: "candidate-title" }, agentName),
    el("div", { class: "hint" }, roleLabel),
    el("div", { class: "candidate-model-name" }, modelName || fallbackLabel),
  ]);
}
