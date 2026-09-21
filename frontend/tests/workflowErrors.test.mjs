import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../assets/js/api.js";
import {
  annotateIssues,
  flaggedNodeKeys,
  nodeKeysMentioned,
  normalizeApiError,
  normalizeValidationResult,
} from "../assets/js/workflowErrors.js";

test("a structured publish failure keeps each issue individually", () => {
  const error = new ApiError("Workflow validation failed (2 issues).", {
    status: 400,
    code: "workflow_validation_failed",
    detail: { issues: ["AGENT node planner requires config.agent_version_id", "No terminal node found (all nodes have outgoing edges)"] },
  });
  const normalized = normalizeApiError(error);
  assert.equal(normalized.isValidation, true);
  assert.equal(normalized.status, 400);
  assert.deepEqual(normalized.issues, [
    "AGENT node planner requires config.agent_version_id",
    "No terminal node found (all nodes have outgoing edges)",
  ]);
  assert.equal(normalized.message, "Workflow validation failed (2 issues).");
});

test("an ordinary error has a message and no issues", () => {
  const normalized = normalizeApiError(new ApiError("Cannot add nodes to published version", { status: 400, code: "http_error" }));
  assert.deepEqual(normalized.issues, []);
  assert.equal(normalized.isValidation, false);
  assert.equal(normalized.status, 400);
});

test("normalizeApiError never throws on odd input and drops non-string issues", () => {
  assert.equal(normalizeApiError(null).message, "Something went wrong.");
  assert.equal(normalizeApiError(undefined).status, null);
  assert.deepEqual(normalizeApiError({ message: "x", detail: { issues: ["ok", 3, null, "", { a: 1 }] } }).issues, ["ok"]);
  assert.deepEqual(normalizeApiError({ message: "x", detail: { issues: "not a list" } }).issues, []);
  assert.deepEqual(normalizeApiError({ message: "x", detail: "just a string" }).issues, []);
});

test("a Python-repr style message is never turned into issues (the old, broken shape)", () => {
  const legacy = new ApiError("{'error': 'DAG validation failed', 'issues': ['a']}", { status: 400, code: "http_error" });
  assert.deepEqual(normalizeApiError(legacy).issues, []);
});

test("normalizeValidationResult: valid only when the server says valid and lists no issues", () => {
  assert.deepEqual(normalizeValidationResult({ valid: true, issues: [] }), { valid: true, issues: [] });
  assert.deepEqual(normalizeValidationResult({ valid: false, issues: ["x", "y"] }), { valid: false, issues: ["x", "y"] });
  assert.deepEqual(normalizeValidationResult({ valid: true, issues: ["contradiction"] }), { valid: false, issues: ["contradiction"] });
  assert.deepEqual(normalizeValidationResult(null), { valid: false, issues: [] });
  assert.deepEqual(normalizeValidationResult({}), { valid: false, issues: [] });
});

const KEYS = ["planner", "engineer", "eval_test", "test", "model", "gate"];

test("nodes are matched only in the validator's own message shapes", () => {
  assert.deepEqual(nodeKeysMentioned("AGENT node planner requires config.agent_version_id", KEYS), ["planner"]);
  assert.deepEqual(nodeKeysMentioned("HUMAN_APPROVAL node gate requires config.approval_group", KEYS), ["gate"]);
  assert.deepEqual(nodeKeysMentioned("Node engineer is not connected to main workflow", KEYS), ["engineer"]);
  assert.deepEqual(nodeKeysMentioned("Unreachable node: engineer", KEYS), ["engineer"]);
  assert.deepEqual(nodeKeysMentioned("EVALUATION node eval_test must have exactly one incoming edge", KEYS), ["eval_test"]);
});

test("a key that is also an ordinary word is NOT matched by accident", () => {
  // 'model' and 'test' are node keys here, but these sentences merely contain the words.
  assert.deepEqual(nodeKeysMentioned("AGENT node planner has no model: choose a model for this node", KEYS), ["planner"]);
  assert.deepEqual(nodeKeysMentioned("Cycle detected in workflow DAG", KEYS), []);
  assert.deepEqual(nodeKeysMentioned("Workflow has no nodes", KEYS), []);
  assert.deepEqual(nodeKeysMentioned("No entry node found (all nodes have incoming edges)", KEYS), []);
});

test("keys are matched as whole tokens, never as a prefix of a longer key", () => {
  assert.deepEqual(nodeKeysMentioned("EVALUATION node eval_test must have exactly one incoming edge", KEYS), ["eval_test"]);
  assert.ok(!nodeKeysMentioned("EVALUATION node eval_test must have exactly one incoming edge", KEYS).includes("test"));
});

test("an edge issue names both of its node keys", () => {
  const issue = "Edge planner -> eval_test has a condition; conditional routing is not supported yet";
  assert.deepEqual(nodeKeysMentioned(issue, KEYS), ["eval_test", "planner"]);
  assert.deepEqual(nodeKeysMentioned("Duplicate edge: id1 -> id2", KEYS), []);
});

test("keys with regex metacharacters are matched literally", () => {
  assert.deepEqual(nodeKeysMentioned("AGENT node a.b requires config.agent_version_id", ["a.b", "axb"]), ["a.b"]);
  assert.deepEqual(nodeKeysMentioned("AGENT node axb requires config.agent_version_id", ["a.b"]), []);
});

test("annotateIssues returns 'possibly related' keys and flaggedNodeKeys unions them", () => {
  const annotated = annotateIssues(
    ["AGENT node planner requires config.agent_version_id", "Cycle detected in workflow DAG", "Node engineer is not connected to main workflow"],
    KEYS
  );
  assert.deepEqual(annotated.map((a) => a.nodeKeys), [["planner"], [], ["engineer"]]);
  assert.deepEqual([...flaggedNodeKeys(annotated)].sort(), ["engineer", "planner"]);
  assert.deepEqual(annotateIssues(null, KEYS), []);
  assert.deepEqual([...flaggedNodeKeys(null)], []);
});

test("a key that is a prefix of another key is never matched by the longer key's message", () => {
  const keys = ["eval", "eval_test", "planner"];
  assert.deepEqual(nodeKeysMentioned("EVALUATION node eval_test must have exactly one incoming edge", keys), ["eval_test"]);
  assert.deepEqual(nodeKeysMentioned("EVALUATION node eval must have exactly one incoming edge", keys), ["eval"]);
  assert.deepEqual(nodeKeysMentioned("Edge planner -> eval_test has a condition", keys), ["eval_test", "planner"]);
});
