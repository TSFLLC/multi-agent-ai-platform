import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../assets/js/api.js";
import {
  APPROVAL_WAIT_INTERVAL_MS,
  HIDDEN_TAB_INTERVAL_MS,
  LIVE_INTERVAL_MS,
  approvalStatusInfo,
  costLabel,
  decisionErrorMessage,
  findingCounts,
  findingInfo,
  formatDuration,
  formatTokenCount,
  formatUsd,
  isTerminalNode,
  isTerminalRun,
  nextStepText,
  nodeState,
  pendingApprovalNodes,
  pollIntervalFor,
  retryErrorMessage,
  runProgress,
  runStatusInfo,
  secondsBetween,
  shortId,
  usageLine,
} from "../assets/js/runState.js";

const node = (status, extra = {}) => ({ status, agent: null, approval_status: null, ...extra });

// -- run status -------------------------------------------------------------------------------------------

test("only completed, failed and cancelled runs are terminal", () => {
  for (const status of ["completed", "failed", "cancelled"]) assert.equal(isTerminalRun(status), true, status);
  for (const status of ["created", "running", "node_waiting_for_agent", "node_waiting_for_approval", "cancelling", "escalated"]) {
    assert.equal(isTerminalRun(status), false, status);
  }
  assert.equal(isTerminalNode("completed") && isTerminalNode("failed") && isTerminalNode("cancelled"), true);
  assert.equal(isTerminalNode("running") || isTerminalNode("pending") || isTerminalNode("waiting_for_approval"), false);
});

test("a run waiting for an approval reads as 'Waiting for your decision' and stays non-terminal", () => {
  const info = runStatusInfo("node_waiting_for_approval");
  assert.equal(info.label, "Waiting for your decision");
  assert.equal(info.tone, "attention");
  assert.match(info.help, /person must approve or reject/);
  assert.equal(isTerminalRun("node_waiting_for_approval"), false);
});

test("an unknown backend status is shown as-is, never hidden or invented", () => {
  assert.equal(runStatusInfo("some_new_status").label, "Some new status");
  assert.equal(runStatusInfo(undefined).label, "Unknown");
});

// -- node state mapping -------------------------------------------------------------------------------------

test("each backend node status maps to the state the user should see", () => {
  assert.equal(nodeState(node("pending")).label, "Waiting");
  assert.equal(nodeState(node("completed")).label, "Completed");
  assert.equal(nodeState(node("failed")).label, "Failed");
  assert.equal(nodeState(node("cancelled")).label, "Cancelled");
  assert.equal(nodeState(node("waiting_for_approval")).label, "Waiting for Human Approval");
  assert.equal(nodeState(node("waiting_for_approval")).key, "awaiting_decision");
});

test("Queued versus Running is read off the node's Agent Run status, not guessed", () => {
  assert.equal(nodeState(node("running", { agent: { status: "created" } })).label, "Queued");
  assert.equal(nodeState(node("running", { agent: { status: "running" } })).label, "Running");
  assert.equal(nodeState(node("running", { agent: { status: "waiting_for_model" } })).label, "Running");
  assert.equal(nodeState(node("running", { agent: { status: "cancelling" } })).label, "Cancelling");
  assert.equal(nodeState(node("running")).label, "Running"); // no agent recorded yet: still just running
});

test("a rejected gate and a cancelled gate are explained without inventing new states", () => {
  const rejected = nodeState(node("failed", { approval_status: "rejected" }));
  assert.equal(rejected.key, "failed");
  assert.match(rejected.detail, /person rejected/);
  const cancelled = nodeState(node("cancelled", { approval_status: "expired" }));
  assert.equal(cancelled.key, "cancelled");
  assert.match(cancelled.detail, /before a decision/);
});

test("an unrecognised node status falls through visibly", () => {
  assert.equal(nodeState(node("mystery")).label, "Mystery");
  assert.equal(nodeState(null).label, "Unknown");
});

test("progress counts group the states and add up to the total", () => {
  const nodes = [
    node("completed"), node("completed"), node("running", { agent: { status: "running" } }),
    node("running", { agent: { status: "created" } }), node("waiting_for_approval"), node("pending"), node("failed"), node("cancelled"),
  ];
  const counts = runProgress(nodes);
  assert.deepEqual(counts, { total: 8, completed: 2, running: 2, waiting: 1, awaiting_decision: 1, failed: 1, cancelled: 1 });
  assert.equal(runProgress(null).total, 0);
});

test("the fan-out state is visible: parallel branches can be in different states at once", () => {
  const branches = [node("completed"), node("running", { agent: { status: "running" } }), node("pending")];
  assert.deepEqual(branches.map((n) => nodeState(n).key), ["completed", "running", "waiting"]);
});

// -- approvals --------------------------------------------------------------------------------------------------

test("pending approvals are exactly the gates waiting on a person right now", () => {
  const detail = {
    nodes: [
      node("waiting_for_approval", { node_key: "gate", approval_status: "pending" }),
      node("completed", { node_key: "old", approval_status: "approved" }),
      node("waiting_for_approval", { node_key: "odd", approval_status: "approved" }),
    ],
  };
  assert.deepEqual(pendingApprovalNodes(detail).map((n) => n.node_key), ["gate"]);
  assert.deepEqual(pendingApprovalNodes(null), []);
});

test("approval statuses read as the person's decision, and a cancel is not a decision", () => {
  assert.equal(approvalStatusInfo("pending").label, "Waiting for your decision");
  assert.equal(approvalStatusInfo("approved").tone, "success");
  assert.equal(approvalStatusInfo("rejected").tone, "danger");
  assert.equal(approvalStatusInfo("expired").label, "Cancelled before a decision");
});

test("'what happens next' never implies anything decides on the person's behalf", () => {
  const waiting = { status: "node_waiting_for_approval", nodes: [node("waiting_for_approval", { approval_status: "pending" })] };
  assert.match(nextStepText(waiting), /waiting for your decision/i);
  assert.match(nextStepText(waiting), /Nothing continues until you/);
  const two = { status: "node_waiting_for_approval", nodes: [node("waiting_for_approval", { approval_status: "pending" }), node("waiting_for_approval", { approval_status: "pending" })] };
  assert.match(nextStepText(two), /2 decisions/);
  for (const status of ["running", "completed", "failed", "cancelled", "cancelling", "created"]) {
    assert.ok(nextStepText({ status, nodes: [] }).length > 10, status);
    assert.ok(!/auto|automatic/i.test(nextStepText({ status, nodes: [] })), status);
  }
  assert.equal(nextStepText(null), "");
});

test("decision failures become clear messages", () => {
  assert.match(decisionErrorMessage(new ApiError("x", { status: 403 })), /permission/);
  assert.match(decisionErrorMessage(new ApiError("x", { status: 409, code: "fingerprint_mismatch" })), /evidence changed/);
  assert.match(decisionErrorMessage(new ApiError("x", { status: 409, code: "invalid_state_transition" })), /already decided differently/);
  assert.match(decisionErrorMessage(new ApiError("x", { status: 401 })), /not authorized/);
  assert.match(decisionErrorMessage(new ApiError("x", { status: 404 })), /no longer exists/);
  assert.equal(decisionErrorMessage(new ApiError("boom", { status: 500 })), "boom");
  assert.ok(decisionErrorMessage(null).length > 5);
});

test("retry failures become clear messages -- a model-availability wording is used ONLY for that exact reason", () => {
  assert.match(retryErrorMessage(new ApiError("x", { status: 403 })), /permission/);
  assert.match(retryErrorMessage(new ApiError("x", { status: 401 })), /not authorized/);
  assert.match(
    retryErrorMessage(new ApiError("Provider model pm-1 does not exist.", { status: 400, code: "retry_not_allowed" })),
    /no longer available or active/
  );
  assert.match(
    retryErrorMessage(new ApiError("Model 'x' is not active (status='deprecated').", { status: 400, code: "retry_not_allowed" })),
    /no longer available or active/
  );
  assert.match(
    retryErrorMessage(new ApiError("Provider 'x' is currently down.", { status: 400, code: "retry_not_allowed" })),
    /no longer available or active/
  );
  // every OTHER retry_not_allowed reason is shown verbatim -- never repainted as a model problem
  assert.equal(
    retryErrorMessage(new ApiError("Node run is completed, not FAILED -- nothing to retry.", { status: 400, code: "retry_not_allowed" })),
    "Node run is completed, not FAILED -- nothing to retry."
  );
  assert.equal(
    retryErrorMessage(new ApiError("Only AGENT nodes can be retried (node 'gate' is human_approval).", { status: 400, code: "retry_not_allowed" })),
    "Only AGENT nodes can be retried (node 'gate' is human_approval)."
  );
  // a genuine 404/409 is reported as such, never disguised as a model-availability message
  assert.match(retryErrorMessage(new ApiError("x", { status: 404 })), /could not be found/);
  assert.match(retryErrorMessage(new ApiError("x", { status: 409 })), /already retried/);
  assert.equal(retryErrorMessage(new ApiError("boom", { status: 500 })), "boom");
  assert.ok(retryErrorMessage(null).length > 5);
});

// -- polling cadence ---------------------------------------------------------------------------------------------

test("polling is slower while only a person is awaited, and slow when the tab is hidden", () => {
  assert.equal(pollIntervalFor({ status: "running" }), LIVE_INTERVAL_MS);
  assert.equal(pollIntervalFor({ status: "node_waiting_for_approval" }), APPROVAL_WAIT_INTERVAL_MS);
  assert.ok(APPROVAL_WAIT_INTERVAL_MS > LIVE_INTERVAL_MS);
  assert.equal(pollIntervalFor({ status: "running" }, { hidden: true }), HIDDEN_TAB_INTERVAL_MS);
  assert.equal(pollIntervalFor({ status: "node_waiting_for_approval" }, { hidden: true }), HIDDEN_TAB_INTERVAL_MS);
  assert.equal(pollIntervalFor(null), LIVE_INTERVAL_MS);
});

// -- formatting -------------------------------------------------------------------------------------------------------

test("durations", () => {
  assert.equal(formatDuration(0), "0s");
  assert.equal(formatDuration(7.4), "7s");
  assert.equal(formatDuration(75), "1m 15s");
  assert.equal(formatDuration(3725), "1h 02m");
  assert.equal(formatDuration(null), "—");
  assert.equal(formatDuration(-5), "0s");
  assert.equal(formatDuration("nope"), "—");
});

test("seconds between timestamps, and a live elapsed time for a running step", () => {
  assert.equal(secondsBetween("2026-01-01T00:00:00Z", "2026-01-01T00:01:30Z"), 90);
  assert.equal(secondsBetween("2026-01-01T00:00:00Z", null, Date.parse("2026-01-01T00:00:10Z")), 10);
  assert.equal(secondsBetween(null, null, 5), null);
  assert.equal(secondsBetween("garbage", "2026-01-01T00:00:00Z"), null);
  assert.equal(secondsBetween("2026-01-01T00:00:10Z", "2026-01-01T00:00:00Z"), 0);
});

test("money is shown exactly as far as it is meaningful, from a Decimal string", () => {
  assert.equal(formatUsd("0.00000000"), "$0.00");
  assert.equal(formatUsd(0), "$0.00");
  assert.equal(formatUsd("0.015"), "$0.015");
  assert.equal(formatUsd("0.0150"), "$0.015");
  assert.equal(formatUsd("0.123456"), "$0.1235");
  assert.equal(formatUsd("0.00042"), "$0.00042");
  assert.equal(formatUsd("12.3456"), "$12.35");
  assert.equal(formatUsd("1"), "$1.00");
  assert.equal(formatUsd(null), "—");
  assert.equal(formatUsd(""), "—");
  assert.equal(formatUsd("abc"), "—");
});

test("a cost that includes an estimate is labelled estimated; an exact one is labelled exact", () => {
  assert.deepEqual(costLabel({ cost_amount: "0.015", cost_is_estimated: true }), { text: "$0.015 (estimated)", estimated: true, exact: false, unavailable: false });
  assert.deepEqual(costLabel({ cost_amount: "0", cost_is_estimated: false }), { text: "$0.00 (exact)", estimated: false, exact: true, unavailable: false });
  assert.equal(costLabel(null).unavailable, true);
  const mixed = costLabel({ cost_amount: null, cost_currency: "mixed", cost_is_estimated: false });
  assert.equal(mixed.text, "mixed currencies");
  assert.equal(mixed.unavailable, true);
});

test("usage lines never invent zeros for a step with no model calls", () => {
  assert.equal(usageLine(null), "No model calls yet");
  assert.equal(usageLine({ total_tokens: 1234, cost_amount: "0.015", cost_is_estimated: true }), "1,234 tokens · $0.015 (estimated)");
  assert.equal(formatTokenCount(1234567), "1,234,567");
  assert.equal(formatTokenCount(null), "—");
});

test("short ids", () => {
  assert.equal(shortId("abcdef1234567890"), "abcdef12");
  assert.equal(shortId(null), "—");
});

// -- evaluation findings are evidence --------------------------------------------------------------------------------

test("evaluation findings keep their four values and get no verdict, score or recommendation", () => {
  assert.deepEqual(["met", "partial", "not_met", "not_applicable"].map((f) => findingInfo(f).label), ["MET", "PARTIAL", "NOT MET", "NOT APPLICABLE"]);
  assert.equal(findingInfo("not_met").tone, "danger");
  assert.equal(findingInfo("weird").label, "WEIRD");
  const counts = findingCounts([{ finding: "met" }, { finding: "met" }, { finding: "not_met" }, { finding: "partial" }, { finding: "not_applicable" }]);
  assert.deepEqual(counts, { met: 2, partial: 1, not_met: 1, not_applicable: 1 });
  assert.deepEqual(Object.keys(counts).sort(), ["met", "not_applicable", "not_met", "partial"]); // counts only: no score/total/verdict key
});
