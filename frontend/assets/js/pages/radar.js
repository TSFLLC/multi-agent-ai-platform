import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import { formatDateTime, truncate } from "../format.js";
import { navigate } from "../router.js";

const LIMIT = 50;
const TODAY_CAP = 3;

const REASON_LABELS = {
  NEW_MODEL: "New model",
  PRICE_CHANGE: "Price change",
  CAPABILITY_CHANGE: "Capability change",
  CONTEXT_CHANGE: "Context change",
  STATUS_CHANGE: "Status change",
  RELATED_TO_INTEREST: "Related to your declared interest",
  RELATED_TO_LEARNING_PLAN: "Related to your learning plan",
  RELATED_TO_USED_MODEL: "Model used in an opted-in project",
  RELATED_TO_USED_PROVIDER: "Provider used in an opted-in project",
  RELATED_TO_PLATFORM_COMPONENT: "Related to a platform component",
  NEEDS_VERIFICATION: "Needs verification",
  CONFLICTING_CLAIMS: "Conflicting evidence",
  SOURCE_STALE: "Source may be stale",
  ATTENTION_RISING: "Attention rising",
  WATCH_TRIGGERED: "Watch trigger reached",
  UNREVIEWED: "Not reviewed",
  NEW_RESEARCH: "New research",
  NEW_BENCHMARK: "New benchmark",
};

const CLAIM_LABELS = {
  FACT: "Fact",
  PROVIDER_CLAIM: "Provider claim",
  RESEARCH_RESULT: "Research result",
  BENCHMARK_RESULT: "Benchmark result",
  COMMUNITY_SIGNAL: "Community signal",
  PLATFORM_OBSERVATION: "Platform observation",
  AI_EXPLANATION: "AI explanation",
};

const TYPE_LABELS = {
  model_release: "Model release",
  pricing: "Pricing",
  capability: "Capability",
  context: "Context",
  status: "Status",
  research: "Research",
  benchmark: "Benchmark",
  protocol: "Protocol",
  tooling: "Tooling",
};

export function reasonLabel(code) {
  return REASON_LABELS[code] || String(code || "Unknown reason").replaceAll("_", " ");
}

export function claimTypeLabel(type) {
  return CLAIM_LABELS[type] || type || "Unknown claim";
}

export function developmentTypeLabel(type) {
  return TYPE_LABELS[type] || type || "Development";
}

export function triageActionValues() {
  return ["IGNORE", "WATCH", "LEARN", "EXPERIMENT", "INVESTIGATE"];
}

export function friendlyLinkName(link) {
  return link?.name || link?.id || "Unknown";
}

export function verificationStepState(level, item) {
  const levels = ["CLAIMED", "DOCUMENTED", "AVAILABLE", "INDEPENDENTLY_MEASURED", "TESTED_BY_US"];
  const index = levels.indexOf(level);
  const position = levels.indexOf(item);
  return { current: item === level, reached: position >= 0 && position <= index };
}

export function visibleReasons(detail) {
  return (detail?.reason_codes || []).map(reasonLabel);
}

function dateValue(detail) {
  return new Date(detail?.development?.announced_at || detail?.development?.first_seen_at || 0).getTime() || 0;
}

export function sortRecent(details) {
  return [...details].sort((a, b) => dateValue(b) - dateValue(a));
}

export function groupToday(details) {
  const recent = sortRecent(details);
  const has = (d, code) => (d.reason_codes || []).includes(code);
  const by = (predicate) => recent.filter(predicate).slice(0, TODAY_CAP);
  const important = recent.filter((d) => !d.current_triage || d.current_triage.decision !== "IGNORE").slice(0, TODAY_CAP);
  return {
    important,
    newModels: by((d) => has(d, "NEW_MODEL") || d.development?.development_type === "model_release"),
    changes: by((d) => ["PRICE_CHANGE", "CAPABILITY_CHANGE", "CONTEXT_CHANGE", "STATUS_CHANGE"].some((c) => has(d, c))),
    learning: by((d) => has(d, "RELATED_TO_INTEREST") || has(d, "RELATED_TO_LEARNING_PLAN")),
    platform: by((d) => ["RELATED_TO_USED_MODEL", "RELATED_TO_USED_PROVIDER", "RELATED_TO_PLATFORM_COMPONENT"].some((c) => has(d, c))),
    verification: by((d) => has(d, "NEEDS_VERIFICATION") || d.verification_level === "CLAIMED"),
    pending: recent.filter((d) => !d.current_triage || has(d, "WATCH_TRIGGERED")).slice(0, TODAY_CAP),
  };
}

async function fetchDetails(rows) {
  const results = await Promise.allSettled(
    rows.map((row) => api.get(`/radar/developments/${encodeURIComponent(row.id)}/intelligence`))
  );
  return results.filter((result) => result.status === "fulfilled").map((result) => result.value);
}

function pageHeader(title, subtitle, actions = []) {
  return el("div", { class: "page-header row between" }, [
    el("div", {}, [el("h1", {}, title), el("div", { class: "subtitle" }, subtitle)]),
    el("div", { class: "row" }, actions),
  ]);
}

function stateCard(message, kind = "empty-state") {
  return el("div", { class: kind }, message);
}

function badge(text, extra = "badge-neutral") {
  return el("span", { class: `badge ${extra}` }, text);
}

function reasonChips(detail) {
  const reasons = visibleReasons(detail);
  return reasons.length
    ? el("div", { class: "chip-row radar-reasons" }, reasons.map((reason) => badge(reason)))
    : el("span", { class: "hint" }, "No deterministic relevance reason recorded.");
}

function dateLine(development) {
  return `${development.announced_at ? "Announced" : "First seen"} ${formatDateTime(development.announced_at || development.first_seen_at)}`;
}

function modelLinks(detail) {
  const links = detail.model_links || (detail.model_ids || []).map((id) => ({ id, name: id }));
  if (!links.length) return el("span", { class: "hint" }, "No linked models.");
  return el("div", { class: "chip-row" }, links.map((link) => el("a", { class: "link-chip", href: `#/models/${encodeURIComponent(link.id)}/explore` }, friendlyLinkName(link))));
}

function providerLinks(detail) {
  const links = detail.provider_links || (detail.provider_ids || []).map((id) => ({ id, name: id }));
  if (!links.length) return el("span", { class: "hint" }, "No linked providers.");
  return el("div", { class: "chip-row" }, links.map((link) => el("a", { class: "link-chip", href: `#/providers/${encodeURIComponent(link.id)}/explore` }, friendlyLinkName(link))));
}

function conceptLinks(detail) {
  const links = detail.concept_links || [];
  if (!links.length) return el("span", { class: "hint" }, "No linked Concepts.");
  return el("div", { class: "chip-row" }, links.map((link) => badge(`${link.concept_id} · ${String(link.state).toLowerCase()}`)));
}

function developmentCard(detail, { compact = false } = {}) {
  const d = detail.development;
  const triage = detail.current_triage?.decision || "Not decided";
  return el("article", { class: `card radar-card${compact ? " compact" : ""}` }, [
    el("div", { class: "row between" }, [
      el("div", {}, [
        el("div", { class: "eyebrow" }, developmentTypeLabel(d.development_type)),
        el("h2", {}, d.title),
      ]),
      badge(detail.verification_level || "Unknown"),
    ]),
    el("p", { class: "radar-date" }, dateLine(d)),
    reasonChips(detail),
    el("div", { class: "radar-meta-grid" }, [
      el("div", {}, [el("span", { class: "hint" }, "Freshness"), el("strong", {}, detail.freshness || "Unknown")]),
      el("div", {}, [el("span", { class: "hint" }, "Attention"), el("strong", {}, detail.attention_state || "UNKNOWN")]),
      el("div", {}, [el("span", { class: "hint" }, "Evidence"), el("strong", {}, `${detail.claims?.length || 0} claim${detail.claims?.length === 1 ? "" : "s"}`)]),
      el("div", {}, [el("span", { class: "hint" }, "Triage"), el("strong", {}, triage)]),
    ]),
    compact ? null : el("div", { class: "stack radar-links" }, [
      el("div", {}, [el("span", { class: "hint" }, "Models "), modelLinks(detail)]),
      el("div", {}, [el("span", { class: "hint" }, "Providers "), providerLinks(detail)]),
      el("div", {}, [el("span", { class: "hint" }, "Concepts "), conceptLinks(detail)]),
    ]),
    el("div", { class: "row" }, [
      el("button", { class: "primary small", onclick: () => navigate(`#/ail/radar/developments/${encodeURIComponent(d.id)}`) }, "Inspect evidence"),
    ]),
  ]);
}

function section(title, items, emptyText) {
  return el("section", { class: "stack today-section" }, [
    el("div", { class: "row between" }, [el("h2", {}, title), el("span", { class: "hint" }, `${items.length} shown`)]),
    items.length ? el("div", { class: "stack" }, items.map((item) => developmentCard(item, { compact: true }))) : stateCard(emptyText),
  ]);
}

export async function renderToday(root) {
  mount(root, el("div", { class: "stack" }, [pageHeader("Today", "A bounded, deterministic view of what changed and why it is shown."), stateCard("Loading Today…", "loading-state")]));
  try {
    const rows = await api.get(`/radar/developments?limit=${LIMIT}`);
    const groups = groupToday(await fetchDetails(rows));
    const content = el("div", { class: "stack" }, [
      pageHeader("Today", "No AI call is required to open this view.", [el("a", { class: "button-link", href: "#/ail/radar" }, "Open Radar")]),
      section("Important or relevant changes", groups.important, "No relevant changes recorded in the current window."),
      section("New or materially changed models", groups.newModels, "No new model changes recorded."),
      section("Pricing and capability changes", groups.changes, "No pricing or capability changes recorded."),
      section("Related to your learning", groups.learning, "No development is linked to your declared interests or active plan."),
      section("Related to opted-in platform usage", groups.platform, "No opted-in usage relationship is available."),
      section("Needs verification", groups.verification, "No development currently needs additional verification."),
      section("Pending review or decisions", groups.pending, "No pending triage decisions."),
    ]);
    mount(root, content);
  } catch (error) {
    mount(root, el("div", { class: "stack" }, [pageHeader("Today", "Deterministic intelligence view."), stateCard(error.message || "Today could not be loaded.", "error-banner")]));
  }
}

function filterControls(state, redraw) {
  const type = el("select", { onchange: (event) => { state.type = event.target.value; redraw(); } }, [
    el("option", { value: "" }, "All development types"),
    ...Object.entries(TYPE_LABELS).map(([value, label]) => el("option", { value, selected: state.type === value }, label)),
  ]);
  const verification = el("select", { onchange: (event) => { state.verification = event.target.value; redraw(); } }, [
    el("option", { value: "" }, "All verification"),
    ...["CLAIMED", "DOCUMENTED", "AVAILABLE", "INDEPENDENTLY_MEASURED", "TESTED_BY_US"].map((value) => el("option", { value, selected: state.verification === value }, value)),
  ]);
  const attention = el("select", { onchange: (event) => { state.attention = event.target.value; redraw(); } }, [
    el("option", { value: "" }, "All attention"),
    ...["UNKNOWN", "LOW", "RISING", "HIGH", "SUSTAINED"].map((value) => el("option", { value, selected: state.attention === value }, value)),
  ]);
  const triage = el("select", { onchange: (event) => { state.triage = event.target.value; redraw(); } }, [
    el("option", { value: "" }, "All triage"),
    ...["IGNORE", "WATCH", "LEARN", "EXPERIMENT", "INVESTIGATE", "UNREVIEWED"].map((value) => el("option", { value, selected: state.triage === value }, value)),
  ]);
  const query = el("input", { type: "text", placeholder: "Search developments", value: state.query, oninput: (event) => { state.query = event.target.value; redraw(); } });
  return el("div", { class: "filter-bar" }, [query, type, verification, attention, triage]);
}

export async function renderRadar(root) {
  const state = { rows: [], details: [], loading: true, error: null, type: "", verification: "", attention: "", triage: "", query: "" };
  const redraw = () => {
    clear(root);
    if (state.loading) { mount(root, el("div", { class: "stack" }, [pageHeader("Radar", "The bounded Development ledger."), stateCard("Loading Radar…", "loading-state")])); return; }
    if (state.error) { mount(root, el("div", { class: "stack" }, [pageHeader("Radar", "The bounded Development ledger."), stateCard(state.error.message || "Radar could not be loaded.", "error-banner")])); return; }
    const filtered = state.details.filter((detail) => {
      const d = detail.development;
      return (!state.type || d.development_type === state.type) &&
        (!state.verification || detail.verification_level === state.verification) &&
        (!state.attention || detail.attention_state === state.attention) &&
        (!state.triage || (state.triage === "UNREVIEWED" ? !detail.current_triage : detail.current_triage?.decision === state.triage)) &&
        (!state.query || `${d.title} ${d.development_type}`.toLowerCase().includes(state.query.toLowerCase()));
    });
    mount(root, el("div", { class: "stack" }, [
      pageHeader("Radar", "Recent Developments with visible evidence, freshness, and user context.", [el("a", { class: "button-link", href: "#/ail/today" }, "Today")]),
      filterControls(state, redraw),
      filtered.length ? el("div", { class: "stack" }, filtered.map((detail) => developmentCard(detail))) : stateCard("No developments match these filters."),
    ]));
  };
  redraw();
  try {
    state.rows = await api.get(`/radar/developments?limit=${LIMIT}`);
    state.details = await fetchDetails(state.rows);
    state.loading = false;
  } catch (error) { state.loading = false; state.error = error; }
  redraw();
}

function claimView(claim) {
  const source = claim.source_item;
  const sourceLink = source?.canonical_url
    ? el("a", { href: source.canonical_url, target: "_blank", rel: "noopener noreferrer" }, source.title || source.canonical_url)
    : el("span", { class: "hint" }, "No source item link");
  return el("article", { class: "claim-card" }, [
    el("div", { class: "row between" }, [badge(claimTypeLabel(claim.claim_type)), el("span", { class: "hint" }, `As of ${formatDateTime(claim.as_of)}`)]),
    el("p", { class: "claim-text" }, claim.text),
    claim.quote_span ? el("blockquote", {}, claim.quote_span) : null,
    el("div", { class: "claim-provenance" }, [el("strong", {}, "Origin: "), el("span", {}, claim.origin?.kind || "Unknown"), el("span", {}, " · "), sourceLink]),
    claim.citations?.length ? el("div", { class: "hint" }, `Cites ${claim.citations.length} supporting claim${claim.citations.length === 1 ? "" : "s"}.`) : null,
  ]);
}

function triagePanel(detail, redraw) {
  const current = detail.current_triage;
  const decision = el("select", { "aria-label": "Choose a triage action" }, [
    el("option", { value: "", selected: true, disabled: true }, "Choose an action..."),
    ...triageActionValues().map((value) => el("option", { value }, value)),
  ]);
  const rationale = el("textarea", { placeholder: "Optional rationale" });
  const revisit = el("input", { type: "datetime-local" });
  const message = el("p", { class: "hint" }, "");
  const submitButton = el("button", { class: "primary" }, "Save decision");
  const submit = async () => {
    if (!decision.value) { message.textContent = "Choose an action before saving."; return; }
    if (decision.value === "WATCH" && !revisit.value) { message.textContent = "WATCH requires a revisit date."; return; }
    submitButton.disabled = true;
    try {
      const saved = await api.post(`/radar/developments/${encodeURIComponent(detail.development.id)}/triage`, {
        decision: decision.value,
        rationale: rationale.value || null,
        reason_codes: [],
        revisit_at: revisit.value ? new Date(revisit.value).toISOString() : null,
      });
      detail.current_triage = saved;
      message.textContent = "Decision saved. The Learning Plan was not changed.";
      redraw();
    } catch (error) { message.textContent = error.message || "Decision could not be saved."; }
    submitButton.disabled = false;
  };
  submitButton.onclick = submit;
  return el("div", { class: "card triage-panel" }, [
    el("h2", {}, "Your decision"),
    current ? el("p", {}, `Current: ${current.decision}${current.rationale ? ` — ${current.rationale}` : ""}`) : el("p", { class: "hint" }, "No triage decision recorded for you."),
    el("div", { class: "triage-form" }, [
      el("div", {}, [el("label", {}, "Action"), decision]),
      el("div", {}, [el("label", {}, "Revisit date (required for WATCH)"), revisit]),
      el("div", {}, [el("label", {}, "Rationale"), rationale]),
      submitButton,
    ]),
    message,
    el("p", { class: "hint" }, "LEARN opens related learning context only; EXPERIMENT and INVESTIGATE do not execute work here."),
  ]);
}

export async function renderDevelopmentDetail(root, params) {
  const id = params.id;
  let detail;
  const redraw = () => mount(root, detail ? renderDetail(detail, redraw) : el("div", { class: "stack" }, [pageHeader("Development", "Evidence inspection surface."), stateCard("Loading development…", "loading-state")]));
  redraw();
  try { detail = await api.get(`/radar/developments/${encodeURIComponent(id)}/intelligence`); redraw(); }
  catch (error) { mount(root, stateCard(error.message || "Development could not be loaded.", "error-banner")); }
}

function renderDetail(detail, redraw) {
  const d = detail.development;
  const learnTarget = (detail.concept_links || []).find((link) => link.state === "confirmed");
  return el("div", { class: "stack" }, [
    pageHeader(d.title, `${developmentTypeLabel(d.development_type)} · ${dateLine(d)}`, [el("a", { class: "button-link", href: "#/ail/radar" }, "Back to Radar")]),
    el("div", { class: "card" }, [
      el("div", { class: "row" }, [badge(detail.verification_level || "Unknown"), badge(`Freshness: ${detail.freshness || "Unknown"}`), badge(`Attention: ${detail.attention_state || "UNKNOWN"}`)]),
      el("h2", {}, "Why this is shown"), reasonChips(detail),
      el("p", { class: "hint" }, `${detail.claims?.length || 0} claim(s) · ${detail.attention_samples?.length || 0} attention sample(s) · ${detail.current_triage ? `triaged ${detail.current_triage.decision}` : "not reviewed"}`),
    ]),
    el("div", { class: "card" }, [el("h2", {}, "Related models"), modelLinks(detail), el("h2", {}, "Related providers"), providerLinks(detail), el("h2", {}, "Related Concepts"), conceptLinks(detail)]),
    el("section", { class: "card" }, [el("h2", {}, "Verification ladder"), verificationLadderWithCurrent(detail.verification_level)]),
    el("section", { class: "card" }, [el("h2", {}, "Claims and provenance"), detail.claims?.length ? el("div", { class: "stack" }, detail.claims.map(claimView)) : stateCard("No claims recorded for this Development.")]),
    el("section", { class: "card" }, [el("h2", {}, "Attention evidence"), detail.attention_samples?.length ? el("div", { class: "stack" }, detail.attention_samples.map((sample) => el("div", { class: "row between" }, [el("span", {}, `${sample.metric}: ${sample.value}${sample.unit ? ` ${sample.unit}` : ""}`), el("span", { class: "hint" }, formatDateTime(sample.sampled_at))]))) : stateCard("No attention evidence recorded.")]),
    triagePanel(detail, redraw),
    el("div", { class: "card next-actions" }, [el("h2", {}, "Next actions"), el("div", { class: "row" }, [
      learnTarget ? badge(`Learn via Concept ${learnTarget.concept_id}`) : badge("No confirmed Concept target"),
      el("button", { class: "small", onclick: () => { if (learnTarget) window.alert(`Open Concept ${learnTarget.concept_id} from the Learning surface.`); } }, learnTarget ? "Open learning context" : "Learning context unavailable"),
    ])]),
  ]);
}

function verificationLadderWithCurrent(level) {
  const levels = ["CLAIMED", "DOCUMENTED", "AVAILABLE", "INDEPENDENTLY_MEASURED", "TESTED_BY_US"];
  return el("div", { class: "verification-ladder", "aria-label": `Current verification: ${level || "unknown"}` }, levels.map((item, position) => {
    const { current, reached } = verificationStepState(level, item);
    return el("div", {
      class: `verification-step${reached ? " reached" : ""}${current ? " current" : ""}`,
      "aria-current": current ? "step" : null,
    }, [el("span", { "aria-hidden": "true" }, reached ? "*" : "-"), current ? `${item} (current)` : item]);
  }));
}

function verificationLadder(level) {
  const levels = ["CLAIMED", "DOCUMENTED", "AVAILABLE", "INDEPENDENTLY_MEASURED", "TESTED_BY_US"];
  const index = levels.indexOf(level);
  return el("div", { class: "verification-ladder" }, levels.map((item, position) => el("div", { class: position <= index ? "verification-step reached" : "verification-step" }, [el("span", {}, position <= index ? "✓" : "○"), item])));
}
