// Provider Explorer (AIL.1B) — thin wrapper over provider facts: identity,
// offered models, catalog freshness, recent refresh history.
// Read-only composition over MA2 registry facts.

import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import { formatDateTime, pricingBadgeClass, pricingLabel } from "../format.js";

const BASE = "/ail";

export async function renderProviderExplorer(root, params) {
  const s = {
    providerId: params.id,
    provider: { loading: true },
    alive: true,
  };
  const redraw = () => s.alive && draw(root, s);
  redraw();

  api
    .get(`${BASE}/providers/${s.providerId}`)
    .then((data) => {
      s.provider = { data };
    })
    .catch((error) => {
      s.provider = { error };
    })
    .finally(redraw);

  return () => {
    s.alive = false;
  };
}

function draw(root, s) {
  clear(root);

  if (s.provider.error) {
    mount(root, el("div", { class: "error-banner" }, s.provider.error.message || "Failed to load provider."));
    return;
  }

  if (!s.provider.data) {
    mount(root, el("div", {}, [el("span", { class: "spinner" }), " Loading…"]));
    return;
  }

  const { data: provider } = s.provider;
  const container = el("div", { class: "stack" });

  container.appendChild(
    el("div", { class: "page-header" }, [
      el("h1", {}, provider.identity.name),
      el("div", { class: "subtitle" }, `${provider.identity.type} provider — ${provider.models.length} model${provider.models.length === 1 ? "" : "s"}`),
    ])
  );

  if (provider.freshness?.last_model_refresh_at) {
    container.appendChild(
      el("div", { class: "card" }, [
        el("h2", {}, "Freshness"),
        el("p", {}, [
          `Last model refresh: `,
          el("strong", {}, formatDateTime(provider.freshness.last_model_refresh_at)),
        ]),
      ])
    );
  }

  if (provider.recent_refreshes?.length) {
    container.appendChild(
      el("div", { class: "card" }, [
        el("h2", {}, "Recent Refresh History"),
        el(
          "div",
          { class: "stack" },
          provider.recent_refreshes.slice(0, 5).map((r) =>
            el("div", { class: "card card-nested" }, [
              el("div", { class: "row between" }, [
                el("span", {}, formatDateTime(r.started_at)),
                el("span", { class: `badge ${r.status === "success" ? "badge-done" : "badge-failed"}` }, r.status),
              ]),
              el("div", { class: "card-body" }, [
                el("p", {}, [
                  `Discovered: ${r.models_discovered}, Added: ${r.models_added}, Updated: ${r.models_updated}, Unavailable: ${r.models_unavailable}`,
                ]),
                r.error && el("p", { class: "error-text" }, `Error: ${JSON.stringify(r.error)}`),
              ]),
            ])
          )
        ),
      ])
    );
  }

  if (provider.models?.length) {
    container.appendChild(
      el("div", { class: "card" }, [
        el("h2", {}, "Offered Models"),
        el(
          "div",
          { class: "stack" },
          provider.models.map((m) =>
            el("div", { class: "card card-nested" }, [
              el("div", { class: "row between" }, [
                el("h3", {}, m.canonical_model_id),
                el("span", { class: `badge ${pricingBadgeClass(m.pricing_classification)}` }, pricingLabel(m.pricing_classification)),
              ]),
              el("div", { class: "card-body" }, [
                el("p", {}, `Status: ${m.model_status}, Availability: ${m.availability_status}`),
                m.cost_input_per_mtok &&
                  el("p", {}, `Pricing: ${m.cost_input_per_mtok} / ${m.cost_output_per_mtok} ${m.currency}`),
                el("p", { class: "hint" }, `Last refreshed: ${m.last_refreshed_at ? formatDateTime(m.last_refreshed_at) : "—"}`),
              ]),
            ])
          )
        ),
      ])
    );
  }

  mount(root, container);
}
