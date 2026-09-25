import { test } from "node:test";
import assert from "node:assert/strict";

// A small fake DOM plus a scripted fake fetch: the real page renderers run against them.
class FakeText { constructor(t) { this.text = String(t); } get textContent() { return this.text; } }
class FakeNode {
  constructor(tagName) {
    this.tagName = tagName; this.children = []; this.attrs = {}; this.listeners = {};
    this.className = ""; this.disabled = false; this.checked = false; this.value = "";
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  appendChild(child) { this.children.push(child); return child; }
  get firstChild() { return this.children[0] || null; }
  removeChild(child) { this.children = this.children.filter((c) => c !== child); return child; }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = [new FakeText(v)]; }
}
globalThis.document = { createElement: (t) => new FakeNode(t), createTextNode: (t) => new FakeText(t), body: new FakeNode("body") };
globalThis.window = { location: { hash: "" }, __MAP_LOCAL_TOKEN__: "test-token" };

const pendingTimers = [];
globalThis.setTimeout = (fn) => { pendingTimers.push(fn); return pendingTimers.length; };
globalThis.clearTimeout = () => {};

const calls = [];
let routes = {};
globalThis.fetch = async (path, opts = {}) => {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : undefined;
  calls.push({ method, path, body, headers: Object.fromEntries(opts.headers ? opts.headers.entries() : []) });
  const key = `${method} ${path.split("?")[0]}`;
  const handler = routes[key];
  if (!handler) return { ok: false, status: 404, statusText: "Not Found", text: async () => JSON.stringify({ error: { message: `no route ${key}` } }) };
  const out = typeof handler === "function" ? handler(body, path) : handler;
  if (out && out.__status) return { ok: false, status: out.__status, statusText: "err", text: async () => JSON.stringify({ error: { message: out.message } }) };
  return { ok: true, status: 200, statusText: "OK", text: async () => (typeof out === "string" ? out : JSON.stringify(out)) };
};

const {
  renderAssessmentCenter, renderAssessmentDefinition, renderAssessmentAttempt, renderDemonstrationRecord, renderReviewQueue, renderReviewDetail,
} = await import("../assets/js/pages/assessments.js");
const { isTruthfulText } = await import("../assets/js/assessments.js");

const walk = (n, out = []) => { if (n instanceof FakeNode) { out.push(n); n.children.forEach((c) => walk(c, out)); } return out; };
const byTag = (n, tag) => walk(n).filter((x) => x.tagName === tag);
const byClass = (n, cls) => walk(n).filter((x) => x.className.split(/\s+/).includes(cls));
const links = (n) => byTag(n, "a").map((a) => a.attrs.href);
const buttons = (n, text) => byTag(n, "button").filter((b) => b.textContent.includes(text));
async function click(node) { for (const fn of node.listeners.click || []) await fn({ currentTarget: node }); }
async function fire(node, type) { for (const fn of node.listeners[type] || []) await fn({ currentTarget: node }); }
const flushTimers = async () => { while (pendingTimers.length) await pendingTimers.shift()(); };
const settle = () => new Promise((r) => setImmediate(r));
const root = () => new FakeNode("div");
const reset = (r) => { routes = r; calls.length = 0; pendingTimers.length = 0; window.location.hash = ""; };

const DEF = {
  definition_key: "kc-x", version: 1, kind: "knowledge_check", title: "Check: Structured Output", instructions_md: "Answer the questions.",
  allowed_resources: ["Your lessons"], time_limit_hours: 24, retake_cooldown_hours: 12,
  evidence_collected: ["your responses", "the help level (H0-H5) recorded on your earlier project work"],
  criteria: [{ key: "answers_correct", label: "You answered correctly", decided_by: "the platform", required: true, description: "", anchors: {} }],
  response_fields: [{ path: "fields.explanation", label: "Your explanation", input: "long_text" }],
};
const ATTEMPT = (over = {}) => ({
  id: "att-1", status: "draft", origin: "learner_started", fresh_required: true, fresh_reason: "policy_always", definition: DEF, project_attempt_id: null,
  expires_at: "2099-01-01T00:00:00Z", draft: {}, mentor_locked: true,
  challenge: { entry_kind: "choice", items: [
    { entry_key: "q1", prompt: "What does a schema change?", options: ["Shape", "Truth"], multiple: false },
    { entry_key: "p1", prompt_md: "Explain it.", fixed: true },
  ] },
  ...over,
});

// -- A. Assessment Center -------------------------------------------------------------------------------------------------------

test("Assessment Center shows the empty state, then each section with the right actions", async () => {
  reset({ "GET /academy/assessments/center": { ready_for_assessment: [], in_progress: [], needs_work: [], provisional_or_review: [], demonstrated: [], not_yet_ready: [], history: [] } });
  let r = root();
  await renderAssessmentCenter(r);
  assert.match(r.textContent, /Assessment Center/);
  assert.match(r.textContent, /Nothing is ready yet\. Finish a project or lesson to unlock an assessment\./);

  reset({ "GET /academy/assessments/center": {
    ready_for_assessment: [{ definition_key: "proj/x", title: "Assess your project", kind: "project", why_offered: "Your project work is submitted.", fresh_required: true, project_attempt_id: "pa-1", available_after_ma9: false }],
    in_progress: [{ attempt_id: "a1", title: "Explain", kind: "explain_back", definition_version: 1, status: "draft", outcome: null }],
    needs_work: [{ attempt_id: "a2", title: "Check", kind: "knowledge_check", definition_version: 1, status: "finalized", outcome: "needs_work", finalized_at: "2026-09-25T00:00:00", gaps: [{ label: "Answers", finding: "not_met", detail: "" }] }],
    provisional_or_review: [{ attempt_id: "a3", title: "Explain", kind: "explain_back", definition_version: 1, status: "finalized", outcome: "human_review_required" }],
    demonstrated: [{ result_id: "res 1", title: "Check", concepts: ["Structured Output"], status: "changed_since", status_reasons: ["X changed"], reassess: { definition_key: "kc-x" } }],
    not_yet_ready: [{ definition_key: "cap", title: "Capstone", kind: "capstone", why_offered: "Not ready yet: finish milestones", available_after_ma9: true }],
    history: [{ attempt_id: "a2", title: "Check", kind: "knowledge_check", definition_version: 1, status: "finalized", outcome: "needs_work" }],
  } });
  r = root();
  await renderAssessmentCenter(r);
  const text = r.textContent;
  for (const heading of ["Ready for assessment", "In progress", "Needs more work", "Provisional or waiting for review", "Demonstration records"]) assert.match(text, new RegExp(heading));
  assert.match(text, /Not ready yet \(1\)/);
  assert.match(text, /All attempts \(1\)/);
  assert.match(text, /A fresh challenge will be part of this assessment/);
  assert.match(text, /Available after MA9/);
  assert.match(text, /Answers: Not met|Answers: not_met|Answers/);
  const hrefs = links(r);
  assert.ok(hrefs.includes("#/academy/assessments/definitions/proj%2Fx?project_attempt_id=pa-1"));
  assert.ok(hrefs.includes("#/academy/assessments/attempts/a1"));
  assert.ok(hrefs.includes("#/academy/assessments/records/res%201"));
  assert.ok(hrefs.includes("#/academy/assessments/definitions/kc-x"), "a changed record offers reassessment");
  assert.match(text, /A person needs to review this|Needs more work/);
});

test("Assessment Center reports API failures instead of a blank page", async () => {
  reset({});
  const r = root();
  await renderAssessmentCenter(r);
  assert.equal(byClass(r, "error-banner").length, 1);
});

// -- B/C. Ready + instructions ------------------------------------------------------------------------------------------------------

const READY = (over = {}) => ({
  ready: true, definition: DEF, fresh_required: true, fresh_reason: "source_work_substantially_assisted", source_work: { levels: ["h5"] }, project_attempt_id: "pa-1",
  checks: [{ key: "project_submission", label: "Your project is submitted", met: true, detail: "" }],
  ...over,
});

test("Instructions show what is assessed, allowed resources, the Mentor lock, time and evidence — and the fresh-challenge reason", async () => {
  reset({ "GET /academy/assessments/definitions/kc-x/readiness": READY() });
  const r = root();
  window.location.hash = "#/academy/assessments/definitions/kc-x?project_attempt_id=pa-1";
  await renderAssessmentDefinition(r, { key: "kc-x" });
  const text = r.textContent;
  assert.match(text, /You are ready/);
  assert.match(text, /Your project work: after seeing the solution \(H5\)/);
  assert.match(text, /source project alone cannot show demonstration/);
  assert.match(text, /You answered correctly — decided by the platform/);
  assert.match(text, /Allowed: Your lessons\. Not allowed: AI assistants\./);
  assert.match(text, /Mentor and Professor are paused/);
  assert.match(text, /about 24 hours/);
  assert.match(text, /after 12 hours/);
  assert.match(text, /the help level \(H0-H5\) recorded/);
  assert.match(text, /No camera, screen, keyboard or clipboard monitoring\. No copy detection\./);
  assert.ok(calls[0].path.includes("project_attempt_id=pa-1"));
});

test("Begin is disabled until ready, and starting navigates to the workspace with an idempotency key", async () => {
  reset({ "GET /academy/assessments/definitions/kc-x/readiness": READY({ ready: false, checks: [{ key: "cooldown", label: "You have waited long enough", met: false, detail: "Try again later." }] }) });
  let r = root();
  await renderAssessmentDefinition(r, { key: "kc-x" });
  assert.match(r.textContent, /Not ready yet/);
  assert.match(r.textContent, /✗ You have waited long enough/);
  assert.equal(buttons(r, "Begin assessment")[0].disabled, true);

  reset({
    "GET /academy/assessments/definitions/kc-x/readiness": READY(),
    "POST /academy/assessments/attempts": (body) => ({ id: "att 9", status: "draft" }),
  });
  r = root();
  await renderAssessmentDefinition(r, { key: "kc-x" });
  const begin = buttons(r, "Begin assessment")[0];
  assert.equal(begin.disabled, false);
  await click(begin);
  const post = calls.find((c) => c.method === "POST");
  assert.equal(post.body.definition_key, "kc-x");
  assert.match(post.headers["idempotency-key"], /^start-kc-x-/);
  assert.equal(window.location.hash, "#/academy/assessments/attempts/att%209");
});

test("a learner with an attempt in progress is offered Resume, not a second start", async () => {
  reset({ "GET /academy/assessments/definitions/kc-x/readiness": READY({ ready: false, checks: [{ key: "no_active_attempt", label: "None in progress", met: false, resume_attempt_id: "att-7" }] }) });
  const r = root();
  await renderAssessmentDefinition(r, { key: "kc-x" });
  assert.ok(links(r).includes("#/academy/assessments/attempts/att-7"));
  assert.equal(buttons(r, "Begin assessment").length, 0);
});

// -- D. Workspace ---------------------------------------------------------------------------------------------------------------------------

test("Workspace shows the Assessment Mode banner, the fresh challenge, the explain-back set and inputs", async () => {
  reset({ "GET /academy/assessments/attempts/att-1/result": { status: "draft", attempt: ATTEMPT() } });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  const banner = byClass(r, "assessment-banner")[0];
  assert.match(banner.textContent, /ASSESSMENT MODE/);
  assert.match(banner.textContent, /Mentor and Professor are paused/);
  assert.match(banner.textContent, /no camera, screen, keyboard or clipboard/i);
  assert.match(r.textContent, /What is being assessed/);
  assert.match(r.textContent, /fresh challenge: it shows what you can do independently/);
  assert.match(r.textContent, /Your challenge/);
  assert.match(r.textContent, /Explain-back set/);
  assert.equal(byTag(r, "input").filter((i) => i.attrs.type === "radio").length, 2);
  assert.ok(byTag(r, "textarea").length >= 3, "explain-back prompt + response field + optional reflection");
  assert.match(r.textContent, /never graded/);
  assert.match(r.textContent, /About .*left\. The clock is server-side only/);
});

test("Submitting needs a declaration; then it saves the draft, submits and shows the result", async () => {
  const finalView = { status: "finalized", attempt: ATTEMPT({ status: "finalized", mentor_locked: false }), history: [], reviews: [],
    result: { id: "res-1", outcome: "passed", demonstration_effect: "counts_toward_demonstrated", has_record: true, gaps: [], remediation: [],
      report: { headline: "You passed independently.", platform_fact: { deterministic_checks: [{ label: "Correct", finding: "met", required: true }], assistance: { source_levels: [] }, independence: { challenge_issued: true } },
        grader_judgment: { ran: false, criteria: [], runs: [] }, learner_reflection: {}, professor_coaching: { note: "Separate." }, answers: { learner_state: {}, review_later: {} } } } };
  let served = 0;
  reset({
    "GET /academy/assessments/attempts/att-1/result": () => (served++ === 0 ? { status: "draft", attempt: ATTEMPT() } : finalView),
    "PUT /academy/assessments/attempts/att-1/draft": {},
    "POST /academy/assessments/attempts/att-1/submit": {},
  });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  const radio = byTag(r, "input").filter((i) => i.attrs.type === "radio")[0];
  radio.checked = true;
  await fire(radio, "change");
  const prompt = byTag(r, "textarea").find((t) => t.attrs["aria-label"] === "Explain it.");
  prompt.value = "In my own words"; await fire(prompt, "input");
  const explain = byTag(r, "textarea").find((t) => t.attrs["aria-label"] === "Your explanation");
  explain.value = "My explanation"; await fire(explain, "input");
  await flushTimers();
  assert.ok(calls.some((c) => c.method === "PUT"), "autosave writes the draft");

  const submit = buttons(r, "Submit for assessment")[0];
  await click(submit);
  assert.match(byClass(r, "error-banner")[0].textContent, /declaration/);
  assert.equal(calls.filter((c) => c.method === "POST").length, 0, "nothing is submitted without a declaration");

  const select = byTag(r, "select")[0]; select.value = "no_external_help"; await fire(select, "change");
  await click(submit); await settle(); await settle();
  const put = [...calls].reverse().find((c) => c.method === "PUT");
  assert.deepEqual(put.body.draft.responses.q1, { selected: [0] });
  assert.equal(put.body.draft.responses.p1.text, "In my own words");
  assert.equal(put.body.draft.fields.explanation, "My explanation");
  const post = calls.find((c) => c.method === "POST");
  assert.deepEqual(post.body, { attestation: { declaration: "no_external_help" } });
  assert.match(r.textContent, /Passed/);
});

test("Leave assessment abandons the attempt and returns to the Center", async () => {
  reset({ "GET /academy/assessments/attempts/att-1/result": { status: "draft", attempt: ATTEMPT() }, "POST /academy/assessments/attempts/att-1/abandon": {} });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  await click(buttons(r, "Leave assessment")[0]);
  assert.ok(calls.some((c) => c.path.endsWith("/abandon")));
  assert.equal(window.location.hash, "#/academy/assessments");
});

// -- E/F. Result and Needs Work ---------------------------------------------------------------------------------------------------------------

const REPORT = () => ({
  headline: "This needs more work. Here is exactly what to strengthen.",
  platform_fact: {
    deterministic_checks: [{ key: "long", label: "Enough detail", required: true, finding: "met", detail: "220 characters" }],
    assistance: { source_levels: ["h3"], source_max_assistance: "h3" }, independence: { challenge_issued: true, fresh_required: true, declaration: "no_external_help" },
    execution_verification: "not_applicable",
  },
  grader_judgment: { ran: true, grading_contract_version: "grading_contract_v1", crosscheck_ran: true, criteria: [
    { key: "accuracy", label: "Accurate", finding: "met", confidence: "high", rationale: "Correct.", quotes: ["schema"], gap: null, agreement: "agree" },
    { key: "limits", label: "States a limit", finding: "not_met", confidence: "medium", rationale: "No limit.", quotes: [], gap: "No limit is stated.", agreement: "agree" },
  ], runs: [{ slot: "primary", model_id: "m1", cost_status: "estimated" }, { slot: "crosscheck", model_id: "m2", cost_status: "unknown" }] },
  learner_reflection: { reflection: "Limits were hardest.", attestation: { declaration: "no_external_help" }, earlier_explain_back: [{ id: "e", label: "Your earlier explanation (formative, never graded)", response: "Earlier words", assistance_level: "h3" }] },
  professor_coaching: { label: "PROFESSOR COACHING", note: "Coaching is separate and on demand." },
  answers: { learner_state: { c1: { concept_name: "Structured Output", before: "understood", after: "understood", changed: false, overlays: [] } }, review_later: { c1: { next_review_at: "2026-12-01T00:00:00" } } },
});
const RESULT_VIEW = (result = {}, reviews = []) => ({
  status: "finalized", attempt: ATTEMPT({ status: "finalized", project_attempt_id: "pa-1", mentor_locked: false }), reviews,
  history: [{ kind: "deterministic", outcome: null }, { kind: "final", outcome: "needs_work" }],
  result: { id: "res-1", outcome: "needs_work", demonstration_effect: "none", has_record: false, report: REPORT(),
    gaps: [{ criterion_key: "limits", label: "States a limit", source: "grader", finding: "not_met", detail: "No limit is stated." }],
    remediation: [{ kind: "learning_item", label: "Revisit the lesson", for_criterion: "limits" }, { kind: "milestone", label: "Return to the project" }, { kind: "retry", label: "Try again with a new challenge" }],
    ...result },
});

test("Result keeps PLATFORM FACT, GRADER JUDGMENT, LEARNER REFLECTION and PROFESSOR COACHING distinct and labelled", async () => {
  reset({ "GET /academy/assessments/attempts/att-1/result": RESULT_VIEW() });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  const labels = byTag(r, "span").filter((s) => s.className.includes("badge")).map((s) => s.textContent);
  for (const label of ["PLATFORM FACT", "GRADER JUDGMENT", "LEARNER REFLECTION", "PROFESSOR COACHING"]) assert.ok(labels.includes(label), label);
  assert.equal(byClass(r, "block-fact").length, byClass(r, "block-judgment").length);
  const fact = byClass(r, "block-fact")[0].textContent;
  assert.match(fact, /Met — Enough detail/);
  assert.match(fact, /help|Your source work: with partial structure \(H3\)/);
  assert.match(fact, /fresh challenge in Assessment Mode/);
  assert.match(fact, /self-reported/);
  const judgment = byClass(r, "block-judgment")[0].textContent;
  assert.match(judgment, /Met · High confidence/);
  assert.match(judgment, /Not met · Medium confidence/);
  assert.match(judgment, /Gap: No limit is stated\./);
  assert.match(judgment, /a second model cross-checked/);
  assert.match(judgment, /estimated cost/);
  assert.match(judgment, /cost unknown/);
  assert.match(byClass(r, "block-reflection")[0].textContent, /Limits were hardest\./);
  assert.match(byClass(r, "block-reflection")[0].textContent, /formative, never graded/);
  assert.ok(links(r).includes("#/professor?intent=HELP_ME_AFTER_ASSESSMENT&target_type=assessment_result&target_id=res-1"));
  assert.match(r.textContent, /Did my Learner State change\?/);
  assert.match(r.textContent, /understood → understood \(unchanged\)/);
  assert.match(r.textContent, /never sets it directly/);
  assert.match(r.textContent, /Next review around 2026-12-01/);
});

test("Needs Work shows the specific gap, targeted next steps that link somewhere, and keeps history", async () => {
  reset({ "GET /academy/assessments/attempts/att-1/result": RESULT_VIEW() });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  const block = byClass(r, "block-needswork")[0];
  assert.match(block.textContent, /States a limit — No limit is stated\./);
  assert.match(block.textContent, /GRADER JUDGMENT/);
  assert.match(block.textContent, /Every attempt is kept/);
  const hrefs = links(block);
  assert.ok(hrefs.includes("#/academy"));
  assert.ok(hrefs.includes("#/academy/projects/attempts/pa-1"));
  assert.ok(hrefs.includes("#/academy/assessments/definitions/kc-x"));
  assert.match(r.textContent, /History of this attempt \(2 records\)/);
  assert.equal(links(r).some((h) => h.includes("records/")), false, "no record without a demonstrated pass");
});

test("a passing, demonstrated result links to the Demonstration Record", async () => {
  reset({ "GET /academy/assessments/attempts/att-1/result": RESULT_VIEW({ outcome: "passed", demonstration_effect: "counts_toward_demonstrated", has_record: true, gaps: [], remediation: [] }) });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  assert.match(r.textContent, /Recorded as evidence toward Demonstrated/);
  assert.ok(links(r).includes("#/academy/assessments/records/res-1"));
  assert.equal(byClass(r, "block-needswork").length, 0);
});

test("every learner-facing string on the result page is honest (no certificate / mastery language)", async () => {
  reset({ "GET /academy/assessments/attempts/att-1/result": RESULT_VIEW({ outcome: "passed", demonstration_effect: "counts_toward_demonstrated", has_record: true }) });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  const texts = walk(r).flatMap((n) => n.children.filter((c) => c instanceof FakeText).map((c) => c.text));
  assert.ok(texts.length > 20);
  for (const t of texts) assert.ok(isTruthfulText(t), t);
});

test("a provider failure shows the saved checks and a safe retry that calls grade", async () => {
  let served = 0;
  const pending = { status: "awaiting_grading", attempt: ATTEMPT({ status: "awaiting_grading", mentor_locked: false }), reviews: [], history: [],
    pending: { message: "The Grader could not finish. Your platform checks are saved and it is safe to try again.", deterministic_checks: [{ key: "long", label: "Enough detail", finding: "met" }] } };
  reset({
    "GET /academy/assessments/attempts/att-1/result": () => (served++ === 0 ? pending : RESULT_VIEW({ outcome: "passed", demonstration_effect: "counts_toward_demonstrated" })),
    "POST /academy/assessments/attempts/att-1/grade": {},
  });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  assert.match(r.textContent, /Your attempt is saved/);
  assert.match(r.textContent, /safe to try again/);
  assert.match(r.textContent, /Met — Enough detail/);
  await click(buttons(r, "Try grading again")[0]); await settle(); await settle();
  assert.ok(calls.some((c) => c.method === "POST" && c.path.endsWith("/grade")));
  assert.match(r.textContent, /Passed/);
});

test("provisional and unable-to-assess results say nothing changed and offer review or retry", async () => {
  for (const [outcome, phrase] of [["provisional", /Nothing changed/], ["unable_to_assess", /Nothing changed/], ["human_review_required", /person needs to review/]]) {
    reset({ "GET /academy/assessments/attempts/att-1/result": RESULT_VIEW({ outcome, demonstration_effect: "none", report: { ...REPORT(), headline: {
      provisional: "There was not enough confidence to decide. Nothing changed.", unable_to_assess: "We could not assess this right now. Nothing changed.", human_review_required: "Two judgments disagreed, so a person needs to review this. Nothing changed.",
    }[outcome] } }) });
    const r = root();
    await renderAssessmentAttempt(r, { id: "att-1" });
    assert.match(r.textContent, phrase);
    assert.match(r.textContent, /Ask for a review/);
    assert.match(r.textContent, /Nothing was recorded/);
  }
});

// -- H. Request review ------------------------------------------------------------------------------------------------------------------------

test("Request Review explains what the reviewer sees, needs a reason and explicit consent, then posts", async () => {
  let served = 0;
  reset({
    "GET /academy/assessments/attempts/att-1/result": () => (served++ === 0 ? RESULT_VIEW() : RESULT_VIEW({}, [{ id: "rev-1", status: "open", consented: true }])),
    "POST /academy/assessments/attempts/att-1/review": { id: "rev-1" },
  });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  const block = byClass(r, "block-review")[0];
  assert.match(block.textContent, /Only this attempt/);
  assert.match(block.textContent, /Nothing else: not your other attempts, your Mentor chats, or your profile\./);
  const send = buttons(block, "Request review")[0];
  await click(send);
  assert.match(block.textContent, /at least 10 characters/);
  byTag(block, "textarea")[0].value = "The second criterion was judged unfairly.";
  await click(send);
  assert.match(block.textContent, /explicit consent/);
  assert.equal(calls.filter((c) => c.method === "POST").length, 0);
  byTag(block, "input").find((i) => i.attrs.type === "checkbox").checked = true;
  await click(send); await settle(); await settle();
  assert.deepEqual(calls.find((c) => c.method === "POST").body, { reason: "The second criterion was judged unfairly.", consent: true });
  assert.match(r.textContent, /A reviewer will look at this attempt only/);
  assert.ok(buttons(r, "Withdraw request").length === 1);
});

test("a platform-opened review waits for the learner's consent and can be consented to", async () => {
  let served = 0;
  reset({
    "GET /academy/assessments/attempts/att-1/result": () => (served++ === 0 ? RESULT_VIEW({}, [{ id: "rev-1", status: "open", consented: false }]) : RESULT_VIEW({}, [{ id: "rev-1", status: "open", consented: true }])),
    "POST /academy/assessments/reviews/rev-1/consent": {},
  });
  const r = root();
  await renderAssessmentAttempt(r, { id: "att-1" });
  assert.match(r.textContent, /waiting for your consent before anyone can see your attempt/);
  await click(buttons(r, "I agree to share this attempt")[0]); await settle(); await settle();
  assert.ok(calls.some((c) => c.path.endsWith("/reviews/rev-1/consent")));
  assert.match(r.textContent, /A reviewer will look at this attempt only/);
});

// -- G. Demonstration Record ------------------------------------------------------------------------------------------------------------------

test("Demonstration Record shows evidence with provenance, verified vs self-reported, and a not-a-certificate notice", async () => {
  reset({ "GET /academy/assessments/records/res-1": {
    record_hash: "abcdef1234567890abcd", status: { status: "valid", reasons: [] },
    record: { title: "Demonstration Record", notice: "Not a certificate or credential.",
      assessment: { kind: "modification", definition_key: "mod-x", definition_version: 2 },
      concepts: [{ concept_name: "Structured Output", concept_version: 3 }], project: { title: "Extractor", version: 1 },
      evidence: [{ evidence_type: "modification", assistance_level: "h0", execution_verification: "platform_verified", grader: "deterministic" }],
      provenance: { verified_by_platform: ["You ran your changed extractor"], self_reported: ["Your declaration is self-reported and was not verified."] },
      grader: { ran: true, agent_version_id: "av-1", crosscheck_ran: true } } } });
  const r = root();
  await renderDemonstrationRecord(r, { id: "res-1" });
  const text = r.textContent;
  assert.match(text, /Demonstration Record/);
  assert.match(text, /Not a certificate or credential\./);
  assert.match(text, /Modification challenge · mod-x v2/);
  assert.match(text, /Structured Output \(Concept Version 3\)/);
  assert.match(text, /modification — help level H0, verification platform verified, judged by deterministic/);
  assert.match(text, /Verified by the platform/);
  assert.match(text, /You ran your changed extractor/);
  assert.match(text, /Self-reported/);
  assert.match(text, /Grader Agent Version av-1; cross-check ran/);
  assert.match(text, /Current/);
  assert.equal(buttons(r, "Export Markdown").length + buttons(r, "Export JSON").length, 2);
  assert.match(text, /stays private to you/);
});

// -- I. Reviewer ----------------------------------------------------------------------------------------------------------------------------------

test("Reviewer queue and detail show only shared content and record an audited decision", async () => {
  reset({ "GET /academy/assessments/reviews": [{ id: "rev-1", trigger: "learner_dispute", consented: true }, { id: "rev-2", trigger: "model_disagreement", consented: false }] });
  let r = root();
  await renderReviewQueue(r);
  assert.match(r.textContent, /learner dispute/);
  assert.match(r.textContent, /Waiting for consent/);
  assert.equal(links(r).filter((h) => h.includes("reviews/rev-")).length, 2);

  reset({ "GET /academy/assessments/reviews/rev-2": { consented: false, message: "The learner has not yet agreed to share this attempt, so no content is shown." } });
  r = root();
  await renderReviewDetail(r, { id: "rev-2" });
  assert.match(r.textContent, /no content is shown/);
  assert.equal(buttons(r, "Record decision").length, 0);

  reset({
    "GET /academy/assessments/reviews/rev-1": { consented: true, fingerprint: "f".repeat(64), allowed_decisions: ["confirm", "override_pass", "new_assessment"], reason_text: "Unfair.",
      definition: { title: "Explain", version: 1 }, submission: { fields: { explanation: "words" } }, attestation: { declaration: "no_external_help" },
      result: { outcome: "needs_work", effect: "none", criteria: [{ label: "Limits", finding: "not_met", confidence: "medium" }] } },
    "POST /academy/assessments/reviews/rev-1/decision": {},
  });
  r = root();
  await renderReviewDetail(r, { id: "rev-1" });
  assert.match(r.textContent, /Learner's reason: Unfair\./);
  assert.match(r.textContent, /Limits: Not met \(Medium confidence\)/);
  assert.match(r.textContent, /A required platform check cannot be overridden/);
  assert.deepEqual(byTag(r, "option").map((o) => o.textContent), ["Confirm the result", "Change to passed", "Issue a new assessment"]);
  byTag(r, "textarea")[0].value = "The judgment was too strict.";
  byTag(r, "select")[0].value = "override_pass";
  await click(buttons(r, "Record decision")[0]); await settle();
  assert.deepEqual(calls.find((c) => c.method === "POST").body, { decision: "override_pass", rationale: "The judgment was too strict." });
});
