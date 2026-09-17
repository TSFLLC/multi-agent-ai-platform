// Models / Provider Setup (Section 3, PV-02) — replaces the PowerShell
// workflow for connecting OpenRouter and refreshing the model catalog.
// The raw API key only ever goes into POST /providers, which stores it
// through the real SecretService boundary server-side and never echoes it
// back (app.schemas.providers.ProviderCreate/ProviderRead) — this page
// never persists or displays the key itself.

import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import { pricingBadgeClass, pricingLabel, formatDateTime } from "../format.js";

export async function renderModels(root) {
  const state = { providers: [], models: [], busy: false, error: null, notice: null };
  await load(root, state);
}

async function load(root, state, showSpinner = true) {
  if (showSpinner) mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading providers and models…"]));
  try {
    const [providers, models] = await Promise.all([api.get("/providers"), api.get("/models")]);
    state.providers = providers;
    state.models = models;
  } catch (err) {
    mount(root, el("div", { class: "error-banner" }, err.message || "Failed to load providers/models."));
    return;
  }
  draw(root, state);
}

function draw(root, state) {
  clear(root);
  const container = el("div", { class: "stack" });
  container.appendChild(
    el("div", { class: "page-header" }, [
      el("h1", {}, "Models"),
      el("div", { class: "subtitle" }, "Connect a provider and refresh its model catalog — visually, no API calls by hand."),
    ])
  );

  if (state.error) container.appendChild(el("div", { class: "error-banner" }, state.error));
  if (state.notice) container.appendChild(el("div", { class: "card" }, el("p", {}, state.notice)));

  const openrouter = state.providers.find((p) => p.type === "openrouter");
  container.appendChild(openrouter ? buildConnectedCard(root, state, openrouter) : buildConnectCard(root, state));
  container.appendChild(buildModelTable(state, openrouter));

  mount(root, container);
}

function buildConnectCard(root, state) {
  let apiKey = "";
  const input = el("input", {
    type: "password",
    placeholder: "sk-or-...",
    oninput: (e) => {
      apiKey = e.target.value;
    },
  });
  const connectButton = el(
    "button",
    {
      class: "primary",
      onclick: async () => {
        if (!apiKey.trim()) {
          state.error = "Enter an OpenRouter API key first.";
          draw(root, state);
          return;
        }
        connectButton.disabled = true;
        connectButton.textContent = "Connecting…";
        state.error = null;
        try {
          const provider = await api.post("/providers", { type: "openrouter", name: "OpenRouter", api_key: apiKey });
          await api.post(`/providers/${provider.id}/test-connection`);
          const refresh = await api.post(`/models/refresh?provider_id=${encodeURIComponent(provider.id)}`);
          state.notice =
            refresh.status === "success"
              ? `Connected. Discovered ${refresh.models_discovered} model(s).`
              : "Connected, but the first catalog refresh failed — check the API key and try Refresh Models.";
          await load(root, state, false);
        } catch (err) {
          state.error = err.message || "Failed to connect OpenRouter.";
          draw(root, state);
        }
      },
    },
    "Connect"
  );

  return el("div", { class: "card" }, [
    el("div", { class: "row between" }, [el("h2", {}, "OpenRouter"), el("span", { class: "badge badge-unknown" }, "Not connected")]),
    el("p", {}, "Enter your OpenRouter API key once. It is stored through the platform's local secret store and never shown again."),
    el("label", {}, "API key"),
    el("div", { class: "row" }, [input, connectButton]),
  ]);
}

function buildConnectedCard(root, state, provider) {
  const healthClass = provider.health_status === "up" ? "badge-done" : provider.health_status === "degraded" ? "badge-paid" : "badge-failed";

  const testButton = el(
    "button",
    {
      class: "small",
      onclick: async (e) => {
        e.target.disabled = true;
        e.target.textContent = "Testing…";
        try {
          await api.post(`/providers/${provider.id}/test-connection`);
          await load(root, state, false);
        } catch (err) {
          state.error = err.message;
          draw(root, state);
        }
      },
    },
    "Test Connection"
  );

  const refreshButton = el(
    "button",
    {
      class: "primary small",
      onclick: async (e) => {
        e.target.disabled = true;
        e.target.textContent = "Refreshing…";
        try {
          const refresh = await api.post(`/models/refresh?provider_id=${encodeURIComponent(provider.id)}`);
          state.notice =
            refresh.status === "success"
              ? `Refreshed: ${refresh.models_discovered} discovered, ${refresh.models_added} added, ${refresh.models_updated} updated.`
              : "Refresh failed — check the connection and try again.";
          await load(root, state, false);
        } catch (err) {
          state.error = err.message;
          draw(root, state);
        }
      },
    },
    "Refresh Models"
  );

  return el("div", { class: "card" }, [
    el("div", { class: "row between" }, [
      el("h2", {}, "OpenRouter — Connected"),
      el("span", { class: `badge ${healthClass}` }, provider.health_status),
    ]),
    el("div", { class: "row" }, [testButton, refreshButton]),
  ]);
}

function buildModelTable(state, provider) {
  const models = provider ? state.models.filter((m) => m.provider_id === provider.id) : state.models;
  if (!models.length) {
    return el("div", { class: "card" }, [
      el("h2", {}, "Catalog"),
      el("div", { class: "empty-state" }, [
        el("div", { class: "icon" }, "🧩"),
        el("p", {}, "No models yet. Connect a provider and refresh the catalog above."),
      ]),
    ]);
  }

  const table = el("table", {}, [
    el("thead", {}, el("tr", {}, [el("th", {}, "Model"), el("th", {}, "Pricing"), el("th", {}, "Status"), el("th", {}, "Context"), el("th", {}, "Refreshed")])),
    el(
      "tbody",
      {},
      models.map((m) =>
        el("tr", {}, [
          el("td", {}, m.canonical_model_id),
          el("td", {}, el("span", { class: pricingBadgeClass(m.pricing_classification) }, pricingLabel(m.pricing_classification))),
          el("td", {}, el("span", { class: m.model_status === "active" ? "badge badge-done" : "badge badge-neutral" }, m.model_status)),
          el("td", {}, m.context_window ? m.context_window.toLocaleString("en-US") : "—"),
          el("td", {}, formatDateTime(m.last_refreshed_at)),
        ])
      )
    ),
  ]);

  return el("div", { class: "card" }, [el("h2", {}, "Catalog"), table]);
}
