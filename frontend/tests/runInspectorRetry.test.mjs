import { test } from "node:test";
import assert from "node:assert/strict";
import { WORKER_INTERRUPTED, retryMode } from "../assets/js/runInspector.js";

// MA7.8: which retry a failed step offers in the Control Room.

const failedRun = { status: "failed" };
const interrupted = { category: WORKER_INTERRUPTED, message: "Interrupted by worker restart", source: "agent_run" };

test("a failed AGENT step keeps the MA7.6B replacement-model retry", () => {
  const node = { node_type: "agent", status: "failed", failure: { category: "provider_invalid_response" } };
  assert.equal(retryMode(node, failedRun), "replacement");
});

test("an AGENT step interrupted by a worker restart is retried as configured", () => {
  assert.equal(retryMode({ node_type: "agent", status: "failed", failure: interrupted }, failedRun), "interrupted");
});

test("an EVALUATION step is retryable only when its evaluator was interrupted", () => {
  assert.equal(retryMode({ node_type: "evaluation", status: "failed", failure: interrupted }, failedRun), "interrupted");
  const genuine = { node_type: "evaluation", status: "failed", failure: { category: null, message: "bad JSON", source: "evaluation" } };
  assert.equal(retryMode(genuine, failedRun), null);
});

test("no retry is offered for approvals, unfinished steps or a run that is not failed", () => {
  assert.equal(retryMode({ node_type: "human_approval", status: "failed", failure: interrupted }, failedRun), null);
  assert.equal(retryMode({ node_type: "agent", status: "running", failure: null }, failedRun), null);
  assert.equal(retryMode({ node_type: "agent", status: "failed", failure: interrupted }, { status: "running" }), null);
  assert.equal(retryMode(null, failedRun), null);
});
