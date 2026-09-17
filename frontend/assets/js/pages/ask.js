// Ask Agents — the primary screen (Section 4): Question -> select Agent ->
// select 2-3 models -> Run Comparison. Uses the real MA5 comparison APIs
// end to end (Task -> Comparison -> launch), never a frontend-only fake
// (Section 5).
//
// MA5-UI Post-UAT Enhancement 1: with 400+ models the old always-visible
// checkbox grid was unusable. The model picker below is search/filter-
// driven and bounded (frontend/assets/js/modelFilter.js holds the pure,
// unit-tested filtering/selection logic); a persistent Selected Models
// section keeps a choice visible no matter what the active filter hides.
//
// MA5-UI Post-UAT Enhancement 1B: adds a Model Developer / Family filter
// (frontend/assets/js/modelDeveloper.js) derived from the existing
// canonical_model_id namespace — a purely presentational grouping, never
// a new backend Provider (OpenRouter remains the one configured
// Provider throughout).

import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import { getProjectId } from "../state.js";
import { navigate } from "../router.js";
import { formatPriceSummary, pricingBadgeClass, pricingLabel, truncate } from "../format.js";
import {
  PRICING_FILTER_ALL,
  boundedResults,
  canRunComparison,
  filterModels,
  selectedModelsList,
  toggleModelSelection,
  uniqueProviderIds,
} from "../modelFilter.js";
import { developerLabel, extractDeveloperKey, uniqueDevelopers } from "../modelDeveloper.js";

const MAX_AUTO_FREE_SELECTIONS = 3;
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
    agents: [],
    models: [],
    providerNameById: new Map(),
    selectedModelIds: new Set(),
    userTouchedModels: false,
    selectedAgentId: null,
    searchQuery: "",
    pricingFilter: "free", // Section: default the Ask Agents selector to FREE for local operation.
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

  const [agents, models, providers] = await Promise.all([
    loadAgentsWithActiveVersion(projectId, state),
    api.get("/models"),
    api.get("/providers").catch(() => []),
  ]);
  state.agents = agents;
  state.models = models;
  state.providerNameById = new Map(providers.map((p) => [p.id, p.name]));
  if (agents.length) state.selectedAgentId = agents[0].agent.id;
  applyDefaultFreeSelection(state);

  render(root, projectId, state);
}

async function getProjectIdSafe(state) {
  try {
    return await getProjectId();
  } catch (err) {
    state.loadError = err;
    return null;
  }
}

async function loadAgentsWithActiveVersion(projectId, state) {
  const agents = await api.get(`/agents?project_id=${encodeURIComponent(projectId)}`);
  const withVersions = await Promise.all(
    agents.map(async (agent) => {
      const versions = await api.get(`/agents/${agent.id}/versions`);
      const active = versions
        .filter((v) => v.status === "active")
        .sort((a, b) => b.version - a.version)[0];
      return { agent, activeVersion: active || null };
    })
  );
  return withVersions.filter((entry) => entry.activeVersion !== null);
}

function applyDefaultFreeSelection(state) {
  if (state.userTouchedModels) return;
  const freeActive = state.models.filter((m) => m.pricing_classification === "free" && m.model_status === "active");
  freeActive.slice(0, MAX_AUTO_FREE_SELECTIONS).forEach((m) => state.selectedModelIds.add(m.id));
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
      el("div", { class: "subtitle" }, "Ask a question, pick an Agent and 2+ models, and run a Parallel Comparison — no Swagger, no UUIDs."),
    ])
  );

  if (state.error) {
    container.appendChild(el("div", { class: "error-banner" }, state.error));
  }

  container.appendChild(buildQuestionCard(state));
  container.appendChild(buildAgentCard(state));
  container.appendChild(buildModelSelectorCard(state));
  container.appendChild(buildRunBar(root, projectId, state));

  mount(root, container);
}

function buildQuestionCard(state) {
  const textarea = el("textarea", {
    placeholder: "Ask a question or describe a task for the agents to work on…",
    oninput: (e) => {
      state.question = e.target.value;
    },
  });
  textarea.value = state.question;
  return el("div", { class: "card" }, [el("label", {}, "Question / Task"), textarea]);
}

function buildAgentCard(state) {
  if (!state.agents.length) {
    return el("div", { class: "card" }, [
      el("label", {}, "Agent"),
      el("p", {}, "No published Agent is available yet. Publish an Agent Version before running a comparison."),
    ]);
  }

  const select = el(
    "select",
    {
      onchange: (e) => {
        state.selectedAgentId = e.target.value;
      },
    },
    state.agents.map(({ agent }) =>
      el("option", { value: agent.id, selected: agent.id === state.selectedAgentId }, `${agent.name} (${agent.role})`)
    )
  );

  return el("div", { class: "card" }, [el("label", {}, "Agent"), select]);
}

// -- Model selector (search + pricing/provider filters + bounded results,
// with a persistent Selected Models section) -------------------------------

function buildModelSelectorCard(state) {
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
  const countLabel = el("span", { class: "hint", id: "model-selected-count" }, selectedCountText(state));
  const selectedSection = el("div", { class: "selected-models" });
  const resultsSection = el("div", {});

  function refreshSelected() {
    clear(selectedSection);
    selectedSection.appendChild(buildSelectedModelsBlock(state, refreshAll));
    countLabel.textContent = selectedCountText(state);
  }

  function refreshResults() {
    clear(resultsSection);
    resultsSection.appendChild(buildResultsBlock(state, refreshResults, refreshAll));
  }

  function refreshAll() {
    refreshSelected();
    refreshResults();
  }

  card.appendChild(
    el("div", { class: "row between" }, [el("label", { style: "margin:0" }, "Models (select 2 or more)"), countLabel])
  );
  card.appendChild(selectedSection);
  const onFilterChange = () => {
    state.visibleCount = DEFAULT_VISIBLE_COUNT;
    refreshResults();
  };
  const developerFilter = buildDeveloperFilter(state, onFilterChange);
  if (developerFilter) card.appendChild(developerFilter);
  card.appendChild(buildFilterBar(state, onFilterChange));
  card.appendChild(resultsSection);

  refreshAll();
  return card;
}

function selectedCountText(state) {
  return `${state.selectedModelIds.size} selected`;
}

function buildSelectedModelsBlock(state, onChange) {
  const selected = selectedModelsList(state.models, state.selectedModelIds);
  if (!selected.length) {
    return el("p", { class: "hint" }, "No models selected yet — choose 2 or more below.");
  }
  return el(
    "div",
    { class: "chip-row" },
    selected.map((model) =>
      el("span", { class: "model-chip" }, [
        el("span", { class: "chip-name" }, `${developerLabel(extractDeveloperKey(model))} · ${model.canonical_model_id}`),
        el("span", { class: pricingBadgeClass(model.pricing_classification) }, pricingLabel(model.pricing_classification)),
        el(
          "button",
          {
            class: "chip-remove",
            "aria-label": `Remove ${model.canonical_model_id}`,
            onclick: () => {
              state.userTouchedModels = true;
              state.selectedModelIds = toggleModelSelection(state.selectedModelIds, model.id);
              onChange();
            },
          },
          "×"
        ),
      ])
    )
  );
}

function buildDeveloperFilter(state, onFilterChange) {
  const developers = uniqueDevelopers(state.models);
  if (developers.length <= 1) return null; // nothing meaningful to filter by

  const allPill = el(
    "button",
    {
      type: "button",
      class: `filter-pill${state.developerKeys.size === 0 ? " active" : ""}`,
      onclick: (e) => {
        state.developerKeys = new Set();
        e.currentTarget.parentElement.querySelectorAll(".filter-pill").forEach((btn) => btn.classList.remove("active"));
        e.currentTarget.classList.add("active");
        onFilterChange();
      },
    },
    "All"
  );

  const devPills = developers.map(({ key, label, count }) =>
    el(
      "button",
      {
        type: "button",
        class: `filter-pill${state.developerKeys.has(key) ? " active" : ""}`,
        onclick: (e) => {
          const next = new Set(state.developerKeys);
          if (next.has(key)) next.delete(key);
          else next.add(key);
          state.developerKeys = next;
          e.currentTarget.classList.toggle("active");
          allPill.classList.toggle("active", state.developerKeys.size === 0);
          onFilterChange();
        },
      },
      `${label} (${count})`
    )
  );

  return el("div", { class: "stack", style: "gap:6px; margin-bottom: 4px" }, [
    el("label", { style: "margin:0" }, "Model Developers"),
    el("div", { class: "filter-pills wrap" }, [allPill, ...devPills]),
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

function buildResultsBlock(state, refreshResults, refreshAll) {
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

  const wrapper = el("div", { class: "stack", style: "gap:10px" }, [
    el("div", { class: "hint" }, `${filtered.length} model${filtered.length === 1 ? "" : "s"} match`),
    el(
      "div",
      { class: "model-picker" },
      visible.map((model) => buildModelOption(model, state, refreshAll))
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

function buildModelOption(model, state, onToggle) {
  const checked = state.selectedModelIds.has(model.id);
  const disabled = model.model_status !== "active";
  const checkbox = el("input", {
    type: "checkbox",
    checked,
    disabled,
    onchange: () => {
      state.userTouchedModels = true;
      state.selectedModelIds = toggleModelSelection(state.selectedModelIds, model.id);
      onToggle();
    },
  });

  // Only a capability the registry reliably represents (Section 3:
  // "useful capability indicators only when reliably represented") --
  // never inferred from the model's name.
  const toolCalling =
    model.tool_calling_support && model.tool_calling_support !== "none" ? `tools: ${model.tool_calling_support}` : null;

  return el("label", { class: `model-option${checked ? " checked" : ""}${disabled ? " disabled" : ""}` }, [
    checkbox,
    el("div", {}, [
      el("div", { class: "model-name" }, model.canonical_model_id),
      el("div", { class: "model-meta" }, [
        developerLabel(extractDeveloperKey(model)),
        model.context_window ? ` · ${model.context_window.toLocaleString("en-US")} ctx` : "",
        toolCalling ? ` · ${toolCalling}` : "",
        disabled ? " · unavailable" : "",
      ]),
      el("div", { class: "model-meta" }, [
        el("span", { class: pricingBadgeClass(model.pricing_classification) }, pricingLabel(model.pricing_classification)),
        ` ${formatPriceSummary(model.pricing_classification, model.cost_input_per_mtok, model.cost_output_per_mtok)}`,
      ]),
    ]),
  ]);
}

// -- Run bar -----------------------------------------------------------------

function buildRunBar(root, projectId, state) {
  const canRun = () =>
    canRunComparison({
      question: state.question,
      agentId: state.selectedAgentId,
      selectedCount: state.selectedModelIds.size,
    }) && !state.submitting;

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

  // Model selection/removal is a click, not always an 'input'/'change' on
  // the exact control (e.g. the chip remove button) — listening on all
  // three at the root keeps this correct regardless of which control fired.
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
    const agentEntry = state.agents.find(({ agent }) => agent.id === state.selectedAgentId);
    if (!agentEntry) throw new Error("Select an Agent first.");
    const selected = selectedModelsList(state.models, state.selectedModelIds);
    if (selected.length < 2) throw new Error("Select at least 2 models.");

    const task = await api.post("/tasks", {
      project_id: projectId,
      title: truncate(state.question, 120),
      description: state.question,
      execution_mode: "parallel_comparison",
    });

    const candidates = selected.map((model) => ({
      agent_version_id: agentEntry.activeVersion.id,
      label: `${agentEntry.agent.name} · ${model.canonical_model_id}`,
      model_policy_override: { mode: "manual", manual_provider_model_id: model.id },
    }));

    const comparison = await api.post("/comparisons", { task_id: task.id, candidates });
    await api.post(`/comparisons/${comparison.id}/launch`);

    navigate(`#/comparisons/${comparison.id}`);
  } catch (err) {
    state.error = err.message || "Failed to run comparison.";
    state.submitting = false;
    render(root, projectId, state);
  }
}
