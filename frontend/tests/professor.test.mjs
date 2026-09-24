import { test } from "node:test";
import assert from "node:assert/strict";

const { statusMessage, targetCompatible, targetRequired, buildProfessorRequest } = await import("../assets/js/pages/professor.js");

test("Professor learner states use clear beginner-facing labels", () => {
  assert.equal(statusMessage("preparing"), "Preparing context…");
  assert.equal(statusMessage("asking"), "Asking Professor…");
  assert.equal(statusMessage("complete"), "Complete");
  assert.equal(statusMessage("budget_exceeded"), "Budget limit");
  assert.equal(statusMessage("validation_error"), "Validation error");
  assert.equal(statusMessage("provider_timeout"), "provider timeout");
});

test("only target-dependent intents require an explicit target", () => {
  assert.equal(targetRequired("ASK_PROFESSOR"), false);
  assert.equal(targetRequired("WHAT_SHOULD_I_LEARN_NEXT"), false);
  assert.equal(targetRequired("EXPLAIN_THIS"), true);
  assert.equal(targetRequired("UNDERSTAND_MY_EXPERIMENT"), true);
  assert.equal(targetRequired("HELP_ME_REVIEW"), true);
  assert.equal(targetRequired("WHY_DOES_THIS_MATTER"), true);
});

test("Professor request sends the selected canonical target", () => {
  const target = { type: "experiment", id: "experiment-123", label: "A test" };
  assert.equal(targetCompatible("UNDERSTAND_MY_EXPERIMENT", target), true);
  assert.deepEqual(buildProfessorRequest({
    intent: "UNDERSTAND_MY_EXPERIMENT",
    question: "Explain this experiment.",
    target,
  }), {
    intent: "UNDERSTAND_MY_EXPERIMENT",
    question: "Explain this experiment.",
    target: { type: "experiment", id: "experiment-123" },
  });
});

test("incompatible or missing targets cannot be submitted", () => {
  assert.equal(targetCompatible("EXPLAIN_THIS", null), false);
  assert.equal(targetCompatible("EXPLAIN_THIS", { type: "experiment", id: "x" }), false);
  assert.equal(targetCompatible("HELP_ME_REVIEW", { type: "review_attempt", id: "review-1" }), true);
  assert.deepEqual(buildProfessorRequest({ intent: "ASK_PROFESSOR", question: "What next?", target: null }), {
    intent: "ASK_PROFESSOR",
    question: "What next?",
  });
});
