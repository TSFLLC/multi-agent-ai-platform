import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The Academy pages point at the EXISTING Assessment Center. These tests run the real page
// renderers against a small fake DOM and a scripted fake fetch.
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
  // just enough of querySelector for ".class" selectors
  querySelector(selector) {
    const cls = selector.replace(/^\./, "");
    for (const child of this.children) {
      if (!(child instanceof FakeNode)) continue;
      if (child.className.split(/\s+/).includes(cls)) return child;
      const inner = child.querySelector(selector);
      if (inner) return inner;
    }
    return null;
  }
}
globalThis.document = { createElement: (t) => new FakeNode(t), createTextNode: (t) => new FakeText(t), body: new FakeNode("body") };
globalThis.window = { location: { hash: "" }, __MAP_LOCAL_TOKEN__: "test-token" };

const calls = [];
let routes = {};
globalThis.fetch = async (path, opts = {}) => {
  const method = opts.method || "GET";
  calls.push({ method, path });
  const handler = routes[`${method} ${path.split("?")[0]}`];
  if (handler === undefined) return { ok: false, status: 404, statusText: "Not Found", text: async () => JSON.stringify({ error: { message: `no route ${method} ${path}` } }) };
  const out = typeof handler === "function" ? handler() : handler;
  return { ok: true, status: 200, statusText: "OK", text: async () => JSON.stringify(out) };
};

const { renderAcademyHome, renderAcademyProgram, renderAcademyLesson } = await import("../assets/js/pages/academy.js");
const { renderAcademy: renderLevel1, renderAcademyDay } = await import("../assets/js/pages/academy-level1.js");
const links = await import("../assets/js/academyLinks.js");

const walk = (n, out = []) => { if (n instanceof FakeNode) { out.push(n); n.children.forEach((c) => walk(c, out)); } return out; };
const byTag = (n, tag) => walk(n).filter((x) => x.tagName === tag);
const hrefs = (n) => byTag(n, "a").map((a) => a.attrs.href);
const buttons = (n, text) => byTag(n, "button").filter((b) => b.textContent.includes(text));
const settle = () => new Promise((r) => setImmediate(r));
const root = () => new FakeNode("div");
const reset = (r) => { routes = r; calls.length = 0; window.location.hash = ""; };
async function click(node) { for (const fn of node.listeners.click || []) await fn({ currentTarget: node }); }

const CENTER = "#/academy/assessments";
const DAY = (over = {}) => ({
  id: "item-1", day: 1, week: 1, title: "Day 1: What AI Is and Isn't", kind: "lecture", estimated_minutes: 60,
  body_md: "Long authored lecture.", concept_id: "c1", concept_slug: "what-ai-is-and-isnt", capstone_stage: null,
  spec: { day: 1, week: 1, assessment_definition_key: "level1-day-01-explain-ai", explain_back: { required: true, assessment_definition_key: "level1-day-01-explain-ai", binding_status: "bound" } },
  ...over,
});
const dayRoutes = (day, item) => ({
  [`GET /academy/level-1/days/${day}`]: item,
  // AIL5D.6: the Day page first asks whether the Day is structured; a legacy Day answers structured:false and keeps this page.
  [`GET /academy/level-1/days/${day}/learning`]: { structured: false, item_id: item.id, title: item.title },
  [`POST /academy/level-1/items/${item.id}/open`]: { evidence_id: "e", concept_id: "c1", learner_state: "exposed" },
});

// -- the pure binding helper --------------------------------------------------------------------------------

test("a Day bound to an assessment resolves the existing definition key and route", () => {
  const action = links.level1AssessmentAction(DAY());
  assert.equal(action.key, "level1-day-01-explain-ai");
  assert.equal(action.href, `${CENTER}/definitions/level1-day-01-explain-ai`);
  assert.equal(action.heading, "Demonstrate what you learned");
});

test("the binding is read from either place it is stored, and the key is URL-encoded", () => {
  const onlyExplainBack = DAY({ spec: { explain_back: { assessment_definition_key: "a b/c" } } });
  assert.equal(links.level1AssessmentAction(onlyExplainBack).href, `${CENTER}/definitions/a%20b%2Fc`);
});

test("unbound Days and malformed items get no assessment action", () => {
  assert.equal(links.level1AssessmentAction(DAY({ spec: { day: 2 } })), null);
  assert.equal(links.level1AssessmentAction(DAY({ spec: { explain_back: { required: false, assessment_definition_key: null } } })), null);
  assert.equal(links.level1AssessmentAction(null), null);
  assert.equal(links.level1AssessmentAction({}), null);
});

test("capstone Days keep their own flow and get no second action", () => {
  assert.equal(links.level1AssessmentAction(DAY({ day: 29, capstone_stage: "DEMONSTRATE", spec: { assessment_definition_key: "capstone-foundations" } })), null);
});

// -- AI Academy ---------------------------------------------------------------------------------------------

test("Academy Home offers the Assessment Center and Level 1 without any enrollment", async () => {
  reset({ "GET /academy/enrollments": [], "GET /academy/programs": [{ id: "p1", title: "Practical AI Foundations", description: "d" }] });
  const r = root();
  await renderAcademyHome(r); await settle();
  assert.ok(hrefs(r).includes(CENTER), "the Assessment Center link must not depend on an enrollment card");
  assert.ok(hrefs(r).includes("#/academy/level-1"));
});

test("Academy Home keeps its enrollment links and still offers the Assessment Center", async () => {
  reset({
    "GET /academy/enrollments": [{ id: "en1" }],
    "GET /academy/enrollments/en1": { enrollment: { id: "en1", status: "active" }, program: { title: "P", versions: [{ duration_days: 30 }] }, progress: { completed_items: 1, required_items: 2, demonstrated_concepts: 0, required_concepts: 1, practiced_or_better: 0, exposed: 1, understood_or_better: 0, review_due: 0, complete: false } },
  });
  const r = root();
  await renderAcademyHome(r); await settle();
  assert.ok(hrefs(r).includes("#/academy/enrollments/en1/today"));
  assert.ok(hrefs(r).filter((h) => h === CENTER).length >= 1);
});

test("a program page and a concept lesson page both link to the Assessment Center", async () => {
  reset({
    "GET /academy/programs/p1": { title: "P", description: "", versions: [{ status: "published", duration_days: 30, version: 1, items: [] }] },
    "GET /academy/concepts/c1/lesson": { concept_id: "c1", name: "What AI Is and Isn't", level: "foundational", kind: "definitional", concept_version: 1, learner_state: "exposed", review_overlays: [], plain_definition: "d", technical_explanation: null, examples_md: null, learning_items: [], evidence_count: 0, passed_evidence_count: 0, prerequisite_eligible: true, unmet_prerequisite_count: 0, review: { due: false, failed: false, due_reasons: [] } },
  });
  const program = root(); await renderAcademyProgram(program, { id: "p1" }); await settle();
  const lesson = root(); await renderAcademyLesson(lesson, { id: "c1" }); await settle();
  assert.ok(hrefs(program).includes(CENTER));
  assert.ok(hrefs(lesson).includes(CENTER));
});

test("generic CHECK QUESTION Learning Items keep their behaviour: static cards, no assessment link or handler", async () => {
  const items = [
    { id: "li1", title: "Explain AI in your own words", item_type: "check_question", body_md: "Explain it.", est_minutes: 10 },
    { id: "li2", title: "AI capability versus human understanding", item_type: "scenario", body_md: "Scenario.", est_minutes: 5 },
  ];
  reset({ "GET /academy/concepts/c1/lesson": { concept_id: "c1", name: "What AI Is and Isn't", level: "foundational", kind: "definitional", concept_version: 1, learner_state: "exposed", review_overlays: [], plain_definition: "d", technical_explanation: null, examples_md: null, learning_items: items, evidence_count: 0, passed_evidence_count: 0, prerequisite_eligible: true, unmet_prerequisite_count: 0, review: { due: false, failed: false, due_reasons: [] } } });
  const r = root(); await renderAcademyLesson(r, { id: "c1" }); await settle();
  const cards = walk(r).filter((n) => n.className.split(/\s+/).includes("academy-learning-item"));
  assert.equal(cards.length, 2);
  for (const card of cards) {
    assert.equal(byTag(card, "a").length, 0, "an item card is not a link");
    assert.equal(byTag(card, "button").length, 0, "an item card is not a button");
    assert.equal(Object.keys(card.listeners).length, 0, "an item card has no handlers");
  }
  assert.match(cards[0].textContent, /check question/);
  assert.match(cards[1].textContent, /scenario/);
});

// -- Level 1 ------------------------------------------------------------------------------------------------

test("the Level 1 30-day page links to the Assessment Center", async () => {
  reset({ "GET /academy/level-1/days": [{ day: 1, week: 1, title: "Day 1", kind: "lecture", state: "not_started", evidence_earned: false, estimated_minutes: 60 }] });
  const r = root(); await renderLevel1(r); await settle();
  assert.ok(hrefs(r).includes(CENTER));
  assert.ok(hrefs(r).includes("#/academy/level-1/1"), "the existing day list is unchanged");
});

test("an assessment-bound Day shows the contextual action pointing at the existing definition", async () => {
  reset(dayRoutes(1, DAY()));
  const r = root(); await renderAcademyDay(r, { day: "1" }); await settle();
  assert.ok(hrefs(r).includes(`${CENTER}/definitions/level1-day-01-explain-ai`));
  assert.ok(hrefs(r).includes(CENTER), "the Day page also links to the Assessment Center");
  assert.match(r.textContent, /Demonstrate what you learned/);
  assert.match(r.textContent, /Take the assessment/);
  assert.match(r.textContent, /Long authored lecture\./, "the Day body is untouched");
  assert.equal(calls.filter((c) => c.method === "POST" && /assessments/.test(c.path)).length, 0, "the page starts nothing itself");
});

test("an unbound Day shows no assessment action, but still links to the Center", async () => {
  reset(dayRoutes(2, DAY({ id: "item-2", day: 2, title: "Day 2", spec: { day: 2, week: 1, explain_back: { required: false, assessment_definition_key: null } } })));
  const r = root(); await renderAcademyDay(r, { day: "2" }); await settle();
  assert.equal(hrefs(r).filter((h) => h.includes("/definitions/")).length, 0);
  assert.doesNotMatch(r.textContent, /Demonstrate what you learned|Take the assessment/);
  assert.ok(hrefs(r).includes(CENTER));
});

test("a bound lab Day shows both the assessment action and the Personal Lab button", async () => {
  reset(dayRoutes(4, DAY({ id: "item-4", day: 4, kind: "lab", title: "Day 4", spec: { day: 4, week: 1, assessment_definition_key: "level1-day-04-experiment-reflection" } })));
  const r = root(); await renderAcademyDay(r, { day: "4" }); await settle();
  assert.ok(hrefs(r).includes(`${CENTER}/definitions/level1-day-04-experiment-reflection`));
  assert.equal(buttons(r, "Start Lab").length, 1);
});

test("Day 29 keeps its Open Assessment Center action and gets no duplicate contextual one", async () => {
  const day29 = DAY({ id: "item-29", day: 29, title: "Day 29", capstone_stage: "DEMONSTRATE", spec: { day: 29, week: 6, capstone_stage: "DEMONSTRATE", assessment_definition_key: "capstone-foundations" } });
  reset({
    ...dayRoutes(29, day29),
    "POST /academy/level-1/days/29/start-capstone": { assessment_center_url: "#/academy/assessments/definitions/capstone-foundations?project_attempt_id=pa1", project_url: "#/academy/projects/attempts/pa1" },
  });
  const r = root(); await renderAcademyDay(r, { day: "29" }); await settle();
  assert.doesNotMatch(r.textContent, /Demonstrate what you learned/);
  const [open] = buttons(r, "Open Assessment Center");
  assert.ok(open, "the existing Day 29 button is preserved");
  await click(open); await settle();
  assert.equal(window.location.hash, "#/academy/assessments/definitions/capstone-foundations?project_attempt_id=pa1");
});

// -- routes are untouched -----------------------------------------------------------------------------------

test("the existing assessment routes are still registered, and no new route was added for this", () => {
  const main = readFileSync(new URL("../assets/js/main.js", import.meta.url), "utf8");
  for (const route of [
    "/academy/assessments", "/academy/assessments/definitions/:key", "/academy/assessments/attempts/:id",
    "/academy/assessments/records/:id", "/academy/assessments/reviews", "/academy/assessments/reviews/:id",
  ]) assert.ok(main.includes(`registerRoute("${route}"`), route);
  assert.equal((main.match(/registerRoute\("\/academy\/assessments/g) || []).length, 6);
});
