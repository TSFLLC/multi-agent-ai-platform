// Ask Agents — the primary screen. Uses the real MA5 comparison APIs end
// to end (Task -> Comparison -> launch), never a frontend-only fake.
//
// MA5-UI Post-UAT Enhancement 1: with 400+ models the old always-visible
// checkbox grid was unusable. The model catalog below is search/filter-
// driven and bounded (frontend/assets/js/modelFilter.js).
//
// MA5-UI Post-UAT Enhancement 1B: adds a Model Developer / Family filter
// (frontend/assets/js/modelDeveloper.js) derived from the existing
// canonical_model_id namespace — a purely presentational grouping, never
// a new backend Provider (OpenRouter remains the one configured
// Provider throughout).
//
// MA5-UI Post-UAT Enhancement 1C: replaces "one Agent + a set of models"
// with a candidate-oriented configuration — each candidate independently
// picks Agent + Role + Model (frontend/assets/js/candidates.js). "Role"
// is not a new backend entity: it is AgentVersion itself (see
// frontend/assets/js/agentDirectory.js's module docstring for the
// architecture investigation) — publishing a second AgentVersion for the
// same Agent (already possible via the existing Agent Registry API)
// makes a second, genuinely-executable Role selectable here for free.
// Candidate identity is never model identity: the same model may be
// assigned to any number of candidates, each with its own Agent/Role.

import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import { getProjectId } from "../state.js";
import { navigate } from "../router.js";
import { formatPriceSummary, pricingBadgeClass, pricingLabel, truncate } from "../format.js";
import { PRICING_FILTER_ALL, boundedResults, filterModels, uniqueProviderIds } from "../modelFilter.js";
import { developerLabel, extractDeveloperKey, uniqueDevelopers } from "../modelDeveloper.js";
import { activeVersionsOf, buildVersionIndex, loadAgentCatalog, roleLabelFor } from "../agentDirectory.js";
import {
  canRunCandidateComparison,
  countCandidatesUsingModel,
  createCandidate,
  duplicateLocalIds,
  runnableCandidates,
} from "../candidates.js";

const DEFAULT_VISIBLE_COUNT = 24;
const SHOW_MORE_STEP = 24;
const PRICING_TABS = [
  { value: "free", label: "FREE" },
  { value: "paid", label: "PAID" },
  { value: "unknown", label: "UNKNOWN" },
  { value: PRICING_FILTER_ALL, label: "ALL" },
];

export async function renderAsk(root) {
  const state = {
    question: "",
    agentCatalog: [], // [{ agent, versions }]
    models: [],
    providerNameById: new Map(),
    candidates: [],
    activeCandidateId: null,
    nextCandidateSeq: 1,
    searchQuery: "",
    pricingFilter: "free", // Section 6: default the Ask Agents selector to FREE for local operation.
    providerFilter: "",
    developerKeys: new Set(), // empty == "All" developers
    visibleCount: DEFAULT_VISIBLE_COUNT,
    submitting: false,
    error: null,
    loadError: null,
  };

  mount(root, buildLoadingCard());

  const projectId = await getProjectIdSafe(state);
  if (state.loadError) {
    mount(root, buildLoadError(state.loadError));
    return;
  }

  const [agentCatalog, models, providers] = await Promise.all([
    loadAgentCatalog(projectId),
    api.get("/models"),
    api.get("/providers").catch(() => []),
  ]);
  state.agentCatalog = agentCatalog.filter((entry) => activeVersionsOf(entry).length > 0);
  state.models = models;
  state.providerNameById = new Map(providers.map((p) => [p.id, p.name]));

  if (state.agentCatalog.length) {
    const firstEntry = state.agentCatalog[0];
    const firstVersion = activeVersionsOf(firstEntry)[0];
    state.candidates = [
      newCandidate(state, firstEntry.agent.id, firstVersion.id),
      newCandidate(state, firstEntry.agent.id, firstVersion.id),
    ];
    state.activeCandidateId = state.candidates[0].localId;
  }

  render(root, projectId, state);
}

function newCandidate(state, agentId, agentVersionId) {
  return createCandidate({ localId: `c${state.nextCandidateSeq++}`, agentId, agentVersionId, modelId: null });
}

async function getProjectIdSafe(state) {
  try {
    return await getProjectId();
  } catch (err) {
    state.loadError = err;
    return null;
  }
}

function buildLoadingCard() {
  return el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading agents and models…"]);
}

function buildLoadError(err) {
  return el("div", { class: "error-banner" }, err.message || "Failed to load Ask Agents.");
}

function render(root, projectId, state) {
  const container = el("div", { class: "stack" });

  container.appendChild(
    el("div", { class: "page-header" }, [
      el("h1", {}, "Ask Agents"),
      el("div", { class: "subtitle" }, "Ask a question, configure 2+ candidates (each its own Agent + Role + Model), and run a Parallel Comparison — no Swagger, no UUIDs."),
    ])
  );

  if (state.error) {
    container.appendChild(el("div", { class: "error-banner" }, state.error));
  }

  container.appendChild(buildQuestionCard(state));
  container.appendChild(buildConfigurationSection(state));
  container.appendChild(buildRunBar(root, projectId, state));

  mount(root, container);
}

function buildQuestionCard(state) {
  // Section 7: one shared question for the whole comparison — never a
  // per-candidate question box.
  const textarea = el("textarea", {
    placeholder: "Ask a question or describe a task for the agents to work on…",
    oninput: (e) => {
      state.question = e.target.value;
    },
  });
  textarea.value = state.question;
  return el("div", { class: "card" }, [el("label", {}, "Question / Task (shared by every candidate)"), textarea]);
}

// -- Candidates + model workspace: linked by a shared refreshAll() so
// changing a candidate's Agent/Role/Model, adding/removing a candidate,
// or assigning a model from the catalog all keep both sections in sync
// (Section 3/6). ---------------------------------------------------------

function buildConfigurationSection(state) {
  const wrapper = el("div", { class: "stack" });
  const candidatesSection = el("div", {});
  const workspaceSection = el("div", {});

  function refreshCandidates() {
    clear(candidatesSection);
    candidatesSection.appendChild(buildCandidatesCard(state, refreshAll));
  }
  function refreshWorkspace() {
    clear(workspaceSection);
    workspaceSection.appendChild(buildModelWorkspaceCard(state, refreshAll));
  }
  function refreshAll() {
    ensureActiveCandidate(state);
    refreshCandidates();
    refreshWorkspace();
  }

  refreshAll();
  wrapper.appendChild(candidatesSection);
  wrapper.appendChild(workspaceSection);
  return wrapper;
}

function ensureActiveCandidate(state) {
  if (!state.candidates.some((c) => c.localId === state.activeCandidateId)) {
    state.activeCandidateId = state.candidates[0] ? state.candidates[0].localId : null;
  }
}

// -- Candidates card ----------------------------------------------------------

function buildCandidatesCard(state, onChange) {
  if (!state.agentCatalog.length) {
    return el("div", { class: "card" }, [
      el("label", {}, "Candidates"),
      el("p", {}, "No published Agent is available yet. Publish an Agent Version before running a comparison."),
    ]);
  }

  const rows = state.candidates.map((candidate, index) => buildCandidateRow(state, candidate, index, onChange));

  const addButton = el(
    "button",
    {
      class: "small",
      onclick: () => {
        const firstEntry = state.agentCatalog[0];
        const firstVersion = activeVersionsOf(firstEntry)[0];
        const candidate = newCandidate(state, firstEntry.agent.id, firstVersion.id);
        state.candidates.push(candidate);
        state.activeCandidateId = candidate.localId;
        onChange();
      },
    },
    "+ Add Candidate"
  );

  return el("div", { class: "card" }, [
    el("div", { class: "row between" }, [
      el("label", { style: "margin:0" }, "Candidates"),
      el("span", { class: "hint" }, `${runnableCandidates(state.candidates).length} runnable (need 2+)`),
    ]),
    el("div", { class: "stack", style: "gap:10px" }, rows),
    addButton,
  ]);
}

function buildCandidateRow(state, candidate, index, onChange) {
  const isActive = candidate.localId === state.activeCandidateId;
  const isDuplicate = duplicateLocalIds(state.candidates).has(candidate.localId);
  const entry = state.agentCatalog.find((e) => e.agent.id === candidate.agentId);
  const versions = activeVersionsOf(entry);
  const assignedModel = candidate.modelId ? state.models.find((m) => m.id === candidate.modelId) : null;

  const agentSelect = el(
    "select",
    {
      onchange: (e) => {
        candidate.agentId = e.target.value;
        const newEntry = state.agentCatalog.find((en) => en.agent.id === candidate.agentId);
        const newVersions = activeVersionsOf(newEntry);
        candidate.agentVersionId = newVersions[0] ? newVersions[0].id : null;
        state.activeCandidateId = candidate.localId;
        onChange();
      },
    },
    state.agentCatalog.map((en) => el("option", { value: en.agent.id, selected: en.agent.id === candidate.agentId }, en.agent.name))
  );

  const roleSelect = el(
    "select",
    {
      disabled: versions.length === 0,
      onchange: (e) => {
        candidate.agentVersionId = e.target.value;
        state.activeCandidateId = candidate.localId;
        onChange();
      },
    },
    versions.map((v) =>
      el(
        "option",
        { value: v.id, selected: v.id === candidate.agentVersionId },
        versions.length > 1 ? `${roleLabelFor(v)} (v${v.version})` : roleLabelFor(v)
      )
    )
  );

  const modelSummary = assignedModel
    ? el("span", {}, [
        el("span", { class: pricingBadgeClass(assignedModel.pricing_classification) }, pricingLabel(assignedModel.pricing_classification)),
        ` ${assignedModel.canonical_model_id}`,
      ])
    : el("span", { class: "hint" }, "No model chosen — pick one below");

  const configureButton = el(
    "button",
    {
      class: `small${isActive ? " primary" : ""}`,
      onclick: () => {
        state.activeCandidateId = candidate.localId;
        onChange();
      },
    },
    isActive ? "Configuring…" : "Configure"
  );

  const removeButton = el(
    "button",
    {
      class: "small danger",
      onclick: () => {
        state.candidates = state.candidates.filter((c) => c.localId !== candidate.localId);
        onChange();
      },
    },
    "Remove"
  );

  return el("div", { class: `candidate-row${isActive ? " active" : ""}` }, [
    el("div", { class: "row between" }, [
      el("div", { class: "candidate-row-title" }, `Candidate ${index + 1}`),
      el("div", { class: "row" }, [configureButton, removeButton]),
    ]),
    el("div", { class: "candidate-row-controls" }, [
      el("div", {}, [el("label", {}, "Agent"), agentSelect]),
      el("div", {}, [el("label", {}, "Role"), roleSelect]),
      el("div", {}, [el("label", {}, "Model"), modelSummary]),
    ]),
    isDuplicate
      ? el(
          "div",
          { class: "error-banner", style: "padding:6px 10px;font-size:0.78rem" },
          "Duplicate of another candidate (same Agent + Role + Model) — won't be included when you run."
        )
      : null,
  ]);
}

// -- Model workspace: developer sidebar + filterable/bounded catalog ---------
// (Section 5/6). Clicking a model assigns it to whichever candidate is
// currently "active" (Section 6) — never deduplicated by model id
// (Section 1/4): the same model can be assigned to any number of
// candidates.

function buildModelWorkspaceCard(state, onAssign) {
  if (!state.models.length) {
    return el("div", { class: "card" }, [
      el("label", {}, "Models"),
      el("div", { class: "empty-state" }, [
        el("div", { class: "icon" }, "🧩"),
        el("h3", {}, "No models available yet"),
        el("p", {}, "Connect OpenRouter and refresh the catalog on the Models page first."),
        el("a", { href: "#/models" }, [el("button", {}, "Go to Models →")]),
      ]),
    ]);
  }

  const card = el("div", { class: "card" });
  const resultsSection = el("div", {});

  function refreshResults() {
    clear(resultsSection);
    resultsSection.appendChild(buildCatalogResults(state, refreshResults, onAssign));
  }

  const onFilterChange = () => {
    state.visibleCount = DEFAULT_VISIBLE_COUNT;
    refreshResults();
  };

  const sidebar = buildDeveloperSidebar(state, onFilterChange);
  const filterBar = buildFilterBar(state, onFilterChange);
  const catalogColumn = el("div", { class: "stack", style: "gap:10px" }, [filterBar, resultsSection]);

  refreshResults();

  card.appendChild(el("label", {}, "Models"));
  card.appendChild(buildActiveCandidateBanner(state));
  card.appendChild(sidebar ? el("div", { class: "model-workspace" }, [sidebar, catalogColumn]) : catalogColumn);
  return card;
}

function buildActiveCandidateBanner(state) {
  const index = state.candidates.findIndex((c) => c.localId === state.activeCandidateId);
  if (index === -1) {
    return el("div", { class: "active-candidate-banner" }, "Add a candidate above to begin choosing its model.");
  }
  const candidate = state.candidates[index];
  const entry = state.agentCatalog.find((e) => e.agent.id === candidate.agentId);
  const version = entry ? entry.versions.find((v) => v.id === candidate.agentVersionId) : null;
  return el(
    "div",
    { class: "active-candidate-banner" },
    `Configuring: Candidate ${index + 1} — ${entry ? entry.agent.name : "?"} / ${roleLabelFor(version)}`
  );
}

function buildDeveloperSidebar(state, onFilterChange) {
  const developers = uniqueDevelopers(state.models);
  if (developers.length <= 1) return null; // nothing meaningful to filter by

  const devEntries = []; // { checkbox, row }

  const allButton = el(
    "button",
    {
      type: "button",
      class: `dev-all${state.developerKeys.size === 0 ? " active" : ""}`,
      onclick: () => {
        state.developerKeys = new Set();
        devEntries.forEach(({ checkbox, row }) => {
          checkbox.checked = false;
          row.classList.remove("active");
        });
        allButton.classList.add("active");
        onFilterChange();
      },
    },
    "All"
  );

  const devRows = developers.map(({ keys, label, count }) => {
    // `keys` holds every raw namespace this normalized label represents
    // (e.g. "meta" + "meta-llama" both -> "Meta") -- selecting this row
    // must filter by ALL of them together, never just the first one.
    const isSelected = () => [...keys].every((k) => state.developerKeys.has(k));
    const checkbox = el("input", { type: "checkbox", checked: isSelected() });
    const row = el("label", { class: `dev-item${isSelected() ? " active" : ""}` }, [
      checkbox,
      el("span", { class: "dev-item-label" }, label),
      el("span", { class: "dev-count" }, String(count)),
    ]);
    checkbox.addEventListener("change", () => {
      const next = new Set(state.developerKeys);
      if (checkbox.checked) keys.forEach((k) => next.add(k));
      else keys.forEach((k) => next.delete(k));
      state.developerKeys = next;
      row.classList.toggle("active", checkbox.checked);
      allButton.classList.toggle("active", state.developerKeys.size === 0);
      onFilterChange();
    });
    devEntries.push({ checkbox, row });
    return row;
  });

  return el("div", { class: "developer-sidebar" }, [
    el("div", { class: "sidebar-title" }, "Model Developers"),
    allButton,
    el("div", { class: "dev-list" }, devRows),
  ]);
}

function buildFilterBar(state, onFilterChange) {
  const providerIds = uniqueProviderIds(state.models);
  const showProviderFilter = providerIds.length > 1; // Section: never fake provider categories.

  const searchInput = el("input", {
    type: "text",
    placeholder: "Search models by name…",
    oninput: (e) => {
      state.searchQuery = e.target.value;
      onFilterChange();
    },
  });
  searchInput.value = state.searchQuery;

  const pricingPills = el(
    "div",
    { class: "filter-pills" },
    PRICING_TABS.map((tab) =>
      el(
        "button",
        {
          type: "button",
          class: `filter-pill${state.pricingFilter === tab.value ? " active" : ""}`,
          onclick: (e) => {
            state.pricingFilter = tab.value;
            e.currentTarget.parentElement.querySelectorAll(".filter-pill").forEach((btn) => btn.classList.remove("active"));
            e.currentTarget.classList.add("active");
            onFilterChange();
          },
        },
        tab.label
      )
    )
  );

  const children = [searchInput, pricingPills];

  if (showProviderFilter) {
    const providerSelect = el(
      "select",
      {
        onchange: (e) => {
          state.providerFilter = e.target.value;
          onFilterChange();
        },
      },
      [
        el("option", { value: "" }, "All providers"),
        ...providerIds.map((id) =>
          el("option", { value: id, selected: id === state.providerFilter }, state.providerNameById.get(id) || "Unknown provider")
        ),
      ]
    );
    children.push(providerSelect);
  }

  return el("div", { class: "filter-bar" }, children);
}

function buildCatalogResults(state, refreshResults, onAssign) {
  const filtered = filterModels(state.models, {
    query: state.searchQuery,
    pricing: state.pricingFilter,
    providerId: state.providerFilter,
    developerKeys: state.developerKeys,
  });

  if (!filtered.length) {
    return el("div", { class: "empty-state" }, [
      el("div", { class: "icon" }, "🔍"),
      el("p", {}, "No models match this search/filter."),
    ]);
  }

  const { visible, remaining } = boundedResults(filtered, state.visibleCount);
  const activeCandidate = state.candidates.find((c) => c.localId === state.activeCandidateId) || null;

  const wrapper = el("div", { class: "stack", style: "gap:10px" }, [
    el("div", { class: "hint" }, `${filtered.length} model${filtered.length === 1 ? "" : "s"} match`),
    el(
      "div",
      { class: "model-picker" },
      visible.map((model) => buildModelCatalogRow(model, state, activeCandidate, onAssign))
    ),
  ]);

  if (remaining > 0) {
    wrapper.appendChild(
      el(
        "button",
        {
          class: "small",
          onclick: () => {
            state.visibleCount += SHOW_MORE_STEP;
            refreshResults();
          },
        },
        `Show more (${remaining} remaining)`
      )
    );
  }

  return wrapper;
}

function buildModelCatalogRow(model, state, activeCandidate, onAssign) {
  const disabled = model.model_status !== "active";
  const assignedToActive = Boolean(activeCandidate && activeCandidate.modelId === model.id);
  const usageCount = countCandidatesUsingModel(state.candidates, model.id);

  // Only a capability the registry reliably represents (Section 6:
  // "useful capability indicators only when reliably represented") --
  // never inferred from the model's name.
  const toolCalling =
    model.tool_calling_support && model.tool_calling_support !== "none" ? `tools: ${model.tool_calling_support}` : null;

  return el(
    "button",
    {
      type: "button",
      class: `model-option${assignedToActive ? " checked" : ""}${disabled ? " disabled" : ""}`,
      disabled: disabled || !activeCandidate,
      onclick: () => {
        if (!activeCandidate) return;
        // Clicking the already-assigned model again un-assigns it;
        // clicking a different one replaces it — one model per candidate,
        // but never deduplicated across candidates (Section 1/4).
        activeCandidate.modelId = activeCandidate.modelId === model.id ? null : model.id;
        onAssign();
      },
    },
    [
      el("div", {}, [
        el("div", { class: "model-name" }, model.canonical_model_id),
        el("div", { class: "model-meta" }, [
          developerLabel(extractDeveloperKey(model)),
          model.context_window ? ` · ${model.context_window.toLocaleString("en-US")} ctx` : "",
          toolCalling ? ` · ${toolCalling}` : "",
          disabled ? " · unavailable" : "",
          usageCount > 0 ? ` · used in ${usageCount} candidate${usageCount === 1 ? "" : "s"}` : "",
        ]),
        el("div", { class: "model-meta" }, [
          el("span", { class: pricingBadgeClass(model.pricing_classification) }, pricingLabel(model.pricing_classification)),
          ` ${formatPriceSummary(model.pricing_classification, model.cost_input_per_mtok, model.cost_output_per_mtok)}`,
        ]),
      ]),
    ]
  );
}

// -- Run bar -----------------------------------------------------------------

function buildRunBar(root, projectId, state) {
  const canRun = () => canRunCandidateComparison({ question: state.question, candidates: state.candidates }) && !state.submitting;

  const runButton = el(
    "button",
    {
      class: "primary",
      disabled: !canRun(),
      onclick: () => runComparison(root, projectId, state, runButton, statusLine),
    },
    "Run Comparison"
  );

  const statusLine = el("span", { class: "hint" }, "");

  const bar = el("div", { class: "row between" }, [statusLine, runButton]);
  ["input", "change", "click"].forEach((evt) => {
    root.addEventListener(evt, () => {
      runButton.disabled = !canRun();
    });
  });
  return bar;
}

async function runComparison(root, projectId, state, runButton, statusLine) {
  if (state.submitting) return;
  state.submitting = true;
  runButton.disabled = true;
  statusLine.innerHTML = "";
  statusLine.appendChild(el("span", { class: "spinner" }));
  statusLine.appendChild(document.createTextNode(" Creating comparison…"));

  try {
    const runnable = runnableCandidates(state.candidates);
    if (runnable.length < 2) throw new Error("Configure at least 2 valid, non-duplicate candidates (Agent + Role + Model).");

    const task = await api.post("/tasks", {
      project_id: projectId,
      title: truncate(state.question, 120),
      description: state.question,
      execution_mode: "parallel_comparison",
    });

    // Candidate label encodes Agent/Role/Model (Section 9): the backend's
    // ComparisonCandidateRead has no dedicated agent-name/role fields, so
    // the results page resolves them the same way this page did — via
    // GET /agents + GET /agents/{id}/versions, keyed by agent_version_id
    // — never by parsing this label back apart.
    const versionIndex = buildVersionIndex(state.agentCatalog);
    const candidatesPayload = runnable.map((candidate) => {
      const resolved = versionIndex.get(candidate.agentVersionId);
      const model = state.models.find((m) => m.id === candidate.modelId);
      const agentName = resolved ? resolved.agent.name : "Agent";
      const roleLabel = roleLabelFor(resolved ? resolved.version : null);
      const modelName = model ? model.canonical_model_id : candidate.modelId;
      return {
        agent_version_id: candidate.agentVersionId,
        label: `${agentName} · ${roleLabel} · ${modelName}`,
        model_policy_override: { mode: "manual", manual_provider_model_id: candidate.modelId },
      };
    });

    const comparison = await api.post("/comparisons", { task_id: task.id, candidates: candidatesPayload });
    await api.post(`/comparisons/${comparison.id}/launch`);

    navigate(`#/comparisons/${comparison.id}`);
  } catch (err) {
    state.error = err.message || "Failed to run comparison.";
    state.submitting = false;
    render(root, projectId, state);
  }
}
