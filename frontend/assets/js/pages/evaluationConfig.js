// Evaluation & Analysis section — MA6 Slice 4A (UX-refined). A collapsible
// section appended below the existing candidate grid on the Comparison
// Detail page (frontend/assets/js/pages/comparisonDetail.js), never a
// redesign of it. Three primary controls only -- Evaluation / Evaluator /
// Model -- with everything else (Evaluation Method, Definition Version,
// Evaluator Role) tucked behind a second, nested "Advanced Settings"
// disclosure, so normal operation never has to look at six controls at
// once. The underlying configuration/request shape is unchanged from the
// original 4A cut -- this file only changed which control renders where
// and what its visible label says; app.schemas.comparisons.
// ComparisonEvaluationRequest's fields (evaluation_definition_version_id,
// method, evaluator_agent_version_id, evaluator_model_policy_override) are
// still built by the exact same frontend/assets/js/evaluation.js.
//
// Reuses, verbatim, the same pickers/filters/formatting Ask Agents already
// established:
//   - Evaluator / Evaluator Role: frontend/assets/js/agentDirectory.js
//     (loadAgentCatalog/activeVersionsOf/roleLabelFor) -- "Role" is
//     AgentVersion itself, same architecture note as candidates.
//   - Model: frontend/assets/js/modelFilter.js + frontend/assets/js/
//     modelDeveloper.js + frontend/assets/js/format.js's pricing helpers --
//     the same FREE/PAID/UNKNOWN-driven catalog Ask Agents uses,
//     deliberately lighter here (no developer sidebar, no multi-candidate
//     "active candidate" banner) since this section configures exactly one
//     evaluator, not a list of candidates.
//
// All pure request/gating/summary/default-resolution logic lives in
// frontend/assets/js/evaluation.js (unit-tested there) -- this module is
// DOM-building only, exercised live (see the MA6.4A UAT instructions),
// exactly like frontend/assets/js/pages/ask.js relative to
// frontend/assets/js/candidates.js.
//
// Nothing here ever calls POST /comparisons/{id}/select-winner, and
// Analyze Results only ever fires from the button's own click handler --
// never on section load, never on a picker change (Section: MA6
// non-negotiable "no automatic evaluation").

import { api } from "../api.js";
import { el, clear } from "../dom.js";
import { activeVersionsOf, roleLabelFor } from "../agentDirectory.js";
import { PRICING_FILTER_ALL, boundedResults, filterModels, uniqueProviderIds } from "../modelFilter.js";
import { developerLabel, extractDeveloperKey } from "../modelDeveloper.js";
import { formatPriceSummary, pricingBadgeClass, pricingLabel } from "../format.js";
import {
  EVALUATION_METHOD_AGENT_EVALUATOR,
  EVALUATION_METHOD_DETERMINISTIC,
  EVAL_PHASE_STARTING,
  buildAnalyzeRequest,
  createEvaluationConfig,
  describeMissingRequirement,
  evaluationSectionPhase,
  isAgentEvaluatorMethod,
  isAnalyzeReady,
  resolveUnambiguousVersionId,
  selectEvaluationDefinition,
  selectEvaluatorAgent,
  summarizeAnalyzeResults,
} from "../evaluation.js";

const MODEL_VISIBLE_STEP = 12;
const PRICING_TABS = [
  { value: "free", label: "FREE" },
  { value: "paid", label: "PAID" },
  { value: "unknown", label: "UNKNOWN" },
  { value: PRICING_FILTER_ALL, label: "ALL" },
];

// Fetches every Evaluation Definition in the project plus each one's
// versions -- structurally identical to agentDirectory.js's
// loadAgentCatalog(projectId), applied to Evaluation Definitions instead
// of Agents. Real fetches (via api.js), so exercised live rather than
// unit-tested, same convention as loadAgentCatalog itself.
export async function loadEvaluationDefinitionCatalog(projectId) {
  const definitions = await api.get(`/evaluation-definitions?project_id=${encodeURIComponent(projectId)}`);
  return Promise.all(
    definitions.map(async (definition) => {
      const versions = await api.get(`/evaluation-definitions/${definition.id}/versions`);
      return { definition, versions };
    })
  );
}

export function createEvaluationSectionState() {
  return {
    loaded: false,
    loadError: null,
    definitionCatalog: [], // [{ definition, versions }]
    models: [],
    providers: [],
    config: createEvaluationConfig(),
    submitting: false,
    lastResult: null,
    lastError: null,
    modelSearch: "",
    modelPricingFilter: "free",
    modelProviderFilter: "",
    modelVisibleCount: MODEL_VISIBLE_STEP,
    // Persisted so a manual expand survives candidate-polling's full
    // section rebuild (frontend/assets/js/pages/comparisonDetail.js's
    // load()/draw() tears down and recreates this whole section on every
    // poll tick while candidates are still in flight) -- without this, a
    // native <details> element re-created from scratch every ~1.5s would
    // silently snap back closed moments after the operator opened it.
    advancedSettingsOpen: false,
    // MA6.4B: Evaluation runs and polling state
    evaluationRuns: null, // Loaded from GET /comparisons/{id}/evaluations
    evaluationRunsLoadError: null,
    evaluationRunsPolling: null, // Will be set by comparisonDetail.js
  };
}

// pageState: the full Comparison Detail page state (has .comparison,
// .agentCatalog, .evaluation) -- this function only ever reads
// pageState.comparison.candidates (for safe-reason candidate labels) and
// pageState.agentCatalog (the same catalog already loaded for candidate
// identity resolution, never re-fetched here) alongside its own
// pageState.evaluation sub-state. Returns a DOM node; manages its own
// internal re-rendering so a picker change never has to redraw candidate
// cards.
export function buildEvaluationSection(pageState, comparisonId) {
  const details = el("details", { class: "card eval-section", open: true });
  const body = el("div", { class: "eval-body" });

  function redraw() {
    clear(body);
    body.appendChild(buildBody(pageState, comparisonId, redraw));
  }

  details.appendChild(el("summary", {}, "Evaluation & Analysis"));
  details.appendChild(body);
  redraw();
  return details;
}

function buildBody(pageState, comparisonId, redraw) {
  const evalState = pageState.evaluation;

  if (evalState.loadError) {
    return el("div", { class: "error-banner" }, evalState.loadError);
  }

  const eligibleDefinitions = evalState.definitionCatalog.filter(
    (entry) => activeVersionsOf(entry).length > 0
  );

  if (!eligibleDefinitions.length) {
    return el("div", { class: "empty-state" }, [
      el("div", { class: "icon" }, "📋"),
      el("p", {}, "No active Evaluation Definition is available yet."),
    ]);
  }

  // MA6.4B: If evaluation runs exist, show them (progress or matrix) instead
  // of the configuration UI. This handles both page-entry recovery and
  // post-Analyze flow.
  const runsDisplay = buildEvaluationRunsDisplay(pageState, comparisonId);
  if (runsDisplay) {
    // Runs exist; show them. However, also keep configuration visible for
    // running evaluations so the operator can see what was submitted.
    const config = evalState.config;
    const definitionContext = syncDefinitionContext(config, eligibleDefinitions);
    const agentCatalog = (pageState.agentCatalog || []).filter((entry) => activeVersionsOf(entry).length > 0);
    const evaluatorContext = isAgentEvaluatorMethod(config.method) ? syncEvaluatorContext(config, agentCatalog) : null;

    return el("div", { class: "stack" }, [
      runsDisplay,
      el("hr", {}),
      el("details", { class: "eval-config-collapse", open: false }, [
        el("summary", {}, "Evaluation Configuration"),
        el("div", { class: "stack", style: "gap:12px;margin-top:12px" }, [
          el(
            "div",
            { class: "candidate-row-controls" },
            buildPrimaryFields(pageState, eligibleDefinitions, definitionContext, agentCatalog, evaluatorContext, redraw)
          ),
          buildAdvancedSettings(pageState, definitionContext, agentCatalog, evaluatorContext, redraw),
          buildRunBar(pageState, comparisonId, redraw),
        ]),
      ]),
    ]);
  }

  const config = evalState.config;
  // Sync once per render -- both the primary controls and Advanced
  // Settings read these same resolved values, never re-deriving them
  // independently (Section: MA6.4A UX refinement keeps a single source of
  // truth for "which Definition/Agent is selected, and which Version/Role
  // that unambiguously resolves to, if any").
  const definitionContext = syncDefinitionContext(config, eligibleDefinitions);
  const agentCatalog = (pageState.agentCatalog || []).filter((entry) => activeVersionsOf(entry).length > 0);
  const evaluatorContext = isAgentEvaluatorMethod(config.method) ? syncEvaluatorContext(config, agentCatalog) : null;

  const wrapper = el("div", { class: "stack" }, [
    el(
      "p",
      { class: "hint" },
      "Choose an Evaluation and, for Agent Evaluator, an Evaluator and Model — evaluation only starts when you click Analyze Results."
    ),
    el(
      "div",
      { class: "candidate-row-controls" },
      buildPrimaryFields(pageState, eligibleDefinitions, definitionContext, agentCatalog, evaluatorContext, redraw)
    ),
    buildAdvancedSettings(pageState, definitionContext, agentCatalog, evaluatorContext, redraw),
    buildRunBar(pageState, comparisonId, redraw),
    buildResultBlock(pageState, evalState),
  ]);
  return wrapper;
}

// -- config sync: resolve/validate the two-level Definition->Version and
// Agent->Role pairs against whatever catalogs are currently loaded,
// applying frontend/assets/js/evaluation.js's resolveUnambiguousVersionId
// rule (auto-resolve only when exactly one ACTIVE option exists; leave
// null, never guessed, when more than one requires an operator choice in
// Advanced Settings). -----------------------------------------------------

function syncDefinitionContext(config, eligibleDefinitions) {
  const selectedDefEntry =
    eligibleDefinitions.find((entry) => entry.definition.id === config.evaluationDefinitionId) ||
    eligibleDefinitions[0];
  if (config.evaluationDefinitionId !== selectedDefEntry.definition.id) {
    config.evaluationDefinitionId = selectedDefEntry.definition.id;
  }
  const activeVersions = activeVersionsOf(selectedDefEntry);
  config.evaluationDefinitionVersionId = resolveUnambiguousVersionId(
    activeVersions,
    config.evaluationDefinitionVersionId
  );
  return { selectedDefEntry, activeVersions };
}

function syncEvaluatorContext(config, agentCatalog) {
  if (!agentCatalog.length) return { selectedEntry: null, versions: [] };
  const selectedEntry =
    agentCatalog.find((entry) => entry.agent.id === config.evaluatorAgentId) || agentCatalog[0];
  if (config.evaluatorAgentId !== selectedEntry.agent.id) {
    config.evaluatorAgentId = selectedEntry.agent.id;
  }
  const versions = activeVersionsOf(selectedEntry);
  config.evaluatorAgentVersionId = resolveUnambiguousVersionId(versions, config.evaluatorAgentVersionId);
  return { selectedEntry, versions };
}

// -- primary controls: Evaluation / Evaluator / Model ---------------------

function buildPrimaryFields(pageState, eligibleDefinitions, definitionContext, agentCatalog, evaluatorContext, redraw) {
  const config = pageState.evaluation.config;

  const definitionSelect = el(
    "select",
    {
      onchange: (e) => {
        const entry = eligibleDefinitions.find((en) => en.definition.id === e.target.value);
        selectEvaluationDefinition(config, e.target.value, activeVersionsOf(entry));
        redraw();
      },
    },
    eligibleDefinitions.map((entry) =>
      el(
        "option",
        { value: entry.definition.id, selected: entry.definition.id === definitionContext.selectedDefEntry.definition.id },
        entry.definition.name
      )
    )
  );

  const fields = [el("div", {}, [el("label", {}, "Evaluation"), definitionSelect])];

  if (isAgentEvaluatorMethod(config.method)) {
    fields.push(...buildEvaluatorPrimaryFields(pageState, agentCatalog, evaluatorContext, redraw));
  }

  return fields;
}

function buildEvaluatorPrimaryFields(pageState, agentCatalog, evaluatorContext, redraw) {
  const config = pageState.evaluation.config;

  if (!agentCatalog.length) {
    return [
      el("div", {}, [
        el("label", {}, "Evaluator"),
        el("p", { class: "hint" }, "No published Agent is available yet."),
      ]),
    ];
  }

  const agentSelect = el(
    "select",
    {
      onchange: (e) => {
        const entry = agentCatalog.find((en) => en.agent.id === e.target.value);
        selectEvaluatorAgent(config, e.target.value, activeVersionsOf(entry));
        redraw();
      },
    },
    agentCatalog.map((entry) =>
      el(
        "option",
        { value: entry.agent.id, selected: entry.agent.id === evaluatorContext.selectedEntry.agent.id },
        entry.agent.name
      )
    )
  );

  return [
    el("div", {}, [el("label", {}, "Evaluator"), agentSelect]),
    el("div", {}, [el("label", {}, "Model"), buildEvaluatorModelPicker(pageState, redraw)]),
  ];
}

// -- Advanced Settings: Evaluation Method / Definition Version / Evaluator
// Role -- collapsed by default (native <details>, no new JS state), so
// normal operation never has to look at them. ------------------------------

function buildAdvancedSettings(pageState, definitionContext, agentCatalog, evaluatorContext, redraw) {
  const evalState = pageState.evaluation;
  const config = evalState.config;
  const details = el("details", {
    class: "eval-advanced",
    open: evalState.advancedSettingsOpen,
    ontoggle: (e) => {
      // Remember the operator's own choice only -- never forces a
      // redraw, since the native <details> already reflects its own
      // open/closed state immediately; this just survives it across the
      // next full section rebuild (see createEvaluationSectionState's
      // comment on advancedSettingsOpen above).
      evalState.advancedSettingsOpen = e.target.open;
    },
  });
  details.appendChild(el("summary", {}, "Advanced Settings"));

  const methodSelect = el(
    "select",
    {
      onchange: (e) => {
        config.method = e.target.value;
        redraw();
      },
    },
    [
      el(
        "option",
        { value: EVALUATION_METHOD_DETERMINISTIC, selected: config.method === EVALUATION_METHOD_DETERMINISTIC },
        "Deterministic"
      ),
      el(
        "option",
        { value: EVALUATION_METHOD_AGENT_EVALUATOR, selected: config.method === EVALUATION_METHOD_AGENT_EVALUATOR },
        "Agent Evaluator"
      ),
    ]
  );

  const versionSelect = el(
    "select",
    {
      onchange: (e) => {
        config.evaluationDefinitionVersionId = e.target.value;
        redraw();
      },
    },
    definitionContext.activeVersions.map((v) =>
      el(
        "option",
        { value: v.id, selected: v.id === config.evaluationDefinitionVersionId },
        definitionContext.activeVersions.length > 1
          ? `v${v.version}${v.description ? ` — ${v.description}` : ""}`
          : `v${v.version}`
      )
    )
  );

  const fields = [
    el("div", {}, [el("label", {}, "Evaluation Method"), methodSelect]),
    el("div", {}, [el("label", {}, "Definition Version"), versionSelect]),
  ];

  if (isAgentEvaluatorMethod(config.method) && evaluatorContext && evaluatorContext.selectedEntry) {
    const roleSelect = el(
      "select",
      {
        disabled: evaluatorContext.versions.length === 0,
        onchange: (e) => {
          config.evaluatorAgentVersionId = e.target.value;
          redraw();
        },
      },
      evaluatorContext.versions.map((v) =>
        el(
          "option",
          { value: v.id, selected: v.id === config.evaluatorAgentVersionId },
          evaluatorContext.versions.length > 1 ? `${roleLabelFor(v)} (v${v.version})` : roleLabelFor(v)
        )
      )
    );
    fields.push(el("div", {}, [el("label", {}, "Evaluator Role"), roleSelect]));
  }

  details.appendChild(el("div", { class: "candidate-row-controls" }, fields));
  return details;
}

// -- evaluator model picker: same filtering/pricing behavior as Ask
// Agents' model workspace (frontend/assets/js/pages/ask.js), deliberately
// lighter (single selection target, no developer sidebar) since this
// section configures one evaluator, not a list of candidates. -----------

function buildEvaluatorModelPicker(pageState, redraw) {
  const evalState = pageState.evaluation;
  const config = evalState.config;

  if (!evalState.models.length) {
    return el("p", { class: "hint" }, "No models available yet — connect a provider on the Models page first.");
  }

  const providerIds = uniqueProviderIds(evalState.models);
  const providerNameById = new Map(evalState.providers.map((p) => [p.id, p.name]));

  const searchInput = el("input", {
    type: "text",
    placeholder: "Search evaluator models…",
    oninput: (e) => {
      evalState.modelSearch = e.target.value;
      evalState.modelVisibleCount = MODEL_VISIBLE_STEP;
      redraw();
    },
  });
  searchInput.value = evalState.modelSearch;

  const pricingPills = el(
    "div",
    { class: "filter-pills" },
    PRICING_TABS.map((tab) =>
      el(
        "button",
        {
          type: "button",
          class: `filter-pill${evalState.modelPricingFilter === tab.value ? " active" : ""}`,
          onclick: () => {
            evalState.modelPricingFilter = tab.value;
            evalState.modelVisibleCount = MODEL_VISIBLE_STEP;
            redraw();
          },
        },
        tab.label
      )
    )
  );

  const filterChildren = [searchInput, pricingPills];
  if (providerIds.length > 1) {
    filterChildren.push(
      el(
        "select",
        {
          onchange: (e) => {
            evalState.modelProviderFilter = e.target.value;
            evalState.modelVisibleCount = MODEL_VISIBLE_STEP;
            redraw();
          },
        },
        [
          el("option", { value: "" }, "All providers"),
          ...providerIds.map((id) =>
            el(
              "option",
              { value: id, selected: id === evalState.modelProviderFilter },
              providerNameById.get(id) || "Unknown provider"
            )
          ),
        ]
      )
    );
  }

  const filtered = filterModels(evalState.models, {
    query: evalState.modelSearch,
    pricing: evalState.modelPricingFilter,
    providerId: evalState.modelProviderFilter,
  });
  const { visible, remaining } = boundedResults(filtered, evalState.modelVisibleCount);

  const resultsChildren = [
    el("div", { class: "hint" }, `${filtered.length} model${filtered.length === 1 ? "" : "s"} match`),
    el(
      "div",
      { class: "model-picker" },
      visible.map((model) => buildEvaluatorModelRow(model, config, redraw))
    ),
  ];
  if (remaining > 0) {
    resultsChildren.push(
      el(
        "button",
        {
          class: "small",
          type: "button",
          onclick: () => {
            evalState.modelVisibleCount += MODEL_VISIBLE_STEP;
            redraw();
          },
        },
        `Show more (${remaining} remaining)`
      )
    );
  }

  return el("div", { class: "stack", style: "gap:10px" }, [
    el("div", { class: "filter-bar" }, filterChildren),
    el("div", { class: "stack", style: "gap:8px" }, resultsChildren),
  ]);
}

function buildEvaluatorModelRow(model, config, redraw) {
  const disabled = model.model_status !== "active";
  const selected = config.evaluatorModelId === model.id;
  return el(
    "button",
    {
      type: "button",
      class: `model-option${selected ? " checked" : ""}${disabled ? " disabled" : ""}`,
      disabled,
      onclick: () => {
        // Same one-model-per-slot toggle as Ask Agents' candidate model
        // picker (frontend/assets/js/pages/ask.js) -- clicking the
        // already-selected model unassigns it. Assigning the same model
        // here never conflicts with it also being assigned to any
        // candidate or to a different evaluator Agent/Role elsewhere --
        // model identity is independent of Agent/Role identity throughout.
        config.evaluatorModelId = config.evaluatorModelId === model.id ? null : model.id;
        redraw();
      },
    },
    [
      el("div", { class: "model-name" }, model.canonical_model_id),
      el(
        "div",
        { class: "model-meta" },
        [developerLabel(extractDeveloperKey(model)), disabled ? "unavailable" : null].filter(Boolean).join(" · ")
      ),
      el("div", { class: "model-meta" }, [
        el("span", { class: pricingBadgeClass(model.pricing_classification) }, pricingLabel(model.pricing_classification)),
        ` ${formatPriceSummary(model.pricing_classification, model.cost_input_per_mtok, model.cost_output_per_mtok)}`,
      ]),
    ]
  );
}

// -- run bar / result -----------------------------------------------------------

function buildRunBar(pageState, comparisonId, redraw) {
  const evalState = pageState.evaluation;
  const ready = isAnalyzeReady(evalState.config);
  const phase = evaluationSectionPhase(evalState.config, { submitting: evalState.submitting });

  const button = el(
    "button",
    {
      class: "primary",
      disabled: !ready || evalState.submitting,
      onclick: () => runAnalyze(pageState, comparisonId, redraw),
    },
    phase === EVAL_PHASE_STARTING ? "Starting…" : "Analyze Results"
  );

  const hint =
    phase === EVAL_PHASE_STARTING
      ? "Requesting evaluation…"
      : describeMissingRequirement(evalState.config) ||
        "Ready — evaluation only starts when you click Analyze Results.";

  return el("div", { class: "row between", style: "margin-top:4px" }, [
    el("span", { class: "hint" }, hint),
    button,
  ]);
}

async function runAnalyze(pageState, comparisonId, redraw) {
  const evalState = pageState.evaluation;
  if (evalState.submitting || !isAnalyzeReady(evalState.config)) return;

  evalState.submitting = true;
  evalState.lastError = null;
  evalState.lastResult = null;
  redraw();

  try {
    const body = buildAnalyzeRequest(evalState.config);
    const response = await api.post(`/comparisons/${comparisonId}/evaluations`, body);
    evalState.lastResult = summarizeAnalyzeResults(response.results);

    // MA6.4B: After POST, immediately load and display evaluation runs
    await loadEvaluationRuns(pageState, comparisonId);
    redraw();

    // Start polling if runs are non-terminal (will be managed by comparisonDetail.js)
  } catch (err) {
    evalState.lastError = (err && err.message) || "Failed to start evaluation.";
  } finally {
    evalState.submitting = false;
    redraw();
  }
}

function buildResultBlock(pageState, evalState) {
  if (evalState.lastError) {
    return el("div", { class: "error-banner" }, evalState.lastError);
  }
  if (!evalState.lastResult) return null;

  const { headline, counts, details } = evalState.lastResult;
  const candidatesById = new Map((pageState.comparison.candidates || []).map((c) => [c.id, c]));

  const noteParts = [];
  if (counts.skipped) noteParts.push(`${counts.skipped} skipped`);
  if (counts.failed) noteParts.push(`${counts.failed} failed`);

  const reasonLines = details
    .filter((d) => (d.status === "skipped" || d.status === "failed") && d.reason)
    .map((d) => {
      const candidate = candidatesById.get(d.comparison_candidate_id);
      const label = candidate ? candidate.label : d.comparison_candidate_id;
      return el("div", { class: "hint" }, `${label}: ${d.reason}`);
    });

  return el("div", { class: "stack", style: "gap:4px" }, [
    el("p", {}, headline),
    noteParts.length ? el("p", { class: "hint" }, noteParts.join(" · ")) : null,
    ...reasonLines,
  ]);
}

// -- MA6.4B: Evaluation Runs Loading & Display -----------

export async function loadEvaluationRuns(pageState, comparisonId) {
  const evalState = pageState.evaluation;
  try {
    const data = await api.get(`/comparisons/${comparisonId}/evaluations`);
    evalState.evaluationRuns = data;
    evalState.evaluationRunsLoadError = null;
  } catch (err) {
    evalState.evaluationRunsLoadError = (err && err.message) || "Failed to load evaluation runs.";
  }
}

function buildEvaluationRunsDisplay(pageState, comparisonId) {
  const evalState = pageState.evaluation;

  if (evalState.evaluationRunsLoadError) {
    return el("div", { class: "error-banner" }, evalState.evaluationRunsLoadError);
  }

  if (!evalState.evaluationRuns) {
    return null; // No runs yet
  }

  const { candidates: candidates_from_api } = evalState.evaluationRuns;
  if (!candidates_from_api || candidates_from_api.length === 0) {
    return null;
  }

  // Build candidate-id-to-runs mapping
  const candidatesById = new Map((pageState.comparison.candidates || []).map((c) => [c.id, c]));
  const candidateRunsMap = new Map();
  for (const cand of candidates_from_api) {
    candidateRunsMap.set(cand.comparison_candidate_id, cand.evaluation_runs || []);
  }

  // Get the most recent evaluation configuration (all runs should be for
  // the same definition version if they're part of one evaluation batch;
  // for 4B, we only show one coherent matrix at a time)
  const allRuns = [];
  for (const runs of candidateRunsMap.values()) {
    allRuns.push(...runs);
  }

  if (allRuns.length === 0) {
    return null;
  }

  // Use the first run to extract definition/evaluator info
  const mostRecentRun = allRuns[0];
  const definitionVersionId = mostRecentRun.evaluation_definition_version_id;
  const evaluatorAgentVersionId = mostRecentRun.evaluator_agent_version_id;

  // Find the definition entry in catalog to get criteria ordering
  let criteriaOrder = [];
  for (const defEntry of evalState.definitionCatalog || []) {
    for (const version of defEntry.versions || []) {
      if (version.id === definitionVersionId) {
        criteriaOrder = (version.criteria || []).slice().sort((a, b) => a.order_index - b.order_index);
        break;
      }
    }
  }

  // Show progress if any run is non-terminal
  const runProgresses = pageState.comparison.candidates
    .map((candidate) => {
      const runs = candidateRunsMap.get(candidate.id) || [];
      const latestRun = runs.length > 0 ? runs[runs.length - 1] : null;
      const status = latestRun ? deriveCandidateStatus(latestRun) : null;
      return { candidate, status, run: latestRun };
    })
    .filter((p) => p.run !== null);

  const hasRunning = runProgresses.some((p) => p.status === "Evaluating");
  const numCompleted = runProgresses.filter((p) => p.status === "Completed").length;

  const progressSection = buildEvaluationProgressSection(runProgresses, numCompleted, hasRunning);
  const matrixSection = buildEvaluationMatrixSection(
    runProgresses,
    criteriaOrder,
    mostRecentRun,
    evalState.definitionCatalog,
    pageState.agentCatalog
  );

  return el("div", { class: "stack", style: "gap:16px" }, [
    progressSection,
    matrixSection,
  ]);
}

function deriveCandidateStatus(run) {
  if (!run) return null;
  switch (run.status) {
    case "running":
      return "Evaluating";
    case "completed":
      return "Completed";
    case "failed":
      return "Failed";
    case "cancelled":
      return "Cancelled";
    default:
      return null;
  }
}

function buildEvaluationProgressSection(runProgresses, numCompleted, hasRunning) {
  const totalCandidates = runProgresses.length;
  const progressItems = runProgresses.map((p) => {
    const indicator = p.status === "Evaluating" ? "●" : p.status === "Completed" ? "✓" : "✗";
    return el("div", { class: "eval-candidate-progress" }, [
      el("span", {}, indicator),
      el("span", {}, p.candidate.label),
      el("span", { class: "hint" }, p.status || "unknown"),
    ]);
  });

  const headline = hasRunning
    ? el("p", { class: "bold" }, `Analyzing ${totalCandidates} candidate${totalCandidates === 1 ? "" : "s"}...`)
    : el("p", { class: "bold" }, "Evaluation Complete");

  return el("div", { class: "stack", style: "gap:8px" }, [
    headline,
    ...progressItems,
    el("p", { class: "hint" }, `${numCompleted} / ${totalCandidates} completed`),
  ]);
}

function buildEvaluationMatrixSection(runProgresses, criteriaOrder, mostRecentRun, definitionCatalog, agentCatalog) {
  // Only show matrix if all runs are complete
  const allComplete = runProgresses.every((p) => p.status === "Completed");
  if (!allComplete) {
    return null;
  }

  // Find definition and evaluator names for provenance
  let definitionName = "Unknown";
  let definitionVersion = 1;
  for (const defEntry of definitionCatalog || []) {
    for (const version of defEntry.versions || []) {
      if (version.id === mostRecentRun.evaluation_definition_version_id) {
        definitionName = defEntry.definition.name;
        definitionVersion = version.version;
        break;
      }
    }
  }

  // Resolve evaluator agent name and role from catalog
  let evaluatorAgentName = "Unknown";
  let evaluatorRole = "";
  if (mostRecentRun.evaluator_agent_version_id && agentCatalog) {
    // Find the agent version in the catalog
    for (const agentEntry of agentCatalog) {
      for (const version of agentEntry.versions || []) {
        if (version.id === mostRecentRun.evaluator_agent_version_id) {
          evaluatorAgentName = agentEntry.agent.name;
          evaluatorRole = version.key || ""; // Version key is the role identifier
          break;
        }
      }
    }
  }

  const evaluatorModelStr = mostRecentRun.evaluator_model
    ? mostRecentRun.evaluator_model.canonical_model_id
    : "Unknown";

  const evaluatorStr = evaluatorRole ? `${evaluatorAgentName} · ${evaluatorRole}` : evaluatorAgentName;
  const provenanceHeader = el("p", { class: "hint", style: "margin-bottom:12px" }, [
    `Evaluation: ${definitionName} · v${definitionVersion} | `,
    `Evaluator: ${evaluatorStr} | `,
    `Model: ${evaluatorModelStr}`,
  ]);

  const findingLabel = (finding) => {
    if (!finding) return "—";
    switch (finding.toLowerCase()) {
      case "met":
        return "MET";
      case "partial":
        return "PARTIAL";
      case "not_met":
        return "NOT MET";
      case "not_applicable":
        return "N/A";
      default:
        return finding;
    }
  };

  const matrixRows = criteriaOrder.map((criterion) => {
    const cells = [el("td", {}, criterion.label)];
    for (const p of runProgresses) {
      const run = p.run;
      if (!run || !run.criterion_results) {
        cells.push(el("td", { class: "eval-cell-empty" }, "—"));
        continue;
      }
      const result = run.criterion_results.find((r) => r.criterion_key === criterion.key);
      const finding = result ? result.finding : null;
      const cellClass = `eval-cell eval-cell-${finding ? finding.toLowerCase() : "empty"}`;
      const label = findingLabel(finding);
      cells.push(el("td", { class: cellClass }, el("span", { class: "eval-badge" }, label)));
    }
    return el("tr", {}, cells);
  });

  const headerCells = [el("th", {}, "Criterion")];
  for (const p of runProgresses) {
    headerCells.push(el("th", {}, p.candidate.label));
  }

  const table = el("table", { class: "eval-matrix" }, [
    el("thead", {}, el("tr", {}, headerCells)),
    el("tbody", {}, matrixRows),
  ]);

  return el("div", { class: "stack", style: "gap:8px" }, [
    provenanceHeader,
    el("div", { style: "overflow-x:auto" }, table),
  ]);
}
