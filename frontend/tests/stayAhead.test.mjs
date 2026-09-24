import { test } from "node:test";
import assert from "node:assert/strict";

// A tiny fake DOM (same approach as labLearning.test.mjs): the block is built
// by dom.js's el(), which only needs these few methods.
class FakeText {
  constructor(text) { this.text = String(text); }
  get textContent() { return this.text; }
}
class FakeNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attrs = {};
    this.listeners = {};
    this.className = "";
    this.disabled = false;
  }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  appendChild(child) { this.children.push(child); return child; }
  get firstChild() { return this.children[0] || null; }
  removeChild(child) { this.children = this.children.filter((c) => c !== child); return child; }
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
}
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeText(text),
};

const {
  STAY_AHEAD_SECTIONS, STAY_AHEAD_INTRO, allEmptyText, cardModel, familyLabel, hrefForLink, reasonLabel,
  sectionCountText, stayAheadModel, usageEyebrow,
} = await import("../assets/js/stayAhead.js");
const { stayAheadBlock, stayAheadUnavailable } = await import("../assets/js/pages/stayAhead.js");
const { renderToday } = await import("../assets/js/pages/radar.js");

function walk(node, out = []) {
  if (node instanceof FakeNode) {
    out.push(node);
    node.children.forEach((child) => walk(child, out));
  }
  return out;
}
const byTag = (node, tag) => walk(node).filter((n) => n.tagName === tag);
const byClass = (node, cls) => walk(node).filter((n) => n.className.split(/\s+/).includes(cls));

const T = "2026-09-20T12:00:00Z";

function reason(overrides = {}) {
  return {
    id: "USED_MODEL_CHANGED:pm-1",
    family: "USED_MODEL_CHANGED",
    title: "vendor/model-a changed since you last used it",
    what_changed: "Recorded changes since then: price (output $2 to $4 per million tokens).",
    why: "You used this model in 1 Personal Lab experiment (last on 2026-08-31), and its catalog record has changed since then.",
    reason_codes: ["MODEL_PRICE_CHANGED", "PERSONAL_USAGE"],
    since: T,
    changed_at: T,
    subject: { kind: "model", id: "m-1", name: "vendor/model-a", usage_scope: "personal" },
    changes: [],
    evidence_refs: [{ type: "provider_model_snapshot", id: "s-1" }],
    links: [
      { kind: "model", id: "m-1", label: "Review change" },
      { kind: "experiment", id: "e-1", label: "Open previous experiment" },
    ],
    concept: null,
    learner_state: null,
    distinct_reason_count: null,
    reasons: [],
    ...overrides,
  };
}

function section(items, total = items.length, shown = items.length) {
  return { total, shown, items };
}

function data(overrides = {}) {
  const empty = section([]);
  return {
    generated_at: T,
    window_days: 30,
    window_start: T,
    sections: {
      worth_revisiting: empty,
      used_models_changed: empty,
      watched_developments: empty,
      experiments_to_rerun: empty,
      concepts_changed: empty,
      ...overrides,
    },
  };
}

test("the block has the agreed sections in fixed order (AIL.4B adds Review second)", () => {
  assert.deepEqual(
    STAY_AHEAD_SECTIONS.map((s) => s.title),
    [
      "Worth revisiting",
      "Review",
      "Models that changed since they were used",
      "Watched developments with updates",
      "Experiments worth rerunning",
      "Concepts with relevant changes",
    ],
  );
  assert.deepEqual(
    STAY_AHEAD_SECTIONS.map((s) => s.key),
    ["worth_revisiting", "review", "used_models_changed", "watched_developments", "experiments_to_rerun", "concepts_changed"],
  );
});

test("links map only to existing pages and unknown kinds get no href", () => {
  assert.equal(hrefForLink({ kind: "model", id: "m 1" }), "#/models/m%201/explore");
  assert.equal(hrefForLink({ kind: "development", id: "d-1" }), "#/ail/radar/developments/d-1");
  assert.equal(hrefForLink({ kind: "experiment", id: "e-1" }), "#/ail/lab/experiments/e-1");
  assert.equal(hrefForLink({ kind: "concept", id: "c-1" }), null);
  assert.equal(hrefForLink({ kind: "model" }), null);
  assert.equal(hrefForLink(null), null);
});

test("reason codes render as plain-language labels with a safe fallback", () => {
  assert.equal(reasonLabel("MODEL_PRICE_CHANGED"), "Price changed");
  assert.equal(reasonLabel("WATCH_REVISIT_DATE_REACHED"), "Your revisit date arrived");
  assert.equal(reasonLabel("SOMETHING_NEW"), "something new");
  assert.equal(familyLabel("EXPERIMENT_MAY_BE_STALE"), "Experiment may be out of date");
});

test("personal and shared project usage are labelled as different facts", () => {
  assert.equal(reasonLabel("PERSONAL_USAGE"), "You used it in Personal Lab");
  assert.equal(reasonLabel("OPTED_IN_PROJECT_USAGE"), "Used in an opted-in project you can access");
  const shared = reason({
    title: "vendor/model-a changed since it was last used in an opted-in project",
    why: "This model was used in an opted-in project you can access (1 call, last on 2026-08-31), and its catalog record has changed since then. This is shared project usage; it does not mean you personally used it.",
    reason_codes: ["MODEL_PRICE_CHANGED", "OPTED_IN_PROJECT_USAGE"],
    subject: { kind: "model", id: "m-1", name: "vendor/model-a", usage_scope: "shared_project" },
    links: [{ kind: "model", id: "m-1", label: "Review change" }],
  });
  assert.equal(usageEyebrow(shared, "x"), "Model used in an opted-in project");
  assert.equal(usageEyebrow(reason(), "x"), "Model you used");
  assert.equal(usageEyebrow(reason({ subject: { usage_scope: "personal_and_shared" } }), "x"), "Model you used");
  assert.equal(usageEyebrow(reason({ subject: {} }), "fallback"), "fallback");
  const card = cardModel(shared, "Model used");
  assert.equal(card.eyebrow, "Model used in an opted-in project");
  assert.deepEqual(card.reasons, ["Price changed", "Used in an opted-in project you can access"]);
  assert.ok(!card.reasons.includes("You used it in Personal Lab"));
  const block = stayAheadBlock(data({ used_models_changed: section([shared]) }));
  assert.match(block.textContent, /Model used in an opted-in project/);
  assert.match(block.textContent, /does not mean you personally used it/);
});

test("count text is honest about what is shown", () => {
  assert.equal(sectionCountText({ total: 0, shown: 0 }), "");
  assert.equal(sectionCountText({ total: 2, shown: 2 }), "2 shown");
  assert.equal(sectionCountText({ total: 5, shown: 3 }), "3 of 5 shown");
});

test("a card answers what changed, why, and what to open, from recorded text only", () => {
  const card = cardModel(reason(), "Model you used");
  assert.equal(card.what, reason().what_changed);
  assert.equal(card.why, reason().why);
  assert.deepEqual(card.reasons, ["Price changed", "You used it in Personal Lab"]);
  assert.deepEqual(card.links.map((l) => l.label), ["Review change", "Open previous experiment"]);
  assert.equal(card.learnerState, null);
  assert.deepEqual(card.absorbed, []);
});

test("a Worth revisiting card keeps every absorbed reason with its own codes and links", () => {
  const stale = reason({ id: "EXPERIMENT_MAY_BE_STALE:e-1", family: "EXPERIMENT_MAY_BE_STALE", reason_codes: ["MODEL_PRICE_CHANGED"] });
  const version = reason({
    id: "CONCEPT_CHANGED:c-1", family: "CONCEPT_CHANGED", reason_codes: ["CONCEPT_NEW_MATERIAL_VERSION"],
    links: [{ kind: "development", id: "d-1", label: "Inspect evidence" }],
  });
  const card = cardModel(
    reason({
      id: "WORTH_REVISITING:c-1", family: "WORTH_REVISITING", title: "Worth revisiting: Model Routing",
      learner_state: { ladder: "demonstrated", overlays: [] }, reasons: [stale, version], distinct_reason_count: 2,
    }),
    "Worth revisiting",
  );
  assert.equal(card.learnerState.label, "Demonstrated");
  assert.deepEqual(card.absorbed.map((r) => r.familyLabel), ["Experiment may be out of date", "Concept changed"]);
  assert.deepEqual(card.absorbed[1].reasons, ["New material version"]);
  assert.equal(card.absorbed[1].links[0].href, "#/ail/radar/developments/d-1");
});

test("an all-empty response shows one plain line, not five empty headings", () => {
  const model = stayAheadModel(data());
  assert.equal(model.allEmpty, true);
  const block = stayAheadBlock(data());
  assert.equal(byTag(block, "h2").length, 1);
  assert.match(block.textContent, new RegExp(allEmptyText(30)));
  assert.equal(byTag(block, "article").length, 0);
});

test("non-empty responses render each section, cards, and one-line empty states without backfilling", () => {
  const items = [reason(), reason({ id: "USED_MODEL_CHANGED:pm-2", title: "vendor/model-b changed since you last used it" })];
  const block = stayAheadBlock(data({ used_models_changed: section(items, 4, 2) }));
  const headings = byTag(block, "h2").map((n) => n.textContent);
  assert.deepEqual(headings, ["Stay ahead", ...STAY_AHEAD_SECTIONS.map((s) => s.title)]);
  assert.equal(byTag(block, "article").length, 2);
  assert.match(block.textContent, /2 of 4 shown/);
  for (const config of STAY_AHEAD_SECTIONS.filter((s) => s.key !== "used_models_changed")) {
    assert.ok(block.textContent.includes(config.empty), config.key);
  }
  const card = byTag(block, "article")[0];
  assert.match(card.textContent, /What changed: /);
  assert.match(card.textContent, /Why you are seeing this: /);
});

test("cards contain links only: no buttons and no handlers that could act", () => {
  const block = stayAheadBlock(data({
    worth_revisiting: section([reason({
      id: "WORTH_REVISITING:c-1", family: "WORTH_REVISITING", learner_state: { ladder: "practiced", overlays: [] },
      reasons: [reason()],
    })]),
    used_models_changed: section([reason()]),
  }));
  assert.equal(byTag(block, "button").length, 0);
  const anchors = byTag(block, "a");
  assert.ok(anchors.length >= 4);
  assert.ok(anchors.every((a) => a.attrs.href.startsWith("#/")));
  assert.ok(walk(block).every((n) => Object.keys(n.listeners).length === 0));
});

test("no user-facing string scores, ranks or crowns anything", () => {
  const strings = [
    STAY_AHEAD_INTRO,
    allEmptyText(30),
    ...STAY_AHEAD_SECTIONS.flatMap((s) => [s.title, s.intro, s.empty, s.eyebrow]),
    ...["MODEL_PRICE_CHANGED", "HAS_LEARNING_EVIDENCE", "WATCH_NEW_EVIDENCE", "CONCEPT_NEW_MATERIAL_VERSION"].map(reasonLabel),
  ].join(" ").toLowerCase();
  for (const word of ["score", "rank", "priority", "importance", "winner", "best", "top pick", "#1"]) {
    assert.ok(!strings.includes(word), word);
  }
});

test("a failure to load is contained and says the rest of Today is unaffected", () => {
  const block = stayAheadUnavailable(new Error("boom"));
  assert.match(block.textContent, /could not be loaded: boom/);
  assert.match(block.textContent, /rest of Today is unaffected/);
});

// -- Today integration: existing Radar sections stay intact -------------------

function stubFetch(routes) {
  const calls = [];
  globalThis.fetch = async (path) => {
    calls.push(path);
    const handler = Object.entries(routes).find(([prefix]) => path.startsWith(prefix));
    const result = handler ? handler[1] : { status: 404, body: { error: { message: "not found" } } };
    return {
      ok: result.status === 200,
      statusText: "x",
      text: async () => JSON.stringify(result.body),
    };
  };
  return calls;
}

const DEV = { id: "d-1", title: "Neutral development", development_type: "capability", first_seen_at: T };
const INTEL = {
  development: DEV, reason_codes: ["RELATED_TO_INTEREST"], current_triage: null, verification_level: "DOCUMENTED",
  claims: [], model_links: [], provider_links: [], concept_links: [],
};

async function todayText(routes) {
  const root = new FakeNode("div");
  stubFetch(routes);
  await renderToday(root);
  return root;
}

test("Today shows Stay ahead first and keeps every existing Radar section", async () => {
  const root = await todayText({
    "/stay-ahead/today": { status: 200, body: data({ used_models_changed: section([reason()]) }) },
    "/radar/developments/d-1/intelligence": { status: 200, body: INTEL },
    "/radar/developments": { status: 200, body: [DEV] },
  });
  const headings = byTag(root, "h2").map((n) => n.textContent);
  assert.equal(headings[0], "Stay ahead");
  for (const existing of [
    "Important or relevant changes", "New or materially changed models", "Pricing and capability changes",
    "Related to your learning", "Related to opted-in platform usage", "Needs verification", "Pending review or decisions",
  ]) {
    assert.ok(headings.includes(existing), existing);
  }
  assert.ok(root.textContent.includes("Neutral development"));
});

test("Today still renders Radar when Stay ahead fails", async () => {
  const root = await todayText({
    "/stay-ahead/today": { status: 500, body: { error: { message: "backend down" } } },
    "/radar/developments/d-1/intelligence": { status: 200, body: INTEL },
    "/radar/developments": { status: 200, body: [DEV] },
  });
  assert.match(root.textContent, /Stay ahead could not be loaded: backend down/);
  assert.ok(byTag(root, "h2").map((n) => n.textContent).includes("Important or relevant changes"));
  assert.ok(root.textContent.includes("Neutral development"));
});

test("Today reads Stay ahead with a single GET and never posts", async () => {
  const root = new FakeNode("div");
  const calls = stubFetch({
    "/stay-ahead/today": { status: 200, body: data() },
    "/radar/developments": { status: 200, body: [] },
  });
  const methods = [];
  const inner = globalThis.fetch;
  globalThis.fetch = async (path, options) => { methods.push(options?.method); return inner(path, options); };
  await renderToday(root);
  assert.equal(calls.filter((c) => c === "/stay-ahead/today").length, 1);
  assert.ok(methods.every((m) => m === "GET"));
});
