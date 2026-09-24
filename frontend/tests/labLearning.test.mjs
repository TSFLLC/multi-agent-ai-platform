import { test } from "node:test";
import assert from "node:assert/strict";

// A tiny fake DOM: this project adds no jsdom/browser-test dependency, but the
// results page is built by dom.js's el(), which only needs these few methods.
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
    this.value = "";
  }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  appendChild(child) { this.children.push(child); return child; }
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this.children = [new FakeText(value)]; }
  async click() { for (const fn of this.listeners.click || []) await fn({ currentTarget: this }); }
}
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeText(text),
};

const {
  CONCLUSION_OPTIONS, RESULT_SECTIONS, candidateName, canConclude, conceptLabel, conclusionSummary, countedMessage,
  evaluationView, findingsView, ladderLabel, nextSteps, qualificationView, validateConclusionDraft,
} = await import("../assets/js/labLearning.js");
const { buildResultSections } = await import("../assets/js/pages/lab.js");

function walk(node, out = []) {
  if (node instanceof FakeNode) {
    out.push(node);
    node.children.forEach((child) => walk(child, out));
  }
  return out;
}
const buttons = (node) => walk(node).filter((n) => n.tagName === "button");
const section = (sections, id) => sections.find((s) => s.attrs["data-section"] === id);

function state(overrides = {}) {
  const { experiment = {}, ...rest } = overrides;
  return {
    data: {
      experiment: {
        experiment_type: "model_comparison", hypothesis: "Which model is tidier?",
        models: [{ model_id: "m1", name: "lab/alpha" }, { model_id: "m2", name: "lab/beta" }],
        agent_version_ids: ["a"], repetitions: 1, overall_status: "COMPLETED", evaluation_required: true,
        evaluation_status: "COMPLETED", concept_id: "c1", concept: { id: "c1", name: "Context windows", slug: "context-windows" },
        conclusion: null, development_id: null, ...experiment,
      },
      evaluation_findings: {
        evaluation_required: true,
        candidates: [
          { label: "model-1", runs: 6, runs_evaluated: 6, met: 6, partial: 0, not_met: 0, not_applicable: 0, total: 6 },
          { label: "model-2", runs: 6, runs_evaluated: 6, met: 0, partial: 1, not_met: 5, not_applicable: 0, total: 6 },
        ],
      },
      progress: { total: 4, completed: 4, failed: 0, cancelled: 0, tokens_in: 10, tokens_out: 20, cost_kind: "KNOWN", cost: "0.01" },
      runs: [
        { task_position: 0, repetition: 1, label: "model-1", status: "completed", tokens_in: 5, tokens_out: 5 },
        { task_position: 0, repetition: 1, label: "model-2", status: "failed", tokens_in: 5, tokens_out: 15 },
      ],
    },
    qualification: { status: "READY", ready: true, message: "Ready to count toward learning." },
    conceptResults: [],
    development: null,
    ...rest,
  };
}

test("results follow the fixed hierarchy and never include a Professor", () => {
  const sections = buildResultSections(state());
  assert.deepEqual(sections.map((s) => s.attrs["data-section"]), RESULT_SECTIONS);
  assert.deepEqual(RESULT_SECTIONS, ["tested", "happened", "evaluation", "differed", "conclusion", "count", "next"]);
  assert.doesNotMatch(sections.map((s) => s.textContent).join(" "), /professor/i);
});

test("a saved conclusion is rendered, and every conclusion type is presented as winner-free", () => {
  for (const option of CONCLUSION_OPTIONS) {
    const sections = buildResultSections(state({ experiment: { conclusion: { type: option.value, text: "My reading", concluded_at: "x" } } }));
    const text = section(sections, "conclusion").textContent;
    assert.match(text, new RegExp(option.label));
    assert.match(text, /My reading/);
    assert.match(text, /No winner was chosen/);
  }
  assert.equal(conclusionSummary(null), null);
});

test("the conclusion editor only appears once there are results to read", () => {
  for (const status of ["RUNNING", "EVALUATING", "CANCELLED"]) {
    assert.equal(canConclude({ overall_status: status }), false);
    const conclusion = section(buildResultSections(state({ experiment: { overall_status: status } })), "conclusion");
    assert.equal(buttons(conclusion).length, 0);
  }
  for (const status of ["COMPLETED", "PARTIAL", "FAILED", "EVALUATION_FAILED"]) assert.equal(canConclude({ overall_status: status }), true);
});

test("saving sends the chosen type and text; an empty custom conclusion is stopped before the API", async () => {
  const saved = [];
  const sections = buildResultSections(state(), { saveConclusion: async (draft) => saved.push(draft) });
  const conclusion = section(sections, "conclusion");
  const [select, textarea] = walk(conclusion).filter((n) => n.tagName === "select" || n.tagName === "textarea");
  const [save] = buttons(conclusion);

  select.value = "custom";
  textarea.value = "   ";
  await save.click();
  assert.equal(saved.length, 0);
  assert.match(conclusion.textContent, /own words/);

  select.value = "tradeoff";
  textarea.value = "Cheaper vs tidier";
  await save.click();
  assert.deepEqual(saved, [{ type: "tradeoff", text: "Cheaper vs tidier" }]);
});

test("Count Toward Learning is explicit: offered only when READY and never called on render", async () => {
  let calls = 0;
  const sections = buildResultSections(state(), { countToward: async () => { calls += 1; } });
  assert.equal(calls, 0);
  const count = buttons(section(sections, "count")).filter((b) => b.textContent === "Count toward learning");
  assert.equal(count.length, 1);
  await count[0].click();
  assert.equal(calls, 1);
});

test("every non-ready state explains why and offers no Count button", () => {
  const cases = {
    MISSING_CONCEPT: /Choose a Concept first/,
    CONCEPT_REBIND_REQUIRED: /Confirm your Concept/,
    EXPERIMENT_INCOMPLETE: /has not finished/,
    EVALUATION_NOT_CONFIGURED: /was not evaluated/,
    EVALUATION_PENDING: /still pending/,
    EVALUATION_FAILED: /Evaluation failed/,
    EVALUATION_INCOMPLETE: /incomplete/,
    NO_MEANINGFUL_EVALUATION: /Nothing to learn from/,
  };
  for (const [status, pattern] of Object.entries(cases)) {
    const sections = buildResultSections(state({ qualification: { status, ready: false, message: "" } }));
    const count = section(sections, "count");
    assert.match(count.textContent, pattern, status);
    assert.equal(buttons(count).filter((b) => b.textContent === "Count toward learning").length, 0, status);
  }
});

test("READY explains that candidate scores do not decide whether it can count", () => {
  const view = qualificationView({ status: "READY", ready: true });
  assert.equal(view.canCount, true);
  assert.match(view.hint, /does not matter which candidate scored higher/);
});

test("an already-counted experiment shows its state, locks the Concept and offers no actions", () => {
  const sections = buildResultSections(state({ qualification: { status: "ALREADY_COUNTED", ready: false } }));
  const count = section(sections, "count");
  assert.match(count.textContent, /Already counted toward learning/);
  assert.match(count.textContent, /locked/);
  assert.equal(buttons(count).length, 0);
  assert.equal(qualificationView({ status: "ALREADY_COUNTED" }).counted, true);
});

test("Concept search and selection use the handlers with the typed query and chosen id", async () => {
  const calls = [];
  const handlers = { searchConcepts: async (q) => calls.push(["search", q]), bindConcept: async (id) => calls.push(["bind", id]) };
  let count = section(buildResultSections(state({ experiment: { concept_id: null }, qualification: { status: "MISSING_CONCEPT", ready: false } }), handlers), "count");
  assert.match(count.textContent, /No Concept chosen yet/);
  walk(count).find((n) => n.tagName === "input").value = "tokens";
  await buttons(count).find((b) => b.textContent === "Search").click();

  count = section(buildResultSections(state({ conceptResults: [{ id: "c9", name: "Context windows" }], qualification: { status: "MISSING_CONCEPT", ready: false } }), handlers), "count");
  assert.match(count.textContent, /Context windows/);
  await buttons(count).find((b) => b.textContent === "Use this Concept").click();
  assert.deepEqual(calls, [["search", "tokens"], ["bind", "c9"]]);
});

test("Radar origin is a read-only link, shown only when the experiment came from Radar", () => {
  const plain = section(buildResultSections(state()), "tested");
  assert.equal(walk(plain).some((n) => n.attrs["data-origin"] === "radar"), false);

  const fromRadar = section(buildResultSections(state({ experiment: { development_id: "dev 1" }, development: { title: "New model X" } })), "tested");
  const origin = walk(fromRadar).find((n) => n.attrs["data-origin"] === "radar");
  assert.match(origin.textContent, /New model X/);
  assert.equal(walk(origin).find((n) => n.tagName === "a").attrs.href, "#/ail/radar/developments/dev%201");
  assert.equal(buttons(fromRadar).length, 0);
  assert.ok(nextSteps({ development_id: "d" }, null).some((s) => s.href === "#/ail/radar/developments/d"));
});

test("evaluation shows status only: no criteria counts or denominators without canonical evidence", () => {
  for (const evaluation_status of ["PENDING", "RUNNING", "FAILED", "NOT_REQUIRED", "COMPLETED"]) {
    const view = evaluationView({ evaluation_required: evaluation_status !== "NOT_REQUIRED", evaluation_status });
    assert.doesNotMatch(`${view.headline} ${view.detail}`, /\d/, evaluation_status);
    const text = section(buildResultSections(state({ experiment: { evaluation_status } })), "evaluation").textContent;
    assert.doesNotMatch(text, /\d+ of \d+|\d+\s*\/\s*\d+|%/, evaluation_status);
  }
  assert.equal(evaluationView({ evaluation_required: false }).status, "NOT_REQUIRED");
});

test("no saved conclusion: the selector starts on Choose… and the page says nothing is saved", async () => {
  const saved = [];
  const conclusion = section(buildResultSections(state(), { saveConclusion: async (draft) => saved.push(draft) }), "conclusion");
  const select = walk(conclusion).find((n) => n.tagName === "select");
  const options = walk(select).filter((n) => n.tagName === "option");
  assert.equal(options[0].textContent, "Choose…");
  assert.equal(options[0].attrs.value, "");
  assert.equal(select.value, "");
  assert.equal(options.length, CONCLUSION_OPTIONS.length + 1);
  assert.match(conclusion.textContent, /No conclusion saved yet\./);
  assert.doesNotMatch(conclusion.textContent, /No winner was chosen/);

  await buttons(conclusion)[0].click(); // saving without choosing is stopped
  assert.equal(saved.length, 0);
  assert.match(conclusion.textContent, /Choose how you would describe the result/);
});

test("a persisted conclusion is shown, preselected for editing, and hides the empty-state line", () => {
  const conclusion = section(buildResultSections(state({ experiment: { conclusion: { type: "tradeoff", text: "Cheaper vs tidier", concluded_at: "x" } } })), "conclusion");
  assert.match(conclusion.textContent, /It's a tradeoff/);
  assert.match(conclusion.textContent, /Cheaper vs tidier/);
  assert.doesNotMatch(conclusion.textContent, /No conclusion saved yet/);
  assert.equal(walk(conclusion).find((n) => n.tagName === "select").value, "tradeoff");
  assert.equal(buttons(conclusion)[0].textContent, "Update my conclusion");
});

test("the conclusion stays editable after counting and the page explains it is separate from evidence", () => {
  const counted = section(buildResultSections(state({ qualification: { status: "ALREADY_COUNTED", ready: false } })), "conclusion");
  assert.equal(buttons(counted).length, 1);
  assert.match(counted.textContent, /separate from your learning evidence/);
  assert.match(counted.textContent, /even after counting this experiment/);
});

test("the bound Concept name comes from the experiment itself, with no search needed", () => {
  const count = section(buildResultSections(state({ conceptResults: [] })), "count");
  assert.match(count.textContent, /Concept: Context windows/);
  assert.doesNotMatch(count.textContent, /Selected Concept/);
  assert.equal(conceptLabel({ concept: { name: "X" } }), "X");
  assert.equal(conceptLabel({ concept_id: "c" }), "A Concept (name unavailable)");
  assert.equal(conceptLabel({}), "");
});

test("real model names replace model-1 / model-2 in the beginner-facing summary", () => {
  const experiment = state().data.experiment;
  assert.equal(candidateName("model-1", experiment), "lab/alpha");
  assert.equal(candidateName("model-2", experiment), "lab/beta");
  assert.equal(candidateName("model-3", experiment), "Model 3");
  assert.equal(candidateName("agent-2", experiment), "Agent version 2");
  assert.equal(candidateName("repetition-4", experiment), "Repetition 4");

  const sections = buildResultSections(state());
  assert.match(section(sections, "tested").textContent, /Models: lab\/alpha, lab\/beta/);
  const differed = section(sections, "differed").textContent;
  assert.match(differed, /lab\/alpha/);
  assert.match(differed, /lab\/beta/);
  assert.doesNotMatch(differed, /model-\d/);
});

test("run details are collapsed behind a native expander while the aggregate stays visible", () => {
  const happened = section(buildResultSections(state()), "happened");
  const details = walk(happened).filter((n) => n.tagName === "details");
  assert.equal(details.length, 1);
  assert.equal(details[0].attrs.open, undefined); // collapsed by default
  const summary = walk(details[0]).find((n) => n.tagName === "summary");
  assert.match(summary.textContent, /^View run details/);
  const rowsInside = walk(details[0]).filter((n) => n.tagName === "li");
  assert.equal(rowsInside.length, 2);
  assert.match(details[0].textContent, /lab\/alpha: completed/);

  // Everything outside the expander: aggregates, no individual run rows.
  const outside = walk(happened).filter((n) => !walk(details[0]).includes(n));
  assert.equal(outside.some((n) => n.tagName === "li"), false);
  const text = outside.filter((n) => n.tagName === "p").map((n) => n.textContent).join(" ");
  assert.match(text, /4 completed, 0 failed, 0 cancelled of 4 runs/);
  assert.match(text, /Tokens: 10 input \/ 20 output · Cost: KNOWN \(0\.01\)/);
});

test("where they differed shows canonical finding counts with explicit denominators", () => {
  const differed = section(buildResultSections(state()), "differed");
  const rows = walk(differed).filter((n) => n.attrs["data-candidate"]);
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /lab\/alpha \(6 of 6 runs evaluated\): Met 6 of 6 · Partly met 0 of 6 · Not met 0 of 6 · Not applicable 0 of 6/);
  assert.match(rows[1].textContent, /lab\/beta \(6 of 6 runs evaluated\): Met 0 of 6 · Partly met 1 of 6 · Not met 5 of 6 · Not applicable 0 of 6/);
  assert.match(differed.textContent, /not a ranking, and no winner is chosen/);
});

test("differing denominators are shown as given, not normalised", () => {
  const data = state().data;
  data.evaluation_findings.candidates = [
    { label: "model-1", runs: 6, runs_evaluated: 2, met: 2, partial: 0, not_met: 0, not_applicable: 0, total: 2 },
    { label: "model-2", runs: 6, runs_evaluated: 1, met: 0, partial: 0, not_met: 1, not_applicable: 0, total: 1 },
  ];
  const view = findingsView(data);
  assert.deepEqual(view.candidates.map((c) => c.evaluated), ["2 of 6 runs evaluated", "1 of 6 runs evaluated"]);
  assert.deepEqual(view.candidates[0].cells.map((c) => [c.key, c.count, c.of]), [["met", 2, 2], ["partial", 0, 2], ["not_met", 0, 2], ["not_applicable", 0, 2]]);
});

test("findings never score, rank, or name a best model", () => {
  const differed = section(buildResultSections(state()), "differed");
  const withoutDisclaimer = walk(differed).filter((n) => n.tagName === "li").map((n) => n.textContent).join(" ");
  assert.doesNotMatch(withoutDisclaimer, /\b(best|better|worse|wins?|won|winner|rank(ed|ing)?|score|%|\d+\.\d+)\b/i);
  const view = findingsView(state().data);
  assert.deepEqual(Object.keys(view).sort(), ["available", "candidates", "message", "note"]);
  assert.deepEqual(Object.keys(view.candidates[0]).sort(), ["cells", "evaluated", "hasFindings", "label", "name"]);
});

test("findings are unavailable, not invented, when there was no evaluation or it has not completed", () => {
  const notEvaluated = state().data;
  notEvaluated.evaluation_findings = { evaluation_required: false, candidates: [{ label: "model-1", runs: 6, runs_evaluated: 0, met: 0, partial: 0, not_met: 0, not_applicable: 0, total: 0 }] };
  assert.equal(findingsView(notEvaluated).available, false);
  assert.match(findingsView(notEvaluated).message, /not evaluated/);

  const pending = state().data;
  pending.evaluation_findings.candidates.forEach((c) => Object.assign(c, { runs_evaluated: 0, met: 0, partial: 0, not_met: 0, total: 0 }));
  const view = findingsView(pending);
  assert.equal(view.available, false);
  const differed = section(buildResultSections({ ...state(), data: pending }), "differed");
  assert.match(differed.textContent, /Findings appear once the evaluation has completed/);
  assert.equal(walk(differed).some((n) => n.attrs["data-candidate"]), false);
});

test("counting reports the derived level without claiming the experiment set it", () => {
  const text = countedMessage({ learner_state: { ladder: "practiced" } });
  assert.match(text, /"Practiced"/);
  assert.match(text, /worked out from all of your evidence/);
  assert.equal(ladderLabel("not_started"), "Not started");
});

test("client-side conclusion validation mirrors the backend rules", () => {
  assert.match(validateConclusionDraft({ type: "winner", text: "" }), /Choose/);
  assert.match(validateConclusionDraft({ type: "custom", text: "  " }), /own words/);
  assert.match(validateConclusionDraft({ type: "tradeoff", text: "x".repeat(4001) }), /4000/);
  assert.equal(validateConclusionDraft({ type: "inconclusive", text: "" }), null);
});
