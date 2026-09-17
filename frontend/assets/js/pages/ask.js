// Ask Agents — the primary screen (Section 4): Question -> select Agent ->
// select 2-3 models -> Run Comparison. Uses the real MA5 comparison APIs
// end to end (Task -> Comparison -> launch), never a frontend-only fake
// (Section 5).

import { api } from "../api.js";
import { el, mount } from "../dom.js";
import { getProjectId } from "../state.js";
import { navigate } from "../router.js";
import { pricingBadgeClass, pricingLabel, truncate } from "../format.js";

const MAX_AUTO_FREE_SELECTIONS = 3;

export async function renderAsk(root) {
  const state = {
    question: "",
    agents: [],
    agentVersionById: new Map(), // agent.id -> active AgentVersion.id
    models: [],
    selectedModelIds: new Set(),
    userTouchedModels: false,
    selectedAgentId: null,
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

  const [agents, models] = await Promise.all([loadAgentsWithActiveVersion(projectId, state), api.get("/models")]);
  state.agents = agents;
  state.models = models;
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
  container.appendChild(buildModelsCard(state));
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

function buildModelsCard(state) {
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

  const grid = el(
    "div",
    { class: "model-picker" },
    state.models.map((model) => buildModelOption(model, state))
  );

  return el("div", { class: "card" }, [
    el("div", { class: "row between" }, [
      el("label", { style: "margin:0" }, "Models (select 2 or more)"),
      el("span", { class: "hint", id: "selected-model-count" }, `${state.selectedModelIds.size} selected`),
    ]),
    grid,
  ]);
}

function buildModelOption(model, state) {
  const checked = state.selectedModelIds.has(model.id);
  const disabled = model.model_status !== "active";
  const checkbox = el("input", {
    type: "checkbox",
    checked,
    disabled,
    onchange: (e) => {
      state.userTouchedModels = true;
      if (e.target.checked) state.selectedModelIds.add(model.id);
      else state.selectedModelIds.delete(model.id);
      label.classList.toggle("checked", e.target.checked);
      const countEl = document.getElementById("selected-model-count");
      if (countEl) countEl.textContent = `${state.selectedModelIds.size} selected`;
    },
  });

  const label = el(
    "label",
    { class: `model-option${checked ? " checked" : ""}${disabled ? " disabled" : ""}` },
    [
      checkbox,
      el("div", {}, [
        el("div", { class: "model-name" }, model.canonical_model_id),
        el("div", { class: "model-meta" }, [
          el("span", { class: pricingBadgeClass(model.pricing_classification) }, pricingLabel(model.pricing_classification)),
          model.context_window ? ` · ${model.context_window.toLocaleString("en-US")} ctx` : "",
          disabled ? " · unavailable" : "",
        ]),
      ]),
    ]
  );
  return label;
}

function buildRunBar(root, projectId, state) {
  const canRun = () =>
    state.question.trim().length > 0 &&
    state.selectedAgentId &&
    state.selectedModelIds.size >= 2 &&
    !state.submitting;

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

  // Re-evaluate the disabled state on every interaction inside this render
  // pass by re-checking right before click too (cheap, no extra render loop).
  const bar = el("div", { class: "row between" }, [statusLine, runButton]);
  root.addEventListener("input", () => {
    runButton.disabled = !canRun();
  });
  root.addEventListener("change", () => {
    runButton.disabled = !canRun();
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
    const selected = state.models.filter((m) => state.selectedModelIds.has(m.id));
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
