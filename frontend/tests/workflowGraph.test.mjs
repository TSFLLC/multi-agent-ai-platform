import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_APPROVAL_LABEL,
  NODE_TYPES,
  PALETTE,
  agentConfig,
  applyPositionOverrides,
  approvalConfig,
  buildDefinitionIndex,
  buildNodePayload,
  canCloneVersion,
  canRunVersion,
  clearPositions,
  connectionProblem,
  defaultConfigFor,
  evaluationConfig,
  exampleDag,
  hasUnsupportedNodes,
  isEditableVersion,
  isSupportedType,
  loadPositions,
  manualModelOverride,
  nodeHint,
  normalizeGraph,
  overrideModelId,
  pickInitialVersion,
  positionsKey,
  pruneOverrides,
  runBlockReason,
  savePositions,
  slugKey,
  statusLabel,
  typeLabel,
  uniqueNodeKey,
  unsupportedApprovalKeys,
  unsupportedNodes,
  versionHasUsableModel,
  versionLabel,
} from "../assets/js/workflowGraph.js";

const memoryStorage = () => {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
    data,
  };
};

// -- palette / supported types -------------------------------------------------------------------

test("the palette exposes exactly Agent, Evaluation, Human Approval and Complete", () => {
  assert.deepEqual(
    PALETTE.map((entry) => [entry.label, entry.type]),
    [
      ["Agent", "agent"],
      ["Evaluation", "evaluation"],
      ["Human Approval", "human_approval"],
      ["Complete", "terminal"],
    ]
  );
});

test("the unsupported node types are never in the palette and are reported unsupported", () => {
  for (const type of ["judge", "parallel_group", "consensus", "conditional", "repair_loop"]) {
    assert.equal(isSupportedType(type), false, type);
    assert.equal(PALETTE.some((entry) => entry.type === type), false, type);
    assert.match(typeLabel(type), /unsupported/);
  }
  for (const type of Object.values(NODE_TYPES)) assert.equal(isSupportedType(type), true, type);
});

// -- node keys -----------------------------------------------------------------------------------

test("slugKey makes a readable, safe key", () => {
  assert.equal(slugKey("Software Engineer"), "software_engineer");
  assert.equal(slugKey("  Eval / Security!! "), "eval_security");
  assert.equal(slugKey(""), "node");
  assert.equal(slugKey("<img src=x onerror=alert(1)>"), "img_src_x_onerror_alert_1");
  assert.ok(slugKey("x".repeat(500)).length <= 120);
});

test("node keys are unique within a version and never reuse a taken one", () => {
  assert.equal(uniqueNodeKey("agent", []), "agent");
  assert.equal(uniqueNodeKey("agent", ["agent"]), "agent_2");
  assert.equal(uniqueNodeKey("agent", ["agent", "agent_2", "agent_3"]), "agent_4");
  assert.equal(uniqueNodeKey("Agent", ["agent"]), "agent_2");
  const generated = new Set();
  for (let i = 0; i < 20; i += 1) generated.add(uniqueNodeKey("agent", [...generated]));
  assert.equal(generated.size, 20);
});

test("a generated key stays within the backend's 120-character limit even when suffixed", () => {
  const long = "x".repeat(200);
  const key = uniqueNodeKey(long, [slugKey(long)]);
  assert.ok(key.length <= 120);
  assert.notEqual(key, slugKey(long));
});

// -- Agent / Model separation ----------------------------------------------------------------------

test("an Agent node's config starts as just the agent version -- no model unless one is chosen", () => {
  assert.deepEqual(agentConfig({}, { agentVersionId: "av-1" }), { agent_version_id: "av-1" });
});

test("choosing a model writes the exact manual override shape, under a different key than the Agent", () => {
  const config = agentConfig({ agent_version_id: "av-1" }, { modelProviderModelId: "pm-9" });
  assert.deepEqual(config, {
    agent_version_id: "av-1",
    model_policy_override: { mode: "manual", manual_provider_model_id: "pm-9" },
  });
  assert.deepEqual(manualModelOverride("pm-9"), { mode: "manual", manual_provider_model_id: "pm-9" });
  assert.equal(overrideModelId(config, "model_policy_override"), "pm-9");
});

test("Agent != Model: changing the Agent never touches the model, and changing the model never touches the Agent", () => {
  const start = agentConfig({}, { agentVersionId: "av-1", modelProviderModelId: "pm-1" });
  const newAgent = agentConfig(start, { agentVersionId: "av-2" });
  assert.equal(newAgent.agent_version_id, "av-2");
  assert.deepEqual(newAgent.model_policy_override, manualModelOverride("pm-1"));
  const newModel = agentConfig(start, { modelProviderModelId: "pm-2" });
  assert.equal(newModel.agent_version_id, "av-1");
  assert.deepEqual(newModel.model_policy_override, manualModelOverride("pm-2"));
});

test("'Use Agent default' removes only the override and keeps the Agent", () => {
  const start = agentConfig({}, { agentVersionId: "av-1", modelProviderModelId: "pm-1" });
  assert.deepEqual(agentConfig(start, { modelProviderModelId: null }), { agent_version_id: "av-1" });
});

test("config builders never mutate their input and keep unrelated keys", () => {
  const existing = Object.freeze({ agent_version_id: "av-1", something_else: 7 });
  const next = agentConfig(existing, { modelProviderModelId: "pm-1" });
  assert.equal(next.something_else, 7);
  assert.deepEqual(existing, { agent_version_id: "av-1", something_else: 7 });
});

test("MA8 auto routing is never produced by the builders", () => {
  const text = JSON.stringify([agentConfig({}, { agentVersionId: "a", modelProviderModelId: "m" }), evaluationConfig({}, { evaluatorModelProviderModelId: "m" })]);
  assert.ok(!text.includes("auto"));
});

// -- Evaluation ------------------------------------------------------------------------------------

test("an Evaluation node's config is exactly the MA7.5A contract", () => {
  const config = evaluationConfig({}, { definitionVersionId: "dv-1", evaluatorAgentVersionId: "ev-1" });
  assert.deepEqual(config, { evaluation_definition_version_id: "dv-1", evaluator_agent_version_id: "ev-1" });
  assert.ok(!("evaluator_model_policy_override" in config)); // the override is optional
});

test("the evaluator model override is optional, exact, and removable", () => {
  const base = evaluationConfig({}, { definitionVersionId: "dv-1", evaluatorAgentVersionId: "ev-1" });
  const withModel = evaluationConfig(base, { evaluatorModelProviderModelId: "pm-3" });
  assert.deepEqual(withModel.evaluator_model_policy_override, { mode: "manual", manual_provider_model_id: "pm-3" });
  assert.equal(overrideModelId(withModel, "evaluator_model_policy_override"), "pm-3");
  assert.deepEqual(evaluationConfig(withModel, { evaluatorModelProviderModelId: null }), base);
});

test("an Evaluation config never carries a score, judge, threshold or decision key", () => {
  const config = evaluationConfig({}, { definitionVersionId: "d", evaluatorAgentVersionId: "e", evaluatorModelProviderModelId: "m" });
  const banned = ["score", "judge", "threshold", "winner", "rank", "auto_approve", "decision_rule", "method", "agent_version_id"];
  for (const key of banned) assert.ok(!(key in config), key);
  assert.deepEqual(Object.keys(config).sort(), [
    "evaluation_definition_version_id",
    "evaluator_agent_version_id",
    "evaluator_model_policy_override",
  ]);
});

// -- Human Approval --------------------------------------------------------------------------------

test("a Human Approval's config is only its label, trimmed", () => {
  assert.deepEqual(approvalConfig("  eng-leads "), { approval_group: "eng-leads" });
  assert.deepEqual(approvalConfig(null), { approval_group: "" });
  assert.deepEqual(defaultConfigFor(NODE_TYPES.APPROVAL), { approval_group: DEFAULT_APPROVAL_LABEL });
});

test("no automatic approval, timeout, AI approver, model or threshold key can be produced", () => {
  const keys = Object.keys(approvalConfig("x"));
  assert.deepEqual(keys, ["approval_group"]);
});

test("unsupported approval settings on an API-created node are reported, not preserved silently", () => {
  assert.deepEqual(unsupportedApprovalKeys({ approval_group: "x", auto_approve: true, timeout_seconds: 5 }), ["auto_approve", "timeout_seconds"]);
  assert.deepEqual(unsupportedApprovalKeys({ approval_group: "x" }), []);
});

test("Complete carries no configuration and other types start empty", () => {
  assert.equal(defaultConfigFor(NODE_TYPES.TERMINAL), undefined);
  assert.deepEqual(defaultConfigFor(NODE_TYPES.AGENT), {});
  assert.deepEqual(defaultConfigFor(NODE_TYPES.EVALUATION), {});
});

test("buildNodePayload is the exact POST /nodes body and omits config for Complete", () => {
  assert.deepEqual(buildNodePayload({ key: "done", type: "terminal", config: undefined }), { node_key: "done", node_type: "terminal" });
  assert.deepEqual(buildNodePayload({ key: "a", type: "agent", config: { agent_version_id: "x" } }), {
    node_key: "a",
    node_type: "agent",
    config: { agent_version_id: "x" },
  });
});

// -- model-required hint ---------------------------------------------------------------------------

const version = (over = {}) => ({ id: "av-1", status: "active", model_policy: null, ...over });
const ctxWith = (v) => ({ versionIndex: new Map([[v.id, { agent: { name: "A" }, version: v }]]), definitionIndex: new Map() });

test("versionHasUsableModel: manual needs a provider model, auto needs a policy, nothing else counts", () => {
  assert.equal(versionHasUsableModel(version({ model_policy: { mode: "manual", manual_provider_model_id: "pm" } })), true);
  assert.equal(versionHasUsableModel(version({ model_policy: { manual_provider_model_id: "pm" } })), true); // manual is the default mode
  assert.equal(versionHasUsableModel(version({ model_policy: { mode: "auto", auto_policy: "prefer_free" } })), true);
  assert.equal(versionHasUsableModel(version({ model_policy: { mode: "manual" } })), false);
  assert.equal(versionHasUsableModel(version({ model_policy: { mode: "auto" } })), false);
  assert.equal(versionHasUsableModel(version({ model_policy: {} })), false);
  assert.equal(versionHasUsableModel(version()), false);
  assert.equal(versionHasUsableModel(null), false);
});

test("an Agent node with no Agent, or no model, is told what to do", () => {
  const v = version();
  assert.equal(nodeHint({ type: "agent", config: {} }, ctxWith(v)), "Choose an Agent.");
  assert.match(nodeHint({ type: "agent", config: { agent_version_id: "av-1" } }, ctxWith(v)), /Choose a model/);
  assert.match(nodeHint({ type: "agent", config: { agent_version_id: "missing" } }, ctxWith(v)), /not found/);
  assert.match(nodeHint({ type: "agent", config: { agent_version_id: "av-1" } }, ctxWith(version({ status: "draft" }))), /not active/);
});

test("the model-required hint clears with an explicit override OR with the Agent's own default", () => {
  const withOverride = { type: "agent", config: { agent_version_id: "av-1", model_policy_override: manualModelOverride("pm") } };
  assert.equal(nodeHint(withOverride, ctxWith(version())), null);
  const own = version({ model_policy: { mode: "manual", manual_provider_model_id: "pm" } });
  assert.equal(nodeHint({ type: "agent", config: { agent_version_id: "av-1" } }, ctxWith(own)), null);
});

test("an Evaluation node walks the user through definition, evaluator and evaluator model", () => {
  const evaluator = version({ id: "ev-1" });
  const definitions = buildDefinitionIndex([
    { definition: { id: "d" }, versions: [{ id: "dv-1", status: "active" }, { id: "dv-0", status: "deprecated" }] },
  ]);
  const ctx = { versionIndex: new Map([["ev-1", { agent: {}, version: evaluator }]]), definitionIndex: definitions };
  assert.equal(nodeHint({ type: "evaluation", config: {} }, ctx), "Choose an Evaluation Definition.");
  assert.match(nodeHint({ type: "evaluation", config: { evaluation_definition_version_id: "dv-0" } }, ctx), /not active/);
  assert.equal(nodeHint({ type: "evaluation", config: { evaluation_definition_version_id: "dv-1" } }, ctx), "Choose an Evaluator Agent.");
  const chosen = { evaluation_definition_version_id: "dv-1", evaluator_agent_version_id: "ev-1" };
  assert.match(nodeHint({ type: "evaluation", config: chosen }, ctx), /evaluator model/);
  const done = { ...chosen, evaluator_model_policy_override: manualModelOverride("pm") };
  assert.equal(nodeHint({ type: "evaluation", config: done }, ctx), null);
});

test("an approval with an empty label is prompted; Complete never is", () => {
  assert.equal(nodeHint({ type: "human_approval", config: { approval_group: "  " } }, {}), "Enter an approval label.");
  assert.equal(nodeHint({ type: "human_approval", config: { approval_group: "owners" } }, {}), null);
  assert.equal(nodeHint({ type: "terminal", config: {} }, {}), null);
});

// -- graph model / unsupported ----------------------------------------------------------------------

test("normalizeGraph tolerates missing arrays and null config", () => {
  assert.deepEqual(normalizeGraph(null), { version: undefined, status: undefined, nodes: [], edges: [] });
  const graph = normalizeGraph({
    version: 2,
    status: "draft",
    nodes: [{ id: "n1", key: "a", type: "agent", config: null, max_iterations: null }],
    edges: [{ id: "e1", from: "n1", to: "n2", condition: null }],
  });
  assert.deepEqual(graph.nodes[0], { id: "n1", key: "a", type: "agent", config: {}, maxIterations: null });
  assert.deepEqual(graph.edges[0], { id: "e1", from: "n1", to: "n2", condition: null });
});

test("unsupported node types are detected so the Studio can render them read-only and block Run", () => {
  const graph = normalizeGraph({
    nodes: [
      { id: "1", key: "a", type: "agent" },
      { id: "2", key: "j", type: "judge" },
      { id: "3", key: "r", type: "repair_loop", max_iterations: 2 },
      { id: "4", key: "t", type: "terminal" },
    ],
    edges: [],
  });
  assert.equal(hasUnsupportedNodes(graph), true);
  assert.deepEqual(unsupportedNodes(graph).map((n) => n.key), ["j", "r"]);
  assert.equal(hasUnsupportedNodes(normalizeGraph({ nodes: [{ id: "1", key: "a", type: "agent" }] })), false);
});

test("connectionProblem catches only what is locally obvious", () => {
  const graph = normalizeGraph({
    nodes: [{ id: "a", key: "a", type: "agent" }, { id: "b", key: "b", type: "agent" }],
    edges: [{ id: "e", from: "a", to: "b" }],
  });
  assert.match(connectionProblem(graph, "a", "a"), /itself/);
  assert.match(connectionProblem(graph, "a", "b"), /already connected/);
  assert.match(connectionProblem(graph, "a", "zzz"), /exist/);
  assert.match(connectionProblem(graph, "", "b"), /both ends/i);
  assert.equal(connectionProblem(graph, "b", "a"), null); // a cycle is the SERVER validator's to report
});

// -- versions / new-version handling -------------------------------------------------------------------

const versions = [
  { id: "v1", version: 1, status: "active" },
  { id: "v3", version: 3, status: "draft" },
  { id: "v2", version: 2, status: "active" },
];

test("labels and status names", () => {
  assert.equal(versionLabel(versions[1]), "v3 · DRAFT");
  assert.equal(statusLabel("active"), "ACTIVE");
  assert.equal(statusLabel("weird"), "WEIRD");
});

test("pickInitialVersion prefers the requested one, then the newest DRAFT, then the newest ACTIVE", () => {
  assert.equal(pickInitialVersion(versions, 2).id, "v2");
  assert.equal(pickInitialVersion(versions, "1").id, "v1");
  assert.equal(pickInitialVersion(versions, 99).id, "v3"); // unknown request falls back to the draft
  assert.equal(pickInitialVersion(versions.filter((v) => v.status === "active")).id, "v2");
  assert.equal(pickInitialVersion([]), null);
});

test("only a DRAFT is editable; anything else offers 'Edit as new version'", () => {
  assert.equal(isEditableVersion({ status: "draft" }), true);
  assert.equal(isEditableVersion({ status: "active" }), false);
  assert.equal(isEditableVersion(null), false);
  assert.equal(canCloneVersion({ status: "active" }), true);
  assert.equal(canCloneVersion({ status: "deprecated" }), true);
  assert.equal(canCloneVersion({ status: "draft" }), false);
});

test("Run is available only for an ACTIVE version with only supported nodes", () => {
  const ok = normalizeGraph({ nodes: [{ id: "1", key: "a", type: "agent" }, { id: "2", key: "t", type: "terminal" }] });
  const bad = normalizeGraph({ nodes: [{ id: "1", key: "a", type: "judge" }] });
  assert.equal(canRunVersion({ status: "active" }, ok), true);
  assert.equal(runBlockReason({ status: "draft" }, ok), "Only a published (ACTIVE) version can be run.");
  assert.match(runBlockReason({ status: "active" }, bad), /does not support/);
  assert.match(runBlockReason({ status: "active" }, normalizeGraph({})), /no nodes/);
  assert.match(runBlockReason(null, ok), /No version/);
});

// -- example DAG ------------------------------------------------------------------------------------------

test("the example DAG matches the documented shape", () => {
  const { nodes, edges } = exampleDag();
  assert.equal(nodes.length, 10);
  assert.deepEqual(
    nodes.map((n) => n.type),
    ["agent", "agent", "agent", "agent", "agent", "evaluation", "evaluation", "evaluation", "human_approval", "terminal"]
  );
  assert.equal(new Set(nodes.map((n) => n.key)).size, 10); // unique keys
  assert.equal(edges.length, 14);
  const into = (key) => edges.filter(([, to]) => to === key).map(([from]) => from).sort();
  assert.deepEqual(into("approval"), ["code", "eval_code", "eval_security", "eval_test", "security", "test"]); // six parents
  for (const reviewer of ["test", "security", "code"]) assert.deepEqual(into(`eval_${reviewer}`), [reviewer]); // one AGENT parent each
  assert.deepEqual(into("complete"), ["approval"]);
  assert.deepEqual(into("engineer"), ["planner"]);
});

test("the example DAG configures nothing for the user", () => {
  for (const node of exampleDag().nodes) assert.ok(!("config" in node));
});

// -- local positions --------------------------------------------------------------------------------------

test("positions round-trip through storage keyed by WorkflowVersion id", () => {
  const storage = memoryStorage();
  savePositions("ver-1", { a: { x: 10, y: 20 } }, storage);
  assert.ok(storage.data.has(positionsKey("ver-1")));
  assert.deepEqual(loadPositions("ver-1", storage), { a: { x: 10, y: 20 } });
  assert.deepEqual(loadPositions("ver-2", storage), {}); // another version never sees them
  clearPositions("ver-1", storage);
  assert.deepEqual(loadPositions("ver-1", storage), {});
});

test("corrupt or hostile stored positions are ignored", () => {
  const storage = memoryStorage();
  storage.setItem(positionsKey("v"), "{not json");
  assert.deepEqual(loadPositions("v", storage), {});
  storage.setItem(positionsKey("v"), JSON.stringify({ a: { x: "1", y: 2 }, b: { x: 3, y: 4 }, c: null, d: { x: NaN, y: 1 } }));
  assert.deepEqual(loadPositions("v", storage), { b: { x: 3, y: 4 } });
  assert.deepEqual(loadPositions("v", null), {});
  assert.doesNotThrow(() => savePositions("v", {}, null));
  assert.doesNotThrow(() => savePositions("v", {}, { setItem() { throw new Error("quota"); } }));
});

test("dragged positions override the automatic layout only for existing nodes", () => {
  const layout = { a: { x: 1, y: 1, layer: 0, order: 0 }, b: { x: 2, y: 2, layer: 1, order: 0 } };
  const merged = applyPositionOverrides(layout, { a: { x: 99, y: 88 }, gone: { x: 5, y: 5 } }, ["a", "b"]);
  assert.deepEqual(merged.a, { x: 99, y: 88, layer: 0, order: 0 });
  assert.deepEqual(merged.b, layout.b);
  assert.ok(!("gone" in merged));
  assert.deepEqual(pruneOverrides({ a: { x: 1, y: 1 }, gone: { x: 2, y: 2 } }, ["a"]), { a: { x: 1, y: 1 } });
});

// -- node card content ----------------------------------------------------------------------------------

import { describeNode } from "../assets/js/workflowGraph.js";

const cardCtx = () => {
  const own = { id: "av-own", version: 3, status: "active", model_policy: { mode: "manual", manual_provider_model_id: "pm-x" } };
  const bare = { id: "av-bare", version: 1, status: "active", model_policy: null };
  return {
    versionIndex: new Map([
      ["av-own", { agent: { name: "Software Engineer" }, version: own }],
      ["av-bare", { agent: { name: "Planner" }, version: bare }],
    ]),
    definitionIndex: new Map([["dv-1", { definition: { name: "Correctness Rubric" }, version: { id: "dv-1", version: 2, status: "active" } }]]),
    modelsById: new Map([["pm-1", { canonical_model_id: "google/gemini-x" }]]),
  };
};

test("an Agent card names the Agent and the Model on SEPARATE lines", () => {
  const card = describeNode({ key: "planner", type: "agent", config: { agent_version_id: "av-bare", model_policy_override: manualModelOverride("pm-1") } }, cardCtx());
  assert.equal(card.title, "planner");
  assert.equal(card.tag, "Agent");
  assert.deepEqual(card.lines, ["Agent: Planner v1", "Model: google/gemini-x"]);
});

test("a card shows 'Agent default' only when the Agent really has a default model", () => {
  const own = describeNode({ key: "eng", type: "agent", config: { agent_version_id: "av-own" } }, cardCtx());
  assert.deepEqual(own.lines, ["Agent: Software Engineer v3", "Model: Agent default"]);
  const bare = describeNode({ key: "p", type: "agent", config: { agent_version_id: "av-bare" } }, cardCtx());
  assert.deepEqual(bare.lines, ["Agent: Planner v1", "No model chosen"]);
  assert.deepEqual(describeNode({ key: "n", type: "agent", config: {} }, cardCtx()).lines, ["No Agent chosen", "No model chosen"]);
});

test("an Evaluation card shows its rubric and evaluator, and never a score or verdict", () => {
  const card = describeNode(
    { key: "eval_x", type: "evaluation", config: { evaluation_definition_version_id: "dv-1", evaluator_agent_version_id: "av-own" } },
    cardCtx()
  );
  assert.equal(card.tag, "Evaluation");
  assert.deepEqual(card.lines, ["Rubric: Correctness Rubric v2", "Evaluator: Software Engineer · Evaluator default"]);
  assert.ok(!/score|winner|rank|pass|fail/i.test(card.lines.join(" ")));
});

test("an approval card shows its label and that a person decides; Complete is plain", () => {
  assert.deepEqual(describeNode({ key: "gate", type: "human_approval", config: { approval_group: "owners" } }, {}).lines, ["Approval label: owners", "A person decides"]);
  assert.deepEqual(describeNode({ key: "gate", type: "human_approval", config: {} }, {}).lines, ["No approval label", "A person decides"]);
  assert.equal(describeNode({ key: "done", type: "terminal", config: {} }, {}).tag, "Complete");
});

test("an unsupported node card says so and is flagged", () => {
  const card = describeNode({ key: "j", type: "judge", config: { judge_agent_version_id: "x" } }, cardCtx());
  assert.equal(card.unsupported, true);
  assert.match(card.tag, /unsupported/);
  assert.deepEqual(card.lines, ["Not supported by this Studio", "Shown read-only"]);
});

test("hostile node keys are carried as plain data for the DOM layer to render as text", () => {
  const card = describeNode({ key: '<img src=x onerror="alert(1)">', type: "terminal", config: {} }, {});
  assert.equal(card.title, '<img src=x onerror="alert(1)">');
});
