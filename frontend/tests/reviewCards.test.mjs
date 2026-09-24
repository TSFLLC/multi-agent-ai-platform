import { test } from "node:test";
import assert from "node:assert/strict";

// A small fake DOM (as in stayAhead.test.mjs), with enough of the event and
// child API for the inline review flow.
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
    this.checked = false;
    this.onclick = null;
  }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  appendChild(child) { this.children.push(child); return child; }
  get firstChild() { return this.children[0] || null; }
  removeChild(child) { this.children = this.children.filter((c) => c !== child); return child; }
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this.children = [new FakeText(value)]; }
}
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeText(text),
};

const {
  STAY_AHEAD_SECTIONS, reviewActionModel, reviewCardModel, reviewOutcome, reviewReasonLabel, overlayLabel,
  stayAheadModel, validateReviewSelection, allEmptyText,
} = await import("../assets/js/stayAhead.js");
const { reviewCard, stayAheadBlock } = await import("../assets/js/pages/stayAhead.js");

function walk(node, out = []) {
  if (node instanceof FakeNode) {
    out.push(node);
    node.children.forEach((child) => walk(child, out));
  }
  return out;
}
const byTag = (node, tag) => walk(node).filter((n) => n.tagName === tag);
const byClass = (node, cls) => walk(node).filter((n) => n.className.split(/\s+/).includes(cls));
async function click(node) {
  for (const fn of node.listeners.click || []) await fn({ currentTarget: node });
  if (typeof node.onclick === "function") await node.onclick({ currentTarget: node });
}
async function choose(node) {
  node.checked = true;
  for (const fn of node.listeners.change || []) await fn({ currentTarget: node });
}

const T = "2026-09-20T12:00:00Z";

function card(overrides = {}) {
  return {
    id: "REVIEW:c-1",
    kind: "REVIEW_DUE",
    title: "Review due: “Tokens”",
    what: "Your review interval for this Concept (180 days) has elapsed.",
    why: "Your latest evidence for this definitional Concept is from 2026-03-01. The interval is 180 days, and it has passed. It is reviewed because it is a core Concept.",
    reason_codes: ["REVIEW_ELIGIBLE_CORE", "REVIEW_INTERVAL_ELAPSED"],
    concept: { id: "c-1", kind: "definitional", name: "Tokens", slug: "tokens" },
    learner_state: { ladder: "demonstrated", overlays: ["review_due"] },
    baseline: { evidence_id: "e-1", evidence_type: "knowledge_check", recorded_at: "2026-03-01T00:00:00Z", concept_version: 1 },
    interval: { days: 180, base_days: 180, streak: 0, due_at: "2026-08-28T00:00:00Z" },
    material_changes: [],
    attempt: null,
    action: { kind: "start_review", learning_item_id: "i-1" },
    evidence_refs: [],
    due_at: "2026-08-28T00:00:00Z",
    ...overrides,
  };
}

function data(reviewItems = [], total = reviewItems.length, shown = reviewItems.length) {
  const empty = { total: 0, shown: 0, items: [] };
  return {
    generated_at: T, window_days: 30, window_start: T,
    sections: {
      worth_revisiting: empty, used_models_changed: empty, watched_developments: empty,
      experiments_to_rerun: empty, concepts_changed: empty,
      review: { total, shown, items: reviewItems },
    },
  };
}

function fakeApi({ startResult, completeResult, startError, completeError } = {}) {
  const calls = [];
  return {
    calls,
    start: async (conceptId) => {
      calls.push(["start", conceptId]);
      if (startError) throw startError;
      return startResult;
    },
    complete: async (attemptId, selected) => {
      calls.push(["complete", attemptId, selected]);
      if (completeError) throw completeError;
      return completeResult;
    },
  };
}

const QUESTION = {
  attempt: { id: "a-1", status: "started" },
  item: { id: "i-1", title: "Which is right?", body_md: "Pick the right one.", item_type: "check_question", options: ["wrong", "right", "also wrong"], multiple: false },
  created: true,
};

test("Review is a section of Today, second in order, with its own one-line empty state", () => {
  assert.deepEqual(STAY_AHEAD_SECTIONS.map((s) => s.key).slice(0, 2), ["worth_revisiting", "review"]);
  const EMPTY_LINE = "Nothing you have demonstrated is due for review.";

  // With a card: the Review section shows it, not the empty line.
  const withCard = stayAheadBlock(data([card()]));
  assert.equal(byTag(withCard, "article").length, 1);
  assert.ok(!withCard.textContent.includes(EMPTY_LINE));

  // Something else has content but Review is empty: exactly one plain line, no backfill.
  const other = { id: "m", family: "USED_MODEL_CHANGED", title: "t", what_changed: "w", why: "y", reason_codes: [], changed_at: T, links: [] };
  const base = data([]);
  const elsewhere = stayAheadBlock({ ...base, sections: { ...base.sections, used_models_changed: { total: 1, shown: 1, items: [other] } } });
  assert.ok(elsewhere.textContent.includes(EMPTY_LINE));
  assert.equal(byClass(elsewhere, "stay-ahead-review").length, 0);

  // Everything empty: the single all-empty message.
  assert.match(stayAheadBlock(data()).textContent, new RegExp(allEmptyText(30)));
  assert.equal(stayAheadModel(data()).allEmpty, true);
  assert.equal(stayAheadModel(data([card()])).allEmpty, false);
});

test("a review card exposes concept, ladder, overlay, reasons, why, baseline, attempt state and action", () => {
  const model = reviewCardModel(card({
    attempt: { id: "a-0", status: "failed", started_at: T, completed_at: "2026-09-19T00:00:00Z" },
    learner_state: { ladder: "demonstrated", overlays: ["review_due", "review_failed"] },
  }));
  assert.equal(model.conceptName, "Tokens");
  assert.equal(model.learnerState.label, "Demonstrated");
  assert.deepEqual(model.learnerState.overlays, ["Review due", "Latest review needs attention"]);
  assert.deepEqual(model.reasons, ["Core Concept", "Review interval elapsed"]);
  assert.match(model.why, /core Concept/);
  assert.equal(model.baseline, "Based on your knowledge check from 2026-03-01 (Concept version 1).");
  assert.equal(model.interval, "Review interval: 180 days.");
  assert.equal(model.attempt, "Your latest review did not pass (2026-09-19).");
  assert.deepEqual(model.action, { kind: "start_review", label: "Start review", message: null });
});

test("card kinds, reasons and overlays use plain deterministic labels", () => {
  assert.equal(reviewCardModel(card({ kind: "REVIEW_FAILED" })).kindLabel, "Latest review needs attention");
  assert.equal(reviewCardModel(card({ kind: "CONCEPT_CHANGED_REVIEW" })).kindLabel, "Concept changed; review recommended");
  assert.equal(reviewCardModel(card()).kindLabel, "Review due");
  assert.equal(reviewReasonLabel("REVIEW_CONCEPT_CHANGED"), "Concept changed materially");
  assert.equal(reviewReasonLabel("REVIEW_ELIGIBLE_PLAN"), "In your active plan");
  assert.equal(reviewReasonLabel("REVIEW_FAILED_LATEST"), "Latest review did not pass");
  assert.equal(overlayLabel("review_due"), "Review due");
  const extended = reviewCardModel(card({ interval: { days: 360, base_days: 180, streak: 1, due_at: T } }));
  assert.equal(extended.interval, "Review interval: 360 days, extended after 1 successful review.");
});

test("actions: start, continue, and honest reasons when a review is not available", () => {
  assert.equal(reviewActionModel({ kind: "continue_review", attempt_id: "a" }).label, "Continue review");
  assert.equal(reviewActionModel({ kind: "unavailable", reason: "NO_REVIEW_ITEM" }).message, "No reviewed check is available for this Concept yet.");
  assert.equal(reviewActionModel({ kind: "unavailable", reason: "COOLDOWN", available_after: "2026-09-21T01:00:00Z" }).message, "You can try again after 2026-09-21.");
  assert.equal(reviewActionModel({ kind: "unavailable", reason: "WHO_KNOWS" }).label, null);
  assert.equal(reviewActionModel(undefined).kind, "unavailable");
});

test("rendering a review card makes no request and offers exactly one explicit action", () => {
  const api = fakeApi({ startResult: QUESTION });
  const node = reviewCard(reviewCardModel(card()), { reviewApi: api });
  assert.deepEqual(api.calls, []); // nothing runs on render
  const buttons = byTag(node, "button");
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].textContent, "Start review");
  assert.match(node.textContent, /What changed: /);
  assert.match(node.textContent, /Why you are seeing this: /);
  assert.match(node.textContent, /Your record: Demonstrated/);
  assert.match(node.textContent, /Based on your knowledge check/);
});

test("an unavailable review shows its reason and no button", () => {
  const node = reviewCard(reviewCardModel(card({ action: { kind: "unavailable", reason: "NO_REVIEW_ITEM" } })), { reviewApi: fakeApi() });
  assert.equal(byTag(node, "button").length, 0);
  assert.match(node.textContent, /No reviewed check is available for this Concept yet\./);
});

test("the review runs only after the user clicks: start, choose, submit, then the outcome", async () => {
  const api = fakeApi({
    startResult: QUESTION,
    completeResult: { passed: true, message: "Correct. This review was recorded as learning evidence, and your review clock has been reset.", learner_state: { ladder: "demonstrated", overlays: [] } },
  });
  const node = reviewCard(reviewCardModel(card()), { reviewApi: api });

  await click(byTag(node, "button")[0]);
  assert.deepEqual(api.calls, [["start", "c-1"]]);
  assert.match(node.textContent, /Which is right\?/);
  assert.match(node.textContent, /Pick the right one\./);
  const inputs = byTag(node, "input");
  assert.equal(inputs.length, 3);
  assert.ok(inputs.every((i) => i.attrs.type === "radio"));
  assert.ok(!/answer_key|answerKey/.test(node.textContent));

  const submit = byTag(node, "button").find((b) => b.textContent === "Submit answer");
  assert.equal(submit.disabled, true); // nothing chosen yet
  await choose(inputs[1]);
  assert.equal(submit.disabled, false);

  await click(submit);
  assert.deepEqual(api.calls[1], ["complete", "a-1", [1]]);
  assert.equal(api.calls.length, 2);
  assert.match(node.textContent, /recorded as learning evidence/);
  assert.match(node.textContent, /Reload Today to see your updated review list\./);
  assert.equal(byTag(node, "button").length, 0); // the outcome is final; no re-run from the card
});

test("a wrong answer is described without any claim that the record changed", async () => {
  const message = "Not quite. Your record is unchanged: the Concept stays Demonstrated. You can review it again after a short wait.";
  const api = fakeApi({ startResult: QUESTION, completeResult: { passed: false, message } });
  const node = reviewCard(reviewCardModel(card()), { reviewApi: api });
  await click(byTag(node, "button")[0]);
  await choose(byTag(node, "input")[0]);
  await click(byTag(node, "button").find((b) => b.textContent === "Submit answer"));
  assert.ok(node.textContent.includes(message));
  assert.equal(byClass(node, "not-passed").length, 1);
});

test("a multiple-answer question uses checkboxes and sends every chosen option in order", async () => {
  const multiple = { ...QUESTION, item: { ...QUESTION.item, multiple: true } };
  const api = fakeApi({ startResult: multiple, completeResult: { passed: false, message: "Not quite." } });
  const node = reviewCard(reviewCardModel(card()), { reviewApi: api });
  await click(byTag(node, "button")[0]);
  const inputs = byTag(node, "input");
  assert.ok(inputs.every((i) => i.attrs.type === "checkbox"));
  await choose(inputs[2]);
  await choose(inputs[0]);
  await click(byTag(node, "button").find((b) => b.textContent === "Submit answer"));
  assert.deepEqual(api.calls[1], ["complete", "a-1", [0, 2]]);
});

test("errors are shown in the card, and starting can be retried", async () => {
  const api = fakeApi({ startError: new Error("A retry is available after a short wait.") });
  const node = reviewCard(reviewCardModel(card()), { reviewApi: api });
  await click(byTag(node, "button")[0]);
  assert.match(node.textContent, /A retry is available after a short wait\./);
  assert.equal(byTag(node, "button").length, 1);
  assert.equal(byTag(node, "button")[0].textContent, "Start review");

  const failing = fakeApi({ startResult: QUESTION, completeError: new Error("Choose an answer before submitting.") });
  const second = reviewCard(reviewCardModel(card()), { reviewApi: failing });
  await click(byTag(second, "button")[0]);
  await choose(byTag(second, "input")[1]);
  const submit = byTag(second, "button").find((b) => b.textContent === "Submit answer");
  await click(submit);
  assert.match(second.textContent, /Choose an answer before submitting\./);
  assert.equal(submit.disabled, false); // the user can correct and resubmit
});

test("selection validation mirrors the server rules", () => {
  assert.equal(validateReviewSelection({ multiple: false }, new Set()), "Choose an answer before submitting.");
  assert.equal(validateReviewSelection({ multiple: false }, new Set([0, 1])), "This question takes exactly one answer.");
  assert.equal(validateReviewSelection({ multiple: false }, new Set([2])), null);
  assert.equal(validateReviewSelection({ multiple: true }, new Set([0, 1])), null);
});

test("outcome text falls back safely", () => {
  assert.deepEqual(reviewOutcome({ passed: true, message: "ok" }), { passed: true, message: "ok", note: "Reload Today to see your updated review list." });
  assert.equal(reviewOutcome({ passed: false }).message, "Not quite.");
});

test("a continue action uses the same explicit start call (idempotent server side)", async () => {
  const api = fakeApi({ startResult: { ...QUESTION, created: false } });
  const node = reviewCard(reviewCardModel(card({ action: { kind: "continue_review", attempt_id: "a-1" },
    attempt: { id: "a-1", status: "started", started_at: T } })), { reviewApi: api });
  assert.equal(byTag(node, "button")[0].textContent, "Continue review");
  assert.match(node.textContent, /A review is in progress \(started 2026-09-20\)\./);
  await click(byTag(node, "button")[0]);
  assert.deepEqual(api.calls, [["start", "c-1"]]);
});

test("no user-facing review string scores, ranks or urges", () => {
  const model = reviewCardModel(card());
  const strings = [
    model.kindLabel, model.title, model.what, model.why, ...model.reasons, model.baseline, model.interval,
    ...["REVIEW_ELIGIBLE_CORE", "REVIEW_INTERVAL_ELAPSED", "REVIEW_CONCEPT_CHANGED", "REVIEW_FAILED_LATEST"].map(reviewReasonLabel),
    overlayLabel("review_due"), overlayLabel("review_failed"),
    reviewActionModel({ kind: "unavailable", reason: "COOLDOWN", available_after: T }).message,
  ].join(" ").toLowerCase();
  for (const word of ["score", "rank", "priority", "importance", "urgent", "must", "best", "#1"]) {
    assert.ok(!strings.includes(word), word);
  }
});
