// Workflow Studio domain logic (MA7.6A) -- pure, DOM-free, unit-tested directly
// (frontend/tests/workflowGraph.test.mjs).
//
// Product model this file encodes (the workforce analogy is a UX mental model,
// never a domain rename):
//   Agent      = an AI team member/role       -> config.agent_version_id
//   Model      = interchangeable intelligence -> config.model_policy_override
//   Evaluation = structured quality evidence  -> the MA7.5A evaluation config
//   Human Approval = an owner/manager decision -> config.approval_group (a label)
// Agent != Model: the two are chosen independently, stored under different
// keys, and changing one never touches the other.
//
// Nothing here invents configuration the backend does not accept, and nothing
// re-implements the server's validation rules: structural validity (cycles,
// entry/terminal, fan-out, config completeness) is the server's dry-run
// validator's job. The only "hints" below are cheap, obviously-local prompts
// ("choose a model") that stop a user staring at a red validation list.

export const NODE_TYPES = {
  AGENT: "agent",
  EVALUATION: "evaluation",
  APPROVAL: "human_approval",
  TERMINAL: "terminal",
};

// The authoring palette exposes ONLY these four. JUDGE, PARALLEL_GROUP,
// CONSENSUS, CONDITIONAL and REPAIR_LOOP exist in the backend enum but are
// not runnable, so the Studio never offers them.
export const PALETTE = [
  { type: NODE_TYPES.AGENT, label: "Agent", baseKey: "agent", hint: "An AI team member who does a piece of the work." },
  {
    type: NODE_TYPES.EVALUATION,
    label: "Evaluation",
    baseKey: "evaluation",
    hint: "Structured quality evidence about one Agent's output. It never decides anything.",
  },
  { type: NODE_TYPES.APPROVAL, label: "Human Approval", baseKey: "approval", hint: "A person decides. Never automatic." },
  { type: NODE_TYPES.TERMINAL, label: "Complete", baseKey: "complete", hint: "Where the workflow finishes." },
];

export const SUPPORTED_TYPES = new Set(PALETTE.map((entry) => entry.type));
export const DEFAULT_APPROVAL_LABEL = "owners";
export const MAX_NODE_KEY_LENGTH = 120;

export function isSupportedType(type) {
  return SUPPORTED_TYPES.has(type);
}

export function typeLabel(type) {
  const entry = PALETTE.find((candidate) => candidate.type === type);
  return entry ? entry.label : `${String(type || "unknown").replace(/_/g, " ")} (unsupported)`;
}

// -- node keys ---------------------------------------------------------------------------------

export function slugKey(text) {
  const slug = String(text || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return slug.slice(0, MAX_NODE_KEY_LENGTH - 6) || "node";
}

// A key not already used: "agent", then "agent_2", "agent_3", ... Keys are
// permanent once a node exists (and order the approval evidence), so a
// readable, stable one is generated rather than an opaque id.
export function uniqueNodeKey(base, existingKeys) {
  const taken = new Set(existingKeys || []);
  const root = slugKey(base);
  if (!taken.has(root)) return root;
  for (let n = 2; ; n += 1) {
    const candidate = `${root}_${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

export function isKeyTaken(key, existingKeys) {
  return (existingKeys || []).includes(key);
}

// -- config builders ---------------------------------------------------------------------------
// Each takes the node's EXISTING config and returns a new object: only the keys
// this control owns are touched, so an option the user did not change (and any
// key created through the API) is preserved. PATCH replaces the whole config,
// so the caller sends exactly what these return.

export function manualModelOverride(providerModelId) {
  return { mode: "manual", manual_provider_model_id: providerModelId };
}

function withKey(config, key, value, present) {
  const next = { ...(config || {}) };
  if (present) next[key] = value;
  else delete next[key];
  return next;
}

// changes: { agentVersionId?, modelProviderModelId? }. `undefined` leaves a key
// untouched; `null` removes it ("Use Agent default" removes the override).
export function agentConfig(existing, changes = {}) {
  let next = { ...(existing || {}) };
  if ("agentVersionId" in changes && changes.agentVersionId !== undefined) {
    next = withKey(next, "agent_version_id", changes.agentVersionId, Boolean(changes.agentVersionId));
  }
  if ("modelProviderModelId" in changes && changes.modelProviderModelId !== undefined) {
    next = withKey(next, "model_policy_override", manualModelOverride(changes.modelProviderModelId), Boolean(changes.modelProviderModelId));
  }
  return next;
}

// changes: { definitionVersionId?, evaluatorAgentVersionId?, evaluatorModelProviderModelId? }
export function evaluationConfig(existing, changes = {}) {
  let next = { ...(existing || {}) };
  if (changes.definitionVersionId !== undefined) {
    next = withKey(next, "evaluation_definition_version_id", changes.definitionVersionId, Boolean(changes.definitionVersionId));
  }
  if (changes.evaluatorAgentVersionId !== undefined) {
    next = withKey(next, "evaluator_agent_version_id", changes.evaluatorAgentVersionId, Boolean(changes.evaluatorAgentVersionId));
  }
  if (changes.evaluatorModelProviderModelId !== undefined) {
    next = withKey(
      next,
      "evaluator_model_policy_override",
      manualModelOverride(changes.evaluatorModelProviderModelId),
      Boolean(changes.evaluatorModelProviderModelId)
    );
  }
  return next;
}

// A Human Approval's configuration is exactly its label -- nothing else is ever
// offered or written (no automatic approval, timeout, AI approver, model or
// threshold).
export function approvalConfig(label) {
  return { approval_group: String(label == null ? "" : label).trim() };
}

const APPROVAL_OWN_KEYS = new Set(["approval_group"]);

// Keys on an existing approval node that the Studio does not support (created
// through the API). Saving the label replaces them, so the UI says so.
export function unsupportedApprovalKeys(config) {
  return Object.keys(config || {}).filter((key) => !APPROVAL_OWN_KEYS.has(key)).sort();
}

export function defaultConfigFor(type) {
  if (type === NODE_TYPES.APPROVAL) return approvalConfig(DEFAULT_APPROVAL_LABEL);
  if (type === NODE_TYPES.AGENT || type === NODE_TYPES.EVALUATION) return {};
  return undefined; // Complete carries no configuration
}

// The POST /nodes body.
export function buildNodePayload({ key, type, config }) {
  const body = { node_key: key, node_type: type };
  if (config !== undefined) body.config = config;
  return body;
}

// -- graph model -------------------------------------------------------------------------------

export function normalizeGraph(apiGraph) {
  const raw = apiGraph || {};
  const nodes = (Array.isArray(raw.nodes) ? raw.nodes : []).map((node) => ({
    id: node.id,
    key: node.key,
    type: node.type,
    config: node.config && typeof node.config === "object" ? node.config : {},
    maxIterations: node.max_iterations == null ? null : node.max_iterations,
  }));
  const edges = (Array.isArray(raw.edges) ? raw.edges : []).map((edge) => ({
    id: edge.id,
    from: edge.from,
    to: edge.to,
    condition: edge.condition == null ? null : edge.condition,
  }));
  return { version: raw.version, status: raw.status, nodes, edges };
}

export function unsupportedNodes(graph) {
  return ((graph && graph.nodes) || []).filter((node) => !isSupportedType(node.type));
}

export function hasUnsupportedNodes(graph) {
  return unsupportedNodes(graph).length > 0;
}

export function incomingEdges(graph, nodeId) {
  return graph.edges.filter((edge) => edge.to === nodeId);
}

export function outgoingEdges(graph, nodeId) {
  return graph.edges.filter((edge) => edge.from === nodeId);
}

// Cheap local checks before a request that the server would refuse anyway
// (400). Cycles, entry/terminal and per-type rules are the server's to judge.
export function connectionProblem(graph, fromId, toId) {
  if (!fromId || !toId) return "Choose both ends of the connection.";
  if (fromId === toId) return "A node cannot connect to itself.";
  const nodeIds = new Set(graph.nodes.map((node) => node.id));
  if (!nodeIds.has(fromId) || !nodeIds.has(toId)) return "Both nodes must exist in this version.";
  if (graph.edges.some((edge) => edge.from === fromId && edge.to === toId)) return "Those nodes are already connected.";
  return null;
}

// -- versions ----------------------------------------------------------------------------------

export const STATUS_LABELS = {
  draft: "DRAFT",
  active: "ACTIVE",
  deprecated: "DEPRECATED",
  retired: "RETIRED",
};

export function statusLabel(status) {
  return STATUS_LABELS[status] || String(status || "").toUpperCase();
}

export function versionLabel(version) {
  return `v${version.version} · ${statusLabel(version.status)}`;
}

export function sortVersionsNewestFirst(versions) {
  return [...(versions || [])].sort((a, b) => b.version - a.version);
}

// Which version to open: the requested one if it exists, else the newest DRAFT
// (work in progress), else the newest ACTIVE, else the newest of anything.
export function pickInitialVersion(versions, requestedNumber) {
  const ordered = sortVersionsNewestFirst(versions);
  if (!ordered.length) return null;
  if (requestedNumber != null) {
    const wanted = ordered.find((version) => version.version === Number(requestedNumber));
    if (wanted) return wanted;
  }
  return (
    ordered.find((version) => version.status === "draft") ||
    ordered.find((version) => version.status === "active") ||
    ordered[0]
  );
}

export function isEditableVersion(version) {
  return Boolean(version) && version.status === "draft";
}

// "Edit as new version" is offered for anything that is not a DRAFT.
export function canCloneVersion(version) {
  return Boolean(version) && version.status !== "draft";
}

// Why Run is unavailable, or null when the selected version can be run.
export function runBlockReason(version, graph) {
  if (!version) return "No version selected.";
  if (version.status !== "active") return "Only a published (ACTIVE) version can be run.";
  if (!graph || !graph.nodes.length) return "This version has no nodes.";
  if (hasUnsupportedNodes(graph)) {
    return "This workflow contains node types the Studio does not support, so it cannot be run from here.";
  }
  return null;
}

export function canRunVersion(version, graph) {
  return runBlockReason(version, graph) === null;
}

// -- models / hints ----------------------------------------------------------------------------

// Whether an Agent Version already carries a usable model policy of its own
// ("Use Agent default"). Mirrors what the runtime can resolve, no more: a manual
// policy needs a provider model id, an auto policy needs its policy name.
export function versionHasUsableModel(version) {
  const policy = version && version.model_policy;
  if (!policy || typeof policy !== "object") return false;
  const mode = policy.mode || "manual";
  if (mode === "manual") return Boolean(policy.manual_provider_model_id);
  if (mode === "auto") return Boolean(policy.auto_policy);
  return false;
}

export function overrideModelId(config, key) {
  const override = config && config[key];
  return override && override.mode === "manual" && override.manual_provider_model_id ? override.manual_provider_model_id : null;
}

// versionIndex: Map(agent_version_id -> { agent, version })  (agentDirectory.buildVersionIndex)
// definitionIndex: Map(evaluation_definition_version_id -> { definition, version })
export function buildDefinitionIndex(catalog) {
  const index = new Map();
  for (const entry of catalog || []) {
    for (const version of entry.versions || []) index.set(version.id, { definition: entry.definition, version });
  }
  return index;
}

// The one-line prompt shown on a node that still needs something from the user,
// or null. These are hints, not validation: the server's validator is the
// authority and is what Validate and Publish call.
export function nodeHint(node, ctx) {
  const config = node.config || {};
  const versionIndex = (ctx && ctx.versionIndex) || new Map();
  const definitionIndex = (ctx && ctx.definitionIndex) || new Map();
  if (node.type === NODE_TYPES.AGENT) {
    const versionId = config.agent_version_id;
    if (!versionId) return "Choose an Agent.";
    const resolved = versionIndex.get(versionId);
    if (!resolved) return "The chosen Agent version was not found in this project.";
    if (resolved.version.status !== "active") return "The chosen Agent version is not active.";
    if (!overrideModelId(config, "model_policy_override") && !versionHasUsableModel(resolved.version)) {
      return "Choose a model. This Agent has no default model.";
    }
    return null;
  }
  if (node.type === NODE_TYPES.EVALUATION) {
    if (!config.evaluation_definition_version_id) return "Choose an Evaluation Definition.";
    const definition = definitionIndex.get(config.evaluation_definition_version_id);
    if (!definition) return "The chosen Evaluation Definition version was not found in this project.";
    if (definition.version.status !== "active") return "The chosen Evaluation Definition version is not active.";
    const evaluatorId = config.evaluator_agent_version_id;
    if (!evaluatorId) return "Choose an Evaluator Agent.";
    const evaluator = versionIndex.get(evaluatorId);
    if (!evaluator) return "The chosen Evaluator Agent version was not found in this project.";
    if (evaluator.version.status !== "active") return "The chosen Evaluator Agent version is not active.";
    if (!overrideModelId(config, "evaluator_model_policy_override") && !versionHasUsableModel(evaluator.version)) {
      return "Choose an evaluator model. This Evaluator Agent has no default model.";
    }
    return null;
  }
  if (node.type === NODE_TYPES.APPROVAL) {
    return String(config.approval_group || "").trim() ? null : "Enter an approval label.";
  }
  return null;
}

// -- example ----------------------------------------------------------------------------------

// The example DAG the Studio can drop into an empty draft:
//   Planner -> Engineer -> { Test, Security, Code } -> one Evaluation each
//   -> ONE Human Approval fed by all six -> Complete.
// Structure only: no Agent, model or evaluation is chosen for the user.
export function exampleDag() {
  const agent = NODE_TYPES.AGENT;
  const evaluation = NODE_TYPES.EVALUATION;
  const nodes = [
    { key: "planner", type: agent },
    { key: "engineer", type: agent },
    { key: "test", type: agent },
    { key: "security", type: agent },
    { key: "code", type: agent },
    { key: "eval_test", type: evaluation },
    { key: "eval_security", type: evaluation },
    { key: "eval_code", type: evaluation },
    { key: "approval", type: NODE_TYPES.APPROVAL },
    { key: "complete", type: NODE_TYPES.TERMINAL },
  ];
  const edges = [["planner", "engineer"]];
  for (const reviewer of ["test", "security", "code"]) {
    edges.push(["engineer", reviewer], [reviewer, `eval_${reviewer}`], [reviewer, "approval"], [`eval_${reviewer}`, "approval"]);
  }
  edges.push(["approval", "complete"]);
  return { nodes, edges };
}

// -- local (never persisted server-side) node positions ------------------------------------

export function positionsKey(versionId) {
  return `map.studio.positions.${versionId}`;
}

function defaultStorage() {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function loadPositions(versionId, storage = defaultStorage()) {
  try {
    const raw = storage && storage.getItem(positionsKey(versionId));
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    const clean = {};
    for (const [id, value] of Object.entries(parsed || {})) {
      if (value && Number.isFinite(value.x) && Number.isFinite(value.y)) clean[id] = { x: value.x, y: value.y };
    }
    return clean;
  } catch {
    return {};
  }
}

export function savePositions(versionId, positions, storage = defaultStorage()) {
  try {
    if (!storage) return;
    storage.setItem(positionsKey(versionId), JSON.stringify(positions || {}));
  } catch {
    // A convenience only: positions fall back to the automatic layout.
  }
}

export function clearPositions(versionId, storage = defaultStorage()) {
  try {
    if (storage) storage.removeItem(positionsKey(versionId));
  } catch {
    // ignore
  }
}

// Automatic layout positions, overridden by any locally dragged position for a
// node that still exists (overrides for deleted nodes are dropped).
export function applyPositionOverrides(layoutPositions, overrides, nodeIds) {
  const result = {};
  const ids = new Set(nodeIds);
  for (const id of ids) {
    const base = layoutPositions[id];
    if (!base) continue;
    const moved = overrides && overrides[id];
    result[id] = moved ? { ...base, x: moved.x, y: moved.y } : { ...base };
  }
  return result;
}

export function pruneOverrides(overrides, nodeIds) {
  const ids = new Set(nodeIds);
  const kept = {};
  for (const [id, value] of Object.entries(overrides || {})) if (ids.has(id)) kept[id] = value;
  return kept;
}

// -- node card content ------------------------------------------------------------------------

// What a node card shows. The card's title is always the node's own key (what
// validation messages name); the lines describe it in product terms. Agent and
// Model are separate lines -- an Agent card never blends the two.
//   ctx: { versionIndex, definitionIndex, modelsById: Map(provider_model id -> catalog entry) }
export function describeNode(node, ctx) {
  const config = node.config || {};
  const versionIndex = (ctx && ctx.versionIndex) || new Map();
  const definitionIndex = (ctx && ctx.definitionIndex) || new Map();
  const modelsById = (ctx && ctx.modelsById) || new Map();
  const base = { title: node.key, tag: typeLabel(node.type), lines: [], unsupported: !isSupportedType(node.type) };
  if (base.unsupported) {
    base.lines = ["Not supported by this Studio", "Shown read-only"];
    return base;
  }

  const modelLine = (overrideKey, resolvedVersion, defaultWord) => {
    const modelId = overrideModelId(config, overrideKey);
    if (modelId) {
      const model = modelsById.get(modelId);
      return `Model: ${model ? model.canonical_model_id : "(unknown model)"}`;
    }
    return versionHasUsableModel(resolvedVersion) ? `Model: ${defaultWord}` : "No model chosen";
  };

  if (node.type === NODE_TYPES.AGENT) {
    const resolved = versionIndex.get(config.agent_version_id);
    base.lines = resolved
      ? [`Agent: ${resolved.agent.name} v${resolved.version.version}`, modelLine("model_policy_override", resolved.version, "Agent default")]
      : ["No Agent chosen", "No model chosen"];
  } else if (node.type === NODE_TYPES.EVALUATION) {
    const definition = definitionIndex.get(config.evaluation_definition_version_id);
    const evaluator = versionIndex.get(config.evaluator_agent_version_id);
    base.lines = [
      definition ? `Rubric: ${definition.definition.name} v${definition.version.version}` : "No Evaluation Definition chosen",
      evaluator
        ? `Evaluator: ${evaluator.agent.name} · ${modelLine("evaluator_model_policy_override", evaluator.version, "Evaluator default").replace(/^Model: /, "")}`
        : "No Evaluator Agent chosen",
    ];
  } else if (node.type === NODE_TYPES.APPROVAL) {
    const label = String(config.approval_group || "").trim();
    base.lines = [label ? `Approval label: ${label}` : "No approval label", "A person decides"];
  } else {
    base.lines = ["The workflow finishes here"];
  }
  return base;
}
