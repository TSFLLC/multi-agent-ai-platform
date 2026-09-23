// Model Explorer (AIL.1B) — inspect model facts, provider availability,
// pricing, our usage, reliability/evaluation evidence, catalog change history.
// Composed read-only view over the existing registry (MA2) and platform
// observations (MA6/MA8) — no new source of truth, no second router.

import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import { formatDateTime, pricingBadgeClass, pricingLabel, formatLatency } from "../format.js";
import { getProjectId } from "../state.js";
import { evidenceStatusLabel, reliabilityView, qualityView, NOT_ENOUGH_HISTORY } from "../modelIntelligence.js";

const BASE = "/ail";

function q(projectId, extra = {}) {
  const params = new URLSearchParams({ project_id: projectId, ...extra });
  return params.toString();
}

function kv(label, value, hint = null) {
  return el("div", { class: "run-kv" }, [
    el("span", { class: "hint run-kv-label" }, label),
    el("span", { class: "run-kv-value" }, value == null || value === "" ? "—" : value),
    hint ? el("span", { class: "hint" }, hint) : null,
  ]);
}

export async function renderModelExplorer(root, params) {
  const s = {
    projectId: null,
    modelId: params.id,
    model: { loading: true },
    history: { loading: true, items: [] },
    alive: true,
  };
  const redraw = () => s.alive && draw(root, s);
  redraw();

  try {
    s.projectId = await getProjectId();
  } catch (err) {
    mount(root, el("div", { class: "error-banner" }, err.message || "Failed to load the project."));
    return () => {};
  }

  Promise.all([
    api
      .get(`${BASE}/models/${s.modelId}?${q(s.projectId)}`)
      .then((data) => {
        s.model = { data };
      })
      .catch((error) => {
        s.model = { error };
      }),
    api
      .get(`${BASE}/models/${s.modelId}/history?limit=20`)
      .then((data) => {
        s.history = { items: data.items };
      })
      .catch(() => {
        s.history = { items: [] };
      }),
  ]).finally(redraw);

  return () => {
    s.alive = false;
  };
}

function draw(root, s) {
  clear(root);

  if (s.model.error) {
    mount(root, el("div", { class: "error-banner" }, s.model.error.message || "Failed to load model."));
    return;
  }

  if (!s.model.data) {
    mount(root, el("div", {}, [el("span", { class: "spinner" }), " Loading…"]));
    return;
  }

  const { data: model } = s.model;
  const container = el("div", { class: "stack" });

  container.appendChild(
    el("div", { class: "page-header" }, [
      el("h1", {}, model.identity.canonical_model_id),
      el("div", { class: "subtitle" }, `Identity, availability, usage, and reliability across your projects.`),
    ])
  );

  container.appendChild(buildIdentity(model.identity));
  container.appendChild(buildAvailability(model.availability));
  if (model.capabilities?.length) container.appendChild(buildCapabilities(model.capabilities));
  container.appendChild(buildOurEvidence(model.our_evidence));
  if (s.history.items.length) container.appendChild(buildHistory(s.history.items));

  mount(root, container);
}

function buildIdentity(identity) {
  return el("div", { class: "card" }, [
    el("h2", {}, "Identity"),
    el("div", { class: "card-body" }, [
      kv("Model", identity.canonical_model_id),
      kv("Family", identity.family || "—"),
      kv("Status", identity.status),
      kv("Context window", identity.context_window ? `${identity.context_window.toLocaleString()} tokens` : "—"),
      kv("Input modalities", identity.input_modalities ? identity.input_modalities.join(", ") : "—"),
      kv("Output modalities", identity.output_modalities ? identity.output_modalities.join(", ") : "—"),
      kv("Tool calling", identity.tool_calling_support || "—"),
      kv("Structured output", identity.structured_output_support ? "Yes" : "No"),
      kv("Vision", identity.vision_capability || "—"),
      kv("Reasoning", identity.reasoning_tier || "—"),
      kv("Coding", identity.coding_capability_tier || "—"),
      kv("Last refreshed", identity.last_refreshed_at ? formatDateTime(identity.last_refreshed_at) : "—"),
    ]),
  ]);
}

function buildAvailability(offerings) {
  if (!offerings?.length) return el("div", { class: "card" }, el("p", { class: "hint" }, "Not available through any provider."));

  return el("div", { class: "card" }, [
    el("h2", {}, `Offered through ${offerings.length} provider${offerings.length === 1 ? "" : "s"}`),
    el(
      "div",
      { class: "stack" },
      offerings.map((o) =>
        el("div", { class: "card card-nested" }, [
          el("div", { class: "row between" }, [
            el("h3", {}, o.provider_name),
            el("span", { class: `badge ${pricingBadgeClass(o.pricing_classification)}` }, pricingLabel(o.pricing_classification)),
          ]),
          el("div", { class: "card-body" }, [
            o.cost_input_per_mtok || o.cost_output_per_mtok
              ? kv(
                  "Pricing",
                  `${o.cost_input_per_mtok || "—"} / ${o.cost_output_per_mtok || "—"} ${o.currency}`,
                  "per million tokens"
                )
              : kv("Pricing", "Unknown"),
            kv("Status", o.availability_status),
            kv("Provider health", o.provider_health_status),
            kv("Last refreshed", o.last_refreshed_at ? formatDateTime(o.last_refreshed_at) : "—"),
          ]),
        ])
      )
    ),
  ]);
}

function buildCapabilities(capabilities) {
  if (!capabilities.length) return null;

  return el("div", { class: "card" }, [
    el("h2", {}, "Capabilities"),
    el(
      "div",
      { class: "grid" },
      capabilities.map((c) =>
        el("div", { class: "card card-nested" }, [
          el("div", { class: "run-kv-label" }, c.key),
          el("div", { class: "run-kv-value" }, `${c.value}${c.tier ? ` (${c.tier})` : ""}`),
        ])
      )
    ),
  ]);
}

function buildOurEvidence(evidence) {
  if (!evidence) {
    return el("div", { class: "card" }, [
      el("h2", {}, "Our Evidence"),
      el("p", { class: "hint" }, "No project selected. Select a project to see usage and reliability."),
    ]);
  }

  if (evidence.status === "not_in_registry") {
    return el("div", { class: "card" }, [
      el("h2", {}, "Our Evidence"),
      el("p", { class: "hint" }, "This model is not in the registry."),
    ]);
  }

  if (!evidence.by_provider_model) {
    return el("div", { class: "card" }, [
      el("h2", {}, "Our Evidence"),
      el("p", { class: "hint" }, "No data available."),
    ]);
  }

  const entries = Object.values(evidence.by_provider_model);
  if (!entries.length) {
    return el("div", { class: "card" }, [
      el("h2", {}, "Our Evidence"),
      el("p", { class: "hint" }, "Not in use yet."),
    ]);
  }

  return el("div", { class: "card" }, [
    el("h2", {}, `Our Evidence (${evidence.window_days} days)`),
    el(
      "div",
      { class: "stack" },
      entries.map((ev) => buildEvidenceEntry(ev))
    ),
  ]);
}

function buildEvidenceEntry(ev) {
  if (ev.status === "no_data") {
    return el("div", { class: "card card-nested" }, [
      el("p", { class: "hint" }, "Not enough history yet."),
      kv("Router selections", ev.router_selection_count),
    ]);
  }

  const exactUsd = ev.cost ? parseFloat(ev.cost.exact_usd || 0) : 0;
  const estimatedUsd = ev.cost ? parseFloat(ev.cost.estimated_usd || 0) : 0;
  const totalUsd = exactUsd + estimatedUsd;

  return el("div", { class: "card card-nested" }, [
    el("div", { class: "metrics-grid" }, [
      el("div", {}, [
        el("div", { class: "metric-label" }, "Calls (window)"),
        el("div", { class: "metric-value" }, `${ev.model_calls}`),
      ]),
      el("div", {}, [
        el("div", { class: "metric-label" }, "Completed"),
        el("div", { class: "metric-value" }, `${ev.completed_calls}`),
      ]),
      el("div", {}, [
        el("div", { class: "metric-label" }, "Tokens"),
        el("div", { class: "metric-value" }, `${(ev.tokens_in + ev.tokens_out).toLocaleString()}`),
      ]),
      el("div", {}, [
        el("div", { class: "metric-label" }, "Cost"),
        el("div", { class: "metric-value" }, totalUsd > 0 ? `$${totalUsd.toFixed(2)}` : "—"),
      ]),
      el("div", {}, [
        el("div", { class: "metric-label" }, "Reliability"),
        el("div", { class: "metric-value" }, reliabilityView(ev.reliability)),
      ]),
      el("div", {}, [
        el("div", { class: "metric-label" }, "Quality"),
        el("div", { class: "metric-value" }, qualityView(ev.quality)),
      ]),
    ]),
    el("div", { class: "card-body" }, [
      ev.cost &&
        kv(
          "Cost breakdown",
          [
            ev.cost.exact_calls > 0 && `${ev.cost.exact_calls} exact`,
            ev.cost.estimated_calls > 0 && `${ev.cost.estimated_calls} estimated`,
            ev.cost.unknown_calls > 0 && `${ev.cost.unknown_calls} unknown`,
          ]
            .filter(Boolean)
            .join(", "),
          "calls"
        ),
      ev.evaluated_runs > 0 && kv("Evaluations", `${ev.evaluated_runs}`, `${ev.findings.met} met, ${ev.findings.partial} partial, ${ev.findings.not_met} not met`),
      kv("Router selections", ev.router_selection_count),
    ]),
  ]);
}

function buildHistory(items) {
  if (!items.length) return null;

  return el("div", { class: "card" }, [
    el("h2", {}, "Catalog Change History"),
    el(
      "div",
      { class: "stack" },
      items.map((item) =>
        el("div", { class: "card card-nested" }, [
          el("div", { class: "row between" }, [
            el("span", {}, [
              el("strong", {}, item.provider_name),
              " · ",
              item.change_kinds?.join(", ") || "changed",
            ]),
            el("span", { class: "hint" }, formatDateTime(item.snapshotted_at)),
          ]),
          el("div", { class: "card-body" }, [
            item.pricing_input_per_mtok !== null &&
              kv(
                "Pricing",
                `${item.pricing_input_per_mtok || "—"} / ${item.pricing_output_per_mtok || "—"} ${item.currency}`,
                "per million tokens"
              ),
            item.context_window && kv("Context window", `${item.context_window.toLocaleString()} tokens`),
          ]),
        ])
      )
    ),
  ]);
}
