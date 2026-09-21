import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { edgeQuery, newIdempotencyKey, workflowApi } from "../assets/js/workflowApi.js";
import {
  SELECTED_PROJECT_KEY,
  readStoredProjectId,
  resolveSelectedProject,
  writeStoredProjectId,
} from "../assets/js/projectSelection.js";

// Capture what api.js would send, without a server.
let calls;
let nextResponse;
const realFetch = globalThis.fetch;

beforeEach(() => {
  calls = [];
  nextResponse = { status: 200, body: {} };
  globalThis.fetch = async (path, init) => {
    calls.push({ path, method: init.method, body: init.body, headers: Object.fromEntries(init.headers.entries()) });
    const { status, body } = nextResponse;
    return { ok: status < 400, status, statusText: "x", text: async () => (body === undefined ? "" : JSON.stringify(body)) };
  };
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

test("edgeQuery encodes both node ids as query parameters", () => {
  assert.equal(edgeQuery("a b", "c&d"), "from_node_id=a%20b&to_node_id=c%26d");
});

test("connecting sends the ids as QUERY parameters and NO body (a body would be read as a condition)", async () => {
  await workflowApi.addEdge("wf1", 2, "n1", "n2");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].path, "/workflows/wf1/versions/2/edges?from_node_id=n1&to_node_id=n2");
  assert.equal(calls[0].body, undefined);
  assert.equal(calls[0].headers["content-type"], undefined);
});

test("patching a node sends only the new config", async () => {
  await workflowApi.patchNode("wf1", 1, "n1", { approval_group: "owners" });
  assert.equal(calls[0].method, "PATCH");
  assert.equal(calls[0].path, "/workflows/wf1/versions/1/nodes/n1");
  assert.deepEqual(JSON.parse(calls[0].body), { config: { approval_group: "owners" } });
});

test("deleting a node or an edge uses DELETE with no body", async () => {
  await workflowApi.deleteNode("wf1", 1, "n1");
  await workflowApi.deleteEdge("wf1", 1, "e1");
  assert.deepEqual(calls.map((c) => [c.method, c.path, c.body]), [
    ["DELETE", "/workflows/wf1/versions/1/nodes/n1", undefined],
    ["DELETE", "/workflows/wf1/versions/1/edges/e1", undefined],
  ]);
});

test("validate, publish and clone are POSTs to the version's own routes", async () => {
  await workflowApi.validate("wf1", 3);
  await workflowApi.publish("wf1", 3);
  await workflowApi.cloneVersion("wf1", 3);
  assert.deepEqual(
    calls.map((c) => [c.method, c.path]),
    [
      ["POST", "/workflows/wf1/versions/3/validate"],
      ["POST", "/workflows/wf1/versions/3/publish"],
      ["POST", "/workflows/wf1/versions/3/clone"],
    ]
  );
});

test("adding a node posts the exact node payload", async () => {
  await workflowApi.addNode("wf1", 1, { node_key: "gate", node_type: "human_approval", config: { approval_group: "owners" } });
  assert.deepEqual(JSON.parse(calls[0].body), { node_key: "gate", node_type: "human_approval", config: { approval_group: "owners" } });
});

test("the assignment becomes a Task in workflow mode -- never a standalone agent run", async () => {
  await workflowApi.createTask({ projectId: "p1", title: "Ship it", description: "  details " });
  assert.equal(calls[0].path, "/tasks");
  assert.deepEqual(JSON.parse(calls[0].body), {
    project_id: "p1",
    title: "Ship it",
    description: "  details ",
    execution_mode: "workflow",
  });
  await workflowApi.createTask({ projectId: "p1", title: "No description" });
  assert.equal(JSON.parse(calls[1].body).description, null);
  assert.ok(!calls.some((c) => c.path.includes("/tasks/") && c.path.endsWith("/runs"))); // /tasks/{id}/runs launches an agent
});

test("starting a run sends task_id (and only task_id) to the selected version, with an Idempotency-Key", async () => {
  await workflowApi.startRun("wf1", 2, "task-9", "key-123");
  assert.equal(calls[0].path, "/workflows/wf1/versions/2/runs");
  assert.deepEqual(JSON.parse(calls[0].body), { task_id: "task-9" });
  assert.equal(calls[0].headers["idempotency-key"], "key-123");
  await workflowApi.startRun("wf1", 2, "task-9");
  assert.equal(calls[1].headers["idempotency-key"], undefined);
});

test("every call carries the Bearer token header and ids are URL-encoded", async () => {
  await workflowApi.getGraph("a/b", 1);
  assert.equal(calls[0].path, "/workflows/a%2Fb/versions/1/graph");
  assert.ok("authorization" in calls[0].headers);
});

test("a structured validation error reaches the caller as an ApiError with detail.issues", async () => {
  nextResponse = {
    status: 400,
    body: { error: { code: "workflow_validation_failed", message: "Workflow validation failed (1 issue).", detail: { issues: ["x"] } } },
  };
  await assert.rejects(workflowApi.publish("wf1", 1), (err) => {
    assert.equal(err.status, 400);
    assert.equal(err.code, "workflow_validation_failed");
    assert.deepEqual(err.detail, { issues: ["x"] });
    return true;
  });
});

test("idempotency keys are non-empty and distinct", () => {
  const keys = new Set(Array.from({ length: 50 }, () => newIdempotencyKey()));
  assert.equal(keys.size, 50);
  for (const key of keys) assert.ok(key.length >= 8);
});

// -- project selection ----------------------------------------------------------------------------------

const projects = [{ id: "p1", name: "One" }, { id: "p2", name: "Two" }];

test("with several projects nothing is assumed: no stored choice means the user must choose", () => {
  assert.equal(resolveSelectedProject(projects, null), null);
  assert.equal(resolveSelectedProject(projects, "unknown-project"), null);
});

test("a stored choice is honoured only while it is still an accessible project", () => {
  assert.equal(resolveSelectedProject(projects, "p2").id, "p2");
  assert.equal(resolveSelectedProject(projects, "revoked"), null);
});

test("a single project is selected -- visibly, not by silently taking index 0 of many", () => {
  assert.equal(resolveSelectedProject([projects[0]], null).id, "p1");
  assert.equal(resolveSelectedProject([], null), null);
  assert.equal(resolveSelectedProject(null, "p1"), null);
});

test("the stored project id round-trips and tolerates broken storage", () => {
  const data = new Map();
  const storage = { getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), removeItem: (k) => data.delete(k) };
  assert.equal(readStoredProjectId(storage), null);
  writeStoredProjectId("p2", storage);
  assert.equal(data.get(SELECTED_PROJECT_KEY), "p2");
  assert.equal(readStoredProjectId(storage), "p2");
  writeStoredProjectId(null, storage);
  assert.equal(readStoredProjectId(storage), null);
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } };
  assert.equal(readStoredProjectId(broken), null);
  assert.doesNotThrow(() => writeStoredProjectId("p1", broken));
  assert.equal(readStoredProjectId(null), null);
});
