import { test } from "node:test";
import assert from "node:assert/strict";
import {
  assessmentSummary, canContinue, checkAnswered, checkComplete, checkParts, dayGroups, hintsText, initialStepKey, labStages, learningStatusLine,
  nextDay, parseWorkspaceHash, programProgress, safeReturnHash, stepMeta, workspaceMode,
} from "../assets/js/workspace/model.js";

const view = (keys, current) => ({ steps: keys.map((key) => ({ key })), current_step_key: current });

test("a return hash is parsed into plain tokens only", () => {
  assert.deepEqual(parseWorkspaceHash("#/academy/level-1/4?step=lab-run&practice=abc-123"), { day: 4, step: "lab-run", practice: "abc-123" });
  assert.deepEqual(parseWorkspaceHash("#/academy/level-1/4"), { day: 4, step: null, practice: null });
  for (const evil of ["https://evil.example", "../../x", "<script>", "a b", "x".repeat(65), "a/b"]) {
    assert.equal(parseWorkspaceHash(`#/academy/level-1/4?step=${encodeURIComponent(evil)}&practice=${encodeURIComponent(evil)}`).step, null);
    assert.equal(parseWorkspaceHash(`#/academy/level-1/4?practice=${encodeURIComponent(evil)}`).practice, null);
  }
  assert.equal(parseWorkspaceHash("#/ask").day, null);
});

test("only a server-shaped Academy path is ever navigated to", () => {
  assert.equal(safeReturnHash("#/academy/level-1/5?step=lab&practice=p1"), "#/academy/level-1/5?step=lab&practice=p1");
  assert.equal(safeReturnHash("https://evil.example/phish"), "#/academy/level-1");
  assert.equal(safeReturnHash("javascript:alert(1)"), "#/academy/level-1");
  assert.equal(safeReturnHash(undefined), "#/academy/level-1");
});

test("the workspace opens on a valid requested step, else the server's current step, else the first", () => {
  const v = view(["a", "b", "c"], "b");
  assert.equal(initialStepKey(v, "c"), "c");
  assert.equal(initialStepKey(v, "nope"), "b");
  assert.equal(initialStepKey(view(["a", "b"], null), null), "a");
});

test("Continue mirrors what the server will accept and never lets a lab or practice be skipped over", () => {
  assert.equal(canContinue({ type: "teach", status: "opened" }), true);
  assert.equal(canContinue({ type: "reflect", status: "opened", response: null }), false);
  assert.equal(canContinue({ type: "reflect", status: "opened", response: { text: "x" } }), true);
  assert.equal(canContinue({ type: "think", status: "opened", committed: false }), false);
  assert.equal(canContinue({ type: "think", status: "opened", committed: true }), true);
  assert.equal(canContinue({ type: "check", status: "opened", check_result: { ever_passed: false } }), false);
  assert.equal(canContinue({ type: "check", status: "opened", check_result: { ever_passed: true } }), true);
  assert.equal(canContinue({ type: "lab", status: "opened", binding: { kind: "personal_lab", engine: "personal_lab_experiment", kit: { kit_key: "k", scenario_key: "s" } } }), false);
  assert.equal(canContinue({ type: "lab", status: "opened", binding: { kind: "personal_lab", engine: "agent_version" } }), true); // an engine launcher is optional
  assert.equal(canContinue({ type: "practice", status: "opened" }), false);
  assert.equal(canContinue({ type: "lab", status: "completed" }), true);
});

test("the learner always knows whether they are learning, practicing or demonstrating", () => {
  assert.equal(workspaceMode({ type: "teach" }), "learning");
  assert.equal(workspaceMode({ type: "lab" }), "practicing");
  assert.equal(workspaceMode({ type: "practice" }), "practicing");
  assert.equal(workspaceMode({ type: "teach" }, { inAssessment: true }), "demonstrating");
  assert.equal(stepMeta("nope").label, "nope");
});

test("learning complete and concept demonstrated are separate statements, and the assessment is a presentation of AIL.5C", () => {
  assert.deepEqual(learningStatusLine({ learning_complete: true, demonstrated: false }), { learning: "Learning complete", demonstrated: "Concept not yet demonstrated" });
  assert.equal(learningStatusLine({ learning_complete: false, demonstrated: true }).demonstrated, "Concept demonstrated");
  assert.equal(assessmentSummary(null), null);
  assert.equal(assessmentSummary({ status: "passed", definition_key: "k" }).label, "Passed");
  assert.equal(assessmentSummary({ status: "weird", definition_key: "k" }).label, "Not started");
});

test("hints used is read from the canonical assistance summary and absent when untracked", () => {
  assert.equal(hintsText({ tracked: false }), null);
  assert.equal(hintsText({ tracked: true, help_count: 0, hints_used: 0 }), "No Professor help used");
  assert.equal(hintsText({ tracked: true, help_count: 1, hints_used: 1 }), "1 hint used");
  assert.equal(hintsText({ tracked: true, help_count: 3, hints_used: 2 }), "2 hints used");
});

const guided = (over = {}) => ({
  status: "created", next_required: ["prediction", "runs", "change", "observation", "comparison", "reflection"], met: {}, responses: { predictions: {} }, runs: [],
  scenario: { predictions: [{ id: "p" }], requirements: { min_runs: 2, require_change: true, require_comparison: true, require_observation: true, require_reflection: true } }, ...over,
});

test("a guided lab shows Predict → Run → Observe → Change → Rerun → Compare → Reflect and marks the current stage", () => {
  const stages = labStages(guided());
  assert.deepEqual(stages.map((s) => s.key), ["prediction", "runs", "observation", "change", "rerun", "comparison", "reflection"]);
  assert.equal(stages.find((s) => s.current).key, "prediction");
  const later = labStages(guided({ met: { prediction: true, runs: true, change: true }, runs: [{ state: "completed" }, { state: "completed" }], responses: { predictions: {}, observation: { text: "x" } } }));
  assert.equal(later.find((s) => s.current).key, "comparison");
  assert.equal(labStages(guided({ status: "completed" })).every((s) => s.done), true);
});

test("independent practice shows fewer stages and no prediction or reflection", () => {
  const stages = labStages({ status: "created", met: {}, responses: { predictions: {} }, runs: [], scenario: { predictions: [], requirements: { min_runs: 2, require_change: true } } });
  assert.deepEqual(stages.map((s) => s.key), ["runs", "change", "rerun"]);
});

test("a knowledge check handles both the two-part and the single-choice shapes", () => {
  const two = { id: "q1", choices: { system: ["ai", "traditional_software"], claim: ["realistic", "overclaimed"] } };
  const one = { id: "q2", choices: [{ key: "A", text: "x" }, { key: "B", text: "y" }] };
  assert.deepEqual(checkParts(two).map((p) => p.name), ["system", "claim"]);
  assert.equal(checkParts(one)[0].name, null);
  assert.equal(checkAnswered(two, { q1: { system: "ai" } }), false);
  assert.equal(checkAnswered(two, { q1: { system: "ai", claim: "realistic" } }), true);
  assert.equal(checkAnswered(one, { q2: "A" }), true);
  assert.equal(checkComplete([two, one], { q1: { system: "ai", claim: "realistic" } }), false);
  assert.equal(checkComplete([two, one], { q1: { system: "ai", claim: "realistic" }, q2: "B" }), true);
  assert.equal(checkComplete([], {}), false);
});

test("the program overview groups by week, finds the next day, and counts learned and demonstrated separately", () => {
  const days = [
    { day: 1, week: 1, structured: true, learning_complete: true, demonstrated: true },
    { day: 2, week: 1, structured: true, learning_complete: true, demonstrated: false },
    { day: 3, week: 1, structured: true, learning_complete: false, demonstrated: false },
    { day: 6, week: 2, structured: false, learning_complete: null, demonstrated: false },
  ];
  assert.deepEqual(dayGroups(days).map((g) => [g.week, g.days.length]), [[1, 3], [2, 1]]);
  assert.equal(nextDay(days).day, 3);
  assert.deepEqual(programProgress(days), { total: 4, learned: 2, demonstrated: 1, pct: 50 });
});

test("a numeric knowledge-check question asks for the named fields and sends numbers", async () => {
  const { checkPayload: payload } = await import("../assets/js/workspace/model.js");
  const q = { id: "KC-17.1", prompt: "x", fields: ["recall", "precision"] };
  assert.deepEqual(checkParts(q).map((p) => [p.name, p.numeric]), [["recall", true], ["precision", true]]);
  assert.equal(checkAnswered(q, { "KC-17.1": { recall: "0.8" } }), false);
  assert.equal(checkAnswered(q, { "KC-17.1": { recall: "0.8", precision: "abc" } }), false);
  assert.equal(checkAnswered(q, { "KC-17.1": { recall: "0.8", precision: "0.29" } }), true);
  assert.deepEqual(payload([q, { id: "q2", choices: [{ key: "A", text: "x" }] }], { "KC-17.1": { recall: "0.8", precision: "0.29" }, q2: "A" }), { "KC-17.1": { recall: 0.8, precision: 0.29 }, q2: "A" });
  assert.equal(checkAnswered({ id: "z", prompt: "p" }, {}), false);
});
