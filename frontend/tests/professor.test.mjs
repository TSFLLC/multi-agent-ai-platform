import { test } from "node:test";
import assert from "node:assert/strict";

const { statusMessage } = await import("../assets/js/pages/professor.js");

test("Professor learner states use clear beginner-facing labels", () => {
  assert.equal(statusMessage("preparing"), "Preparing context…");
  assert.equal(statusMessage("asking"), "Asking Professor…");
  assert.equal(statusMessage("complete"), "Complete");
  assert.equal(statusMessage("budget_exceeded"), "Budget limit");
  assert.equal(statusMessage("validation_error"), "Validation error");
  assert.equal(statusMessage("provider_timeout"), "provider timeout");
});
