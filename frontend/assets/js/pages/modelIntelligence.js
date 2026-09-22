// Model Intelligence (MA8.3) — "How are my Agents' models performing over
// time, what is the router learning, why was a model chosen, and what is it
// costing?" A companion to the Workflow Control Room (one run, right now),
// not a replacement for it.
//
// Read-only except for one explicit, confirmed control: turning evidence
// routing on or off. Every number comes from app.services.
// model_intelligence_service; every label from ../modelIntelligence.js.

import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import { formatDateTime, formatLatency, pricingBadgeClass, pricingLabel } from "../format.js";
import { getProjectId } from "../state.js";
import { formatTokenCount } from "../runState.js";
import {
  EMPTY_TEXT,
  NOT_ENOUGH_HISTORY,
  choiceComparison,
  costBreakdown,
  costShort,
  evidenceStatusLabel,
  exclusionText,
  issueKindLabel,
  modeView,
  outcomeLabel,
  policyView,
  qualityView,
  reliabilityView,
  roleLabel,
  sectionState,
  strategyLabel,
  toggleEvidenceRouting,
  tokenLine,
  yesNo,
} from "../modelIntelligence.js";

const BASE = "/model-intelligence";
const PAGE_SIZE = 25;

function q(projectId, extra = {}) {
  const params = new URLSearchParams({ project_id: projectId, ...extra });
  return params.toString();
}

function kv(label, value) {
  return el("div", { class: "run-kv" }, [
    el("span", { class: "hint run-kv-label" }, label),
    el("span", { class: "run-kv-value" }, value == null || value === "" ? "—" : value),
  ]);
}

function stateBlock(state, emptyText) {
  if (state.kind === "loading") return el("p", {}, [el("span", { class: "spinner" }), " Loading…"]);
  if (state.kind === "error") return el("div", { class: "error-banner" }, state.text);
  if (state.kind === "empty") return el("p", { class: "hint" }, emptyText);
  return null;
}

function metric(label, value, hint = null) {
  return el("div", {}, [
    el("div", { class: "metric-label" }, label),
    el("div", { class: "metric-value" }, value),
    hint ? el("div", { class: "hint" }, hint) : null,
  ]);
}

// =========================================================================================================
// dashboard
// =========================================================================================================

export async function renderModelIntelligence(root) {
  const s = {
    projectId: null,
    summary: { loading: true },
    roles: { loading: true },
    issues: { loading: true },
    decisions: { loading: true, items: [], offset: 0, hasMore: false },
    policy: { loading: true },
    confirmToggle: false,
    toggling: false,
    toggleError: null,
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

  const load = (key, path, apply) =>
    api
      .get(path)
      .then((data) => {
        s[key] = apply(data);
      })
      .catch((error) => {
        s[key] = { error };
      })
      .finally(redraw);

  load("policy", `${BASE}/evidence-policy`, (data) => ({ data }));
  load("summary", `${BASE}/summary?${q(s.projectId)}`, (data) => ({ data }));
  load("roles", `${BASE}/roles?${q(s.projectId)}`, (data) => ({ data, items: data.entries }));
  load("issues", `${BASE}/issues?${q(s.projectId)}`, (data) => ({ items: data }));
  loadDecisions(s, redraw, 0);

  return () => {
    s.alive = false;
  };
}

function loadDecisions(s, redraw, offset) {
  if (offset === 0) s.decisions = { loading: true, items: [], offset: 0, hasMore: false };
  else s.decisions.loadingMore = true;
  redraw();
  api
    .get(`${BASE}/routing-decisions?${q(s.projectId, { limit: PAGE_SIZE, offset })}`)
    .then((page) => {
      const items = offset === 0 ? page.items : [...s.decisions.items, ...page.items];
      s.decisions = { items, offset: offset + page.items.length, hasMore: page.has_more };
    })
    .catch((error) => {
      s.decisions = { ...s.decisions, loading: false, loadingMore: false, error };
    })
    .finally(redraw);
}

function draw(root, s) {
  clear(root);
  mount(
    root,
    el("div", { class: "stack" }, [
      el("div", { class: "page-header" }, [
        el("h1", {}, "Model Intelligence"),
        el(
          "div",
          { class: "subtitle" },
          "Last 30 days — which models your Agents use, how reliable they have been, what evaluations found, what it cost, and why each model was chosen. For what one workflow is doing right now, open its Control Room."
        ),
      ]),
      buildPolicyCard(root, s),
      buildSummary(s),
      buildRoles(s),
      buildIssues(s),
      buildDecisions(root, s),
    ])
  );
}

// -- evidence routing control ------------------------------------------------------------------------------

function buildPolicyCard(root, s) {
  const card = el("div", { class: "card" }, []);
  card.appendChild(el("h2", {}, "Evidence-based routing"));
  if (s.policy.loading) {
    card.appendChild(el("p", {}, [el("span", { class: "spinner" }), " Loading…"]));
    return card;
  }
  if (s.policy.error) {
    card.appendChild(el("div", { class: "error-banner" }, s.policy.error.message || "Could not load the policy."));
    return card;
  }
  const view = policyView(s.policy.data);
  card.appendChild(el("div", { class: "row" }, [el("span", {}, "Status:"), el("span", { class: view.badgeClass }, view.label)]));
  card.appendChild(el("p", { class: "hint" }, view.summary));
  view.facts.forEach(([label, value]) => card.appendChild(kv(label, value)));

  if (s.toggleError) card.appendChild(el("div", { class: "error-banner" }, s.toggleError));
  if (!view.actionLabel) return card;

  const flip = async (confirmed) => {
    s.toggleError = null;
    if (confirmed) s.toggling = true;
    const result = await toggleEvidenceRouting({ api, current: s.policy.data, confirmed });
    s.toggling = false;
    if (result.needsConfirm) s.confirmToggle = true;
    else {
      s.confirmToggle = false;
      if (result.ok) {
        s.policy = { data: result.status };
        s.summary = s.summary.data ? { data: { ...s.summary.data, evidence_policy: result.status } } : s.summary;
      } else s.toggleError = result.error;
    }
    draw(root, s);
  };

  if (s.confirmToggle) {
    card.appendChild(
      el("div", { class: "studio-confirm", role: "group", "aria-label": view.confirmTitle }, [
        el("p", {}, [el("strong", {}, `${view.confirmTitle} `), view.confirmText]),
        el("div", { class: "row" }, [
          el(
            "button",
            { type: "button", class: "small", disabled: s.toggling, onclick: () => flip(true) },
            s.toggling ? "Saving…" : `Yes, ${view.actionLabel.toLowerCase()}`
          ),
          el(
            "button",
            {
              type: "button",
              class: "small",
              disabled: s.toggling,
              onclick: () => {
                s.confirmToggle = false;
                draw(root, s);
              },
            },
            "Keep it as it is"
          ),
        ]),
      ])
    );
  } else {
    card.appendChild(el("div", { class: "row" }, [el("button", { type: "button", onclick: () => flip(false) }, view.actionLabel)]));
  }
  return card;
}

// -- summary ------------------------------------------------------------------------------------------------

function buildSummary(s) {
  const card = el("div", { class: "card" }, [el("h2", {}, "Last 30 days")]);
  const state = sectionState({ loading: s.summary.loading, error: s.summary.error, items: [1] });
  const block = stateBlock(state);
  if (block) {
    card.appendChild(block);
    return card;
  }
  const d = s.summary.data;
  card.appendChild(
    el("div", { class: "totals-bar" }, [
      metric("Agent runs", formatTokenCount(d.agent_runs)),
      metric("Model calls", formatTokenCount(d.model_calls), `${d.completed_calls} completed · ${d.failed_calls} failed`),
      metric("Reliability issues", formatTokenCount(d.reliability_issues), "provider timeouts, connection errors, invalid responses"),
      metric("Tokens", formatTokenCount(d.tokens_total), `${formatTokenCount(d.tokens_in)} in · ${formatTokenCount(d.tokens_out)} out`),
      el("div", {}, [
        el("div", { class: "metric-label" }, "Cost"),
        ...costBreakdown(d.cost).map((line) => el("div", { class: line.kind === "unknown" ? "hint" : "metric-value" }, line.text)),
      ]),
      metric("Evaluated runs", formatTokenCount(d.evaluated_runs)),
    ])
  );
  return card;
}

// -- agent role x model --------------------------------------------------------------------------------------

function buildRoles(s) {
  const card = el("div", { class: "card" }, [
    el("h2", {}, "Agents & models"),
    el(
      "p",
      { class: "hint" },
      "What each Agent role has run on. Reliability (did the provider answer?) and quality evidence (what evaluations found) are shown separately — neither is a score."
    ),
  ]);
  const state = sectionState({ loading: s.roles.loading, error: s.roles.error, items: s.roles.items });
  const block = stateBlock(state, EMPTY_TEXT.roles);
  if (block) {
    card.appendChild(block);
    return card;
  }
  const t = s.roles.data.thresholds;
  card.appendChild(
    el(
      "p",
      { class: "hint" },
      `“${NOT_ENOUGH_HISTORY}” means fewer than ${t.min_observations} calls (reliability) or ${t.min_evaluated_runs} evaluated runs (quality) — evidence routing treats that model as neutral.`
    )
  );
  let lastRole = null;
  const rows = s.roles.items.map((entry) => {
    const reliability = reliabilityView(entry);
    const quality = qualityView(entry);
    const showRole = entry.agent_role !== lastRole;
    lastRole = entry.agent_role;
    return el("tr", {}, [
      el("td", {}, showRole ? el("strong", {}, roleLabel(entry.agent_role)) : ""),
      el("td", {}, [el("div", {}, entry.canonical_model_id || "(unknown model)"), el("div", { class: "hint" }, entry.provider_name || "")]),
      el("td", {}, [el("div", {}, reliability.text), el("div", { class: "hint" }, reliability.standing)]),
      el("td", {}, [el("div", {}, quality.text), el("div", { class: "hint" }, quality.standing)]),
      el("td", {}, formatLatency(entry.average_latency_ms)),
      el("td", {}, tokenLine(entry)),
      el("td", {}, costShort(entry.cost)),
      el("td", { class: "hint" }, formatDateTime(entry.last_used_at)),
    ]);
  });
  card.appendChild(
    el("table", {}, [
      el("thead", {}, el("tr", {}, ["Role", "Model", "Reliability", "Quality evidence", "Avg latency", "Tokens", "Cost", "Last used"].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows),
    ])
  );
  if (s.roles.data.truncated) card.appendChild(el("p", { class: "hint" }, "Showing the first 200 role/model combinations."));
  return card;
}

// -- issues ----------------------------------------------------------------------------------------------------

function buildIssues(s) {
  const card = el("div", { class: "card" }, [
    el("h2", {}, "Recent model / provider issues"),
    el("p", { class: "hint" }, "A provider failure says the call did not come back usable. It is not evidence that the model's answers are poor."),
  ]);
  const state = sectionState({ loading: s.issues.loading, error: s.issues.error, items: s.issues.items });
  const block = stateBlock(state, EMPTY_TEXT.issues);
  if (block) {
    card.appendChild(block);
    return card;
  }
  card.appendChild(
    el("table", {}, [
      el("thead", {}, el("tr", {}, ["Time", "Agent", "Model", "Issue"].map((h) => el("th", {}, h)))),
      el(
        "tbody",
        {},
        s.issues.items.map((issue) =>
          el("tr", {}, [
            el("td", { class: "hint" }, formatDateTime(issue.occurred_at)),
            el("td", {}, [el("div", {}, issue.agent_name || "—"), el("div", { class: "hint" }, roleLabel(issue.agent_role))]),
            el("td", {}, [el("div", {}, issue.canonical_model_id || "—"), el("div", { class: "hint" }, issue.provider_name || "")]),
            el("td", {}, [el("div", {}, issue.label), el("div", { class: "hint" }, `${issueKindLabel(issue.kind)} · ${issue.category || "unknown"}`)]),
          ])
        )
      ),
    ])
  );
  return card;
}

// -- routing decisions ----------------------------------------------------------------------------------------

function decisionRow(decision) {
  const mode = modeView(decision);
  return el(
    "tr",
    {
      class: "clickable",
      style: "cursor:pointer",
      tabindex: "0",
      title: "Why this model?",
      onclick: () => {
        window.location.hash = `#/model-intelligence/decisions/${encodeURIComponent(decision.id)}`;
      },
      onkeydown: (e) => {
        if (e.key === "Enter") window.location.hash = `#/model-intelligence/decisions/${encodeURIComponent(decision.id)}`;
      },
    },
    [
      el("td", { class: "hint" }, formatDateTime(decision.created_at)),
      el("td", {}, roleLabel(decision.agent_role)),
      el("td", {}, el("span", { class: mode.mode === "MANUAL" ? "badge badge-neutral" : "badge badge-running" }, mode.title)),
      el("td", {}, decision.selected_canonical_model_id || "—"),
      el("td", {}, strategyLabel(decision.routing_strategy)),
      el("td", {}, evidenceStatusLabel(decision.evidence_status, { isManual: decision.is_manual })),
      el("td", {}, decision.fallback_used === true ? "Paid fallback" : "—"),
      el("td", {}, outcomeLabel(decision)),
    ]
  );
}

function buildDecisions(root, s) {
  const card = el("div", { class: "card" }, [
    el("h2", {}, "Recent routing decisions"),
    el("p", { class: "hint" }, "Open a decision to see why that model was chosen, what else was eligible and what was excluded."),
  ]);
  const d = s.decisions;
  const state = sectionState({ loading: d.loading, error: d.error && !d.items.length ? d.error : null, items: d.items });
  const block = stateBlock(state, EMPTY_TEXT.decisions);
  if (block) {
    card.appendChild(block);
    return card;
  }
  card.appendChild(
    el("table", {}, [
      el(
        "thead",
        {},
        el("tr", {}, ["Time", "Agent role", "Mode / policy", "Selected model", "Strategy", "Evidence", "Fallback", "Outcome"].map((h) => el("th", {}, h)))
      ),
      el("tbody", {}, d.items.map(decisionRow)),
    ])
  );
  if (d.error) card.appendChild(el("div", { class: "error-banner" }, d.error.message || "Could not load more decisions."));
  if (d.hasMore) {
    card.appendChild(
      el(
        "button",
        { type: "button", class: "small", disabled: d.loadingMore, onclick: () => loadDecisions(s, () => s.alive && draw(root, s), d.offset) },
        d.loadingMore ? "Loading…" : "Show older decisions"
      )
    );
  }
  return card;
}

// =========================================================================================================
// "Why this model?"
// =========================================================================================================

export async function renderRoutingDecision(root, params) {
  mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading decision…"]));
  try {
    const projectId = await getProjectId();
    const detail = await api.get(`${BASE}/routing-decisions/${encodeURIComponent(params.id)}?${q(projectId)}`);
    mount(root, buildDecisionDetail(detail));
  } catch (err) {
    mount(root, el("div", { class: "stack" }, [backLink(), el("div", { class: "error-banner" }, err.message || "Could not load this decision.")]));
  }
}

// From the Control Room: the routing decision(s) recorded for one Agent Run.
export async function renderAgentRunDecision(root, params) {
  mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading decision…"]));
  try {
    const projectId = await getProjectId();
    const page = await api.get(`${BASE}/routing-decisions?${q(projectId, { agent_run_id: params.id, limit: 10 })}`);
    if (!page.items.length) {
      mount(
        root,
        el("div", { class: "stack" }, [
          backLink(),
          el("div", { class: "card" }, el("p", { class: "hint" }, "No routing decision was recorded for this Agent run (it may predate routing audit).")),
        ])
      );
      return;
    }
    const detail = await api.get(`${BASE}/routing-decisions/${encodeURIComponent(page.items[0].id)}?${q(projectId)}`);
    const view = buildDecisionDetail(detail);
    if (page.items.length > 1) {
      view.insertBefore(
        el("div", { class: "card" }, [
          el("p", { class: "hint" }, `This Agent run routed ${page.items.length} times (one per attempt). Showing the latest; earlier attempts:`),
          ...page.items.slice(1).map((item) =>
            el("div", {}, el("a", { href: `#/model-intelligence/decisions/${encodeURIComponent(item.id)}` }, `${formatDateTime(item.created_at)} — ${item.selected_canonical_model_id || outcomeLabel(item)}`))
          ),
        ]),
        view.children[2] || null
      );
    }
    mount(root, view);
  } catch (err) {
    mount(root, el("div", { class: "stack" }, [backLink(), el("div", { class: "error-banner" }, err.message || "Could not load this decision.")]));
  }
}

function backLink() {
  return el("a", { href: "#/model-intelligence", class: "studio-back" }, "← Model Intelligence");
}

function candidateLine(candidate, extra = null) {
  return el("div", { class: "run-kv" }, [
    el("span", { class: "run-kv-value" }, [
      el("div", {}, candidate.canonical_model_id || candidate.provider_model_id),
      el("div", { class: "hint" }, candidate.provider_name || ""),
    ]),
    el("span", {}, [
      candidate.pricing_classification ? el("span", { class: pricingBadgeClass(candidate.pricing_classification) }, pricingLabel(candidate.pricing_classification)) : null,
      extra ? el("div", { class: "hint" }, extra) : null,
    ]),
  ]);
}

function profileText(profile) {
  const reliability = reliabilityView(profile);
  const quality = qualityView(profile);
  const bits = [`${reliability.text} (${reliability.standing})`, `${quality.text} (${quality.standing})`];
  if (profile.median_latency_ms) bits.push(`median latency ${formatLatency(profile.median_latency_ms)}`);
  if (profile.median_tokens_in != null) bits.push(`median tokens ${profile.median_tokens_in} in / ${profile.median_tokens_out} out`);
  const cost = profile.cost_observations || {};
  bits.push(`cost records: ${cost.exact || 0} exact, ${cost.estimated || 0} estimated, ${cost.unknown || 0} unknown`);
  return bits.join(" · ");
}

export function buildDecisionDetail(detail) {
  const mode = modeView(detail);
  const comparison = choiceComparison(detail);
  const evidence = detail.evidence;

  const selectedCard = el("div", { class: "card" }, [
    el("h2", {}, "Why this model?"),
    el("div", { class: "row" }, [
      el("span", { class: mode.mode === "MANUAL" ? "badge badge-neutral" : "badge badge-running" }, mode.title),
      el("strong", {}, detail.selected_canonical_model_id || outcomeLabel(detail)),
      detail.selected_pricing_classification
        ? el("span", { class: pricingBadgeClass(detail.selected_pricing_classification) }, pricingLabel(detail.selected_pricing_classification))
        : null,
    ]),
    el("p", {}, mode.note),
    kv("Agent", `${detail.agent_name || "—"} (${roleLabel(detail.agent_role)})`),
    kv("Decided at", formatDateTime(detail.created_at)),
    kv("Routing mode", mode.mode),
    detail.is_manual ? null : kv("Requested policy", mode.title.replace(/^AUTO · /, "")),
    kv("Strategy", strategyLabel(detail.routing_strategy)),
    kv("Evidence", evidenceStatusLabel(evidence && evidence.status, { isManual: detail.is_manual })),
    detail.is_manual ? null : kv("Free preference satisfied", yesNo(detail.free_preference_satisfied)),
    detail.is_manual ? null : kv("Paid fallback used", yesNo(detail.fallback_used)),
    kv("Eligible candidates", String(detail.eligible_count ?? "—")),
    kv("Excluded candidates", String(detail.excluded_count ?? "—")),
    detail.outcome === "failed" ? kv("Failure", outcomeLabel(detail)) : null,
    el("details", {}, [el("summary", {}, "Recorded explanation"), el("p", { class: "hint", style: "white-space:pre-wrap" }, detail.rationale || "—")]),
  ]);

  const parts = [backLink(), selectedCard];

  if (comparison) {
    parts.push(
      el("div", { class: "card" }, [
        el("h2", {}, "Deterministic rules vs evidence"),
        kv("Deterministic choice", comparison.deterministic),
        kv(comparison.evidenceUsed ? "Evidence choice" : "Selected", comparison.selected),
        kv("Evidence changed selection", comparison.changed),
      ])
    );
  }

  if (evidence && evidence.status && !detail.is_manual && evidence.status !== "disabled") {
    const cfg = evidence.config || {};
    const card = el("div", { class: "card" }, [
      el("h2", {}, "Evidence at decision time"),
      el("p", { class: "hint" }, "Captured when the model was chosen — not recomputed from today's data."),
      kv("Strategy", `${strategyLabel(evidence.strategy)}${evidence.router_policy_version ? ` (policy v${evidence.router_policy_version})` : ""}`),
      cfg.history_window_days ? kv("History window", `${cfg.history_window_days} days`) : null,
      cfg.min_observations ? kv("Minimum evidence", `${cfg.min_observations} calls (reliability) · ${cfg.min_evaluated_runs} evaluated runs (quality)`) : null,
      kv("Agent role", roleLabel(evidence.agent_role)),
    ]);
    if (evidence.status === "unavailable") card.appendChild(el("p", {}, "Evidence could not be loaded, so the deterministic rules were used."));
    if (!(evidence.profiles || []).length) card.appendChild(el("p", {}, `${NOT_ENOUGH_HISTORY} — no candidate had relevant history for this role.`));
    (evidence.profiles || []).forEach((profile) => card.appendChild(kv(profile.canonical_model_id || profile.provider_model_id, profileText(profile))));
    parts.push(card);
  }

  const eligible = detail.eligible_candidates || [];
  parts.push(
    el("div", { class: "card" }, [
      el("h2", {}, "Candidates"),
      el("h3", {}, "Selected"),
      eligible[0] ? candidateLine(eligible[0]) : el("p", { class: "hint" }, "No model was selected."),
      el("h3", {}, "Eligible alternatives"),
      eligible.length > 1 ? el("div", {}, eligible.slice(1).map((c) => candidateLine(c))) : el("p", { class: "hint" }, "None."),
      eligible.length < (detail.eligible_count || 0) ? el("p", { class: "hint" }, `Showing the first ${eligible.length} of ${detail.eligible_count}.`) : null,
      el("h3", {}, "Excluded"),
      (detail.excluded_candidates || []).length
        ? el("div", {}, detail.excluded_candidates.map((c) => candidateLine(c, exclusionText(c))))
        : el("p", { class: "hint" }, "None."),
      (detail.excluded_candidates || []).length < (detail.excluded_count || 0)
        ? el("p", { class: "hint" }, `Showing the first ${detail.excluded_candidates.length} of ${detail.excluded_count}.`)
        : null,
    ])
  );
  return el("div", { class: "stack" }, parts);
}
