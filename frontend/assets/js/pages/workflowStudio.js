// Workflow Studio (MA7.6A) -- see the team's workflow as a diagram, edit a DRAFT,
// validate, publish, edit an ACTIVE version as a new draft, and run an exact
// version from an assignment.
//
//   Project      = the workplace           Agent      = an AI team member/role
//   Model        = the intelligence an Agent uses (chosen independently)
//   Workflow     = how the team works      Evaluation = quality evidence
//   Human Approval = a person decides
//
// Persistence model: every edit is saved to the DRAFT version immediately
// through the existing workflow API (add/patch/delete node, add/delete edge); an
// ACTIVE version is immutable and read-only here. "Edit as new version" asks the
// server to copy a version atomically into a new DRAFT; the original is never
// touched. Node positions are a local convenience (localStorage, keyed by the
// version id) and are never sent to the server.

import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import { navigate } from "../router.js";
import { openModal, closeModal } from "../modal.js";
import { workflowApi, newIdempotencyKey } from "../workflowApi.js";
import { annotateIssues, flaggedNodeKeys, normalizeApiError, normalizeValidationResult } from "../workflowErrors.js";
import { buildVersionIndex, loadAgentCatalog } from "../agentDirectory.js";
import { loadEvaluationDefinitionCatalog } from "./evaluationConfig.js";
import { computeLayout } from "../dagLayout.js";
import { createDagCanvas } from "../dagCanvas.js";
import { buildEdgeInspector, buildInspector } from "../nodeInspector.js";
import { writeStoredProjectId } from "../projectSelection.js";
import { formatDateTime } from "../format.js";
import { buildVoiceControl } from "../voiceControl.js";
import { runStatusInfo, shortId, usageLine } from "../runState.js";
import {
  PALETTE,
  applyPositionOverrides,
  buildDefinitionIndex,
  buildNodePayload,
  canCloneVersion,
  clearPositions,
  connectionProblem,
  defaultConfigFor,
  describeNode,
  exampleDag,
  isEditableVersion,
  isSupportedType,
  loadPositions,
  nodeHint,
  normalizeGraph,
  pickInitialVersion,
  pruneOverrides,
  runBlockReason,
  savePositions,
  sortVersionsNewestFirst,
  statusLabel,
  typeLabel,
  uniqueNodeKey,
  unsupportedNodes,
  versionLabel,
} from "../workflowGraph.js";

// A notice to show on the next Studio page load (e.g. "Created v3 from v2" after
// the router re-renders for the new version).
let pendingNotice = null;

function versionFromHash() {
  const hash = window.location.hash || "";
  const query = hash.includes("?") ? hash.split("?")[1] : "";
  const value = new URLSearchParams(query).get("v");
  return value ? Number(value) : null;
}

function studioHash(workflowId, versionNumber) {
  return `#/workflows/${encodeURIComponent(workflowId)}${versionNumber != null ? `?v=${versionNumber}` : ""}`;
}

function newState(workflowId) {
  return {
    workflowId,
    workflow: null,
    projects: [],
    versions: [],
    version: null,
    graph: { nodes: [], edges: [] },
    agentCatalog: [],
    definitionCatalog: [],
    models: [],
    versionIndex: new Map(),
    definitionIndex: new Map(),
    modelsById: new Map(),
    selection: { kind: null, id: null },
    connectFromId: null,
    overrides: {},
    validation: null, // { valid, issues, annotated, stale }
    notice: pendingNotice,
    error: null,
    errorIssues: [],
    confirm: null, // { kind: "publish" }
    busy: null,
    history: { open: false, loaded: false, loading: false, error: null, runs: [] },
    ui: { picker: {}, promptCache: new Map(), modelMode: {}, confirmDeleteNodeId: null },
  };
}

// ---------------------------------------------------------------------------------------------------
// loading
// ---------------------------------------------------------------------------------------------------

async function loadCatalogs(s) {
  const projectId = s.workflow.project_id;
  const [agentCatalog, definitionCatalog, models] = await Promise.all([
    loadAgentCatalog(projectId),
    loadEvaluationDefinitionCatalog(projectId).catch(() => []),
    api.get("/models").catch(() => []),
  ]);
  s.agentCatalog = agentCatalog;
  s.definitionCatalog = definitionCatalog;
  s.models = models;
  s.versionIndex = buildVersionIndex(agentCatalog);
  s.definitionIndex = buildDefinitionIndex(definitionCatalog);
  s.modelsById = new Map(models.map((model) => [model.id, model]));
}

async function loadVersion(s, versionRecord) {
  s.version = versionRecord;
  s.graph = normalizeGraph(await workflowApi.getGraph(s.workflowId, versionRecord.version));
  s.overrides = pruneOverrides(loadPositions(versionRecord.id), s.graph.nodes.map((node) => node.id));
  s.selection = { kind: null, id: null };
  s.connectFromId = null;
  s.validation = null;
  s.confirm = null;
  s.ui.confirmDeleteNodeId = null;
}

async function loadAll(s) {
  const [workflow, projects, versions] = await Promise.all([
    workflowApi.getWorkflow(s.workflowId),
    workflowApi.listProjects().catch(() => []),
    workflowApi.listVersions(s.workflowId),
  ]);
  s.workflow = workflow;
  s.projects = projects;
  s.versions = sortVersionsNewestFirst(versions);
  await loadCatalogs(s);
  const initial = pickInitialVersion(s.versions, versionFromHash());
  if (initial) await loadVersion(s, initial);
}

async function refreshGraph(s) {
  s.graph = normalizeGraph(await workflowApi.getGraph(s.workflowId, s.version.version));
  const ids = new Set(s.graph.nodes.map((node) => node.id));
  if (s.selection.kind === "node" && !ids.has(s.selection.id)) s.selection = { kind: null, id: null };
  if (s.selection.kind === "edge" && !s.graph.edges.some((edge) => edge.id === s.selection.id)) s.selection = { kind: null, id: null };
  if (s.connectFromId && !ids.has(s.connectFromId)) s.connectFromId = null;
  const pruned = pruneOverrides(s.overrides, [...ids]);
  if (Object.keys(pruned).length !== Object.keys(s.overrides).length) {
    s.overrides = pruned;
    savePositions(s.version.id, s.overrides);
  }
  if (s.validation) s.validation.stale = true;
}

// ---------------------------------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------------------------------

export async function renderWorkflowStudio(root, params) {
  const s = newState(params.id);
  pendingNotice = null;
  mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Opening the Studio…"]));
  try {
    await loadAll(s);
  } catch (err) {
    mount(root, el("div", { class: "error-banner" }, normalizeApiError(err).message));
    return undefined;
  }
  const onKey = (event) => {
    if (event.key === "Escape" && s.connectFromId) {
      s.connectFromId = null;
      s.notice = null;
      draw(root, s);
    }
  };
  document.addEventListener("keydown", onKey);
  draw(root, s);
  return () => {
    document.removeEventListener("keydown", onKey);
    closeModal();
  };
}

function editable(s) {
  return isEditableVersion(s.version);
}

function viewModel(s) {
  const ids = s.graph.nodes.map((node) => node.id);
  const layout = computeLayout(s.graph.nodes, s.graph.edges);
  const positions = applyPositionOverrides(layout.positions, s.overrides, ids);
  const keyById = new Map(s.graph.nodes.map((node) => [node.id, node.key]));
  const ctx = { versionIndex: s.versionIndex, definitionIndex: s.definitionIndex, modelsById: s.modelsById };
  const flaggedKeys = s.validation && !s.validation.stale ? flaggedNodeKeys(s.validation.annotated) : new Set();
  const flaggedIds = new Set(s.graph.nodes.filter((node) => flaggedKeys.has(node.key)).map((node) => node.id));
  return {
    nodes: s.graph.nodes.map((node) => {
      const card = describeNode(node, ctx);
      const hint = editable(s) && isSupportedType(node.type) ? nodeHint(node, ctx) : null;
      return { id: node.id, type: node.type, title: card.title, tag: card.tag, lines: card.lines, unsupported: card.unsupported, hint };
    }),
    edges: s.graph.edges.map((edge) => ({ id: edge.id, from: edge.from, to: edge.to, label: `${keyById.get(edge.from) || "?"} → ${keyById.get(edge.to) || "?"}` })),
    positions,
    selectedNodeId: s.selection.kind === "node" ? s.selection.id : null,
    selectedEdgeId: s.selection.kind === "edge" ? s.selection.id : null,
    connectFromId: s.connectFromId,
    flaggedIds,
    editable: editable(s),
  };
}

function draw(root, s) {
  const previous = root.querySelector(".studio-canvas");
  const scroll = previous ? { left: previous.scrollLeft, top: previous.scrollTop } : null;
  const redraw = () => draw(root, s);

  const canvas = createDagCanvas({
    onSelectNode: (id) => {
      s.selection = id ? { kind: "node", id } : { kind: null, id: null };
      if (!id) s.connectFromId = null;
      s.ui.confirmDeleteNodeId = null;
      redraw();
    },
    onSelectEdge: (id) => {
      s.selection = { kind: "edge", id };
      s.connectFromId = null;
      redraw();
    },
    onStartConnect: (id) => {
      s.connectFromId = s.connectFromId === id ? null : id;
      s.notice = s.connectFromId ? "Now click the node this one should feed. Press Esc to cancel." : null;
      redraw();
    },
    onConnect: (from, to) => connect(root, s, from, to),
    onMove: (id, x, y) => {
      s.overrides[id] = { x, y };
      savePositions(s.version.id, s.overrides);
    },
    onDelete: () => {
      if (!editable(s)) return;
      if (s.selection.kind === "edge") disconnect(root, s, s.selection.id);
      else if (s.selection.kind === "node") {
        s.ui.confirmDeleteNodeId = s.selection.id;
        redraw();
      }
    },
    onEscape: () => {
      s.connectFromId = null;
      s.notice = null;
      redraw();
    },
  });
  canvas.render(viewModel(s));

  const shell = el("div", { class: "studio" }, [
    buildToolbar(root, s),
    buildBanners(root, s),
    el("div", { class: "studio-grid" }, [
      buildPalette(root, s),
      el("div", { class: "studio-canvas-wrap" }, [
        canvas.element,
        !s.graph.nodes.length ? el("div", { class: "studio-empty" }, buildEmptyCanvas(root, s)) : null,
      ]),
      el("aside", { class: "studio-inspector", "aria-label": "Inspector" }, buildInspectorPanel(root, s)),
    ]),
    buildValidationPanel(root, s),
    buildHistoryPanel(root, s),
  ]);
  mount(root, shell);
  if (scroll) {
    const fresh = root.querySelector(".studio-canvas");
    if (fresh) {
      fresh.scrollLeft = scroll.left;
      fresh.scrollTop = scroll.top;
    }
  }
}

function buildEmptyCanvas(root, s) {
  const parts = [el("p", {}, editable(s) ? "This draft is empty. Add nodes from the palette, or start from an example structure." : "This version has no nodes.")];
  if (editable(s)) {
    parts.push(
      el("button", { type: "button", class: "primary small", disabled: Boolean(s.busy), onclick: () => insertExample(root, s) }, "Insert example structure"),
      el("p", { class: "hint" }, "Planner → Engineer → Test / Security / Code → an Evaluation each → one Human Approval → Complete. You choose the Agents and models.")
    );
  }
  return parts;
}

function buildToolbar(root, s) {
  const version = s.version;
  const projectSelect = el(
    "select",
    {
      "aria-label": "Project",
      onchange: (event) => {
        writeStoredProjectId(event.target.value);
        navigate("#/workflows");
      },
    },
    s.projects.length
      ? s.projects.map((project) => el("option", { value: project.id, selected: project.id === s.workflow.project_id }, project.name))
      : [el("option", { value: s.workflow.project_id, selected: true }, "This project")]
  );
  const versionSelect = s.versions.length
    ? el(
        "select",
        { "aria-label": "Version", onchange: (event) => navigate(studioHash(s.workflowId, Number(event.target.value))) },
        s.versions.map((candidate) => el("option", { value: String(candidate.version), selected: version && candidate.version === version.version }, versionLabel(candidate)))
      )
    : el("span", { class: "hint" }, "no versions");

  const isDraft = editable(s);
  const blocked = version ? runBlockReason(version, s.graph) : "No version selected.";
  const disabled = Boolean(s.busy);
  const buttons = [];
  if (version && isDraft) {
    buttons.push(
      el("button", { type: "button", disabled, onclick: () => validate(root, s) }, "Validate"),
      el("button", { type: "button", class: "primary", disabled: disabled || !s.graph.nodes.length, onclick: () => { s.confirm = { kind: "publish" }; draw(root, s); } }, "Publish")
    );
  }
  if (version && canCloneVersion(version)) {
    buttons.push(el("button", { type: "button", disabled, onclick: () => cloneVersion(root, s) }, "Edit as new version"));
  }
  buttons.push(
    el("button", { type: "button", class: blocked ? "" : "primary", disabled: Boolean(blocked) || disabled, title: blocked || "Run this exact version", onclick: () => openRunDialog(s) }, "Run")
  );
  buttons.push(
    el("button", { type: "button", class: "small", disabled: !s.graph.nodes.length, onclick: () => { clearPositions(s.version.id); s.overrides = {}; draw(root, s); } }, "Auto-arrange")
  );

  return el("div", { class: "studio-toolbar" }, [
    el("div", { class: "studio-toolbar-left" }, [
      el("a", { class: "studio-back", href: "#/workflows" }, "← Workflows"),
      el("label", { class: "studio-inline" }, ["Project ", projectSelect]),
      el("h1", { class: "studio-title" }, s.workflow.name),
      el("label", { class: "studio-inline" }, ["Version ", versionSelect]),
      version ? el("span", { class: `badge ${version.status === "active" ? "badge-done" : version.status === "draft" ? "badge-running" : "badge-neutral"}` }, `${statusLabel(version.status)} v${version.version}`) : null,
    ]),
    el("div", { class: "studio-toolbar-right" }, buttons),
  ]);
}

function buildBanners(root, s) {
  const banners = [];
  if (s.busy && typeof s.busy === "string") banners.push(el("div", { class: "card studio-banner" }, [el("span", { class: "spinner" }), ` ${s.busy}`]));
  if (s.error) {
    banners.push(
      el("div", { class: "error-banner studio-banner", role: "alert" }, [
        el("div", {}, s.error),
        s.errorIssues.length ? el("ul", {}, s.errorIssues.map((issue) => el("li", {}, issue))) : null,
      ])
    );
  }
  if (s.notice && !s.error) banners.push(el("div", { class: "card studio-banner studio-notice", role: "status" }, s.notice));
  if (s.version && !editable(s)) {
    banners.push(
      el("div", { class: "card studio-banner" }, [
        el("strong", {}, `${versionLabel(s.version)} is read-only.`),
        el("span", {}, " A published version never changes, so runs always know exactly what they ran. "),
        canCloneVersion(s.version) ? el("button", { type: "button", class: "small", disabled: Boolean(s.busy), onclick: () => cloneVersion(root, s) }, "Edit as new version") : null,
      ])
    );
  }
  const unsupported = unsupportedNodes(s.graph);
  if (unsupported.length) {
    banners.push(
      el("div", { class: "error-banner studio-banner" }, [
        el("strong", {}, "This workflow uses node types this Studio does not support: "),
        `${[...new Set(unsupported.map((node) => typeLabel(node.type)))].join(", ")}. They are shown read-only and are never changed automatically. Run is blocked from here.`,
      ])
    );
  }
  if (s.confirm && s.confirm.kind === "publish") {
    banners.push(
      el("div", { class: "card studio-banner studio-confirm" }, [
        el("p", {}, `Publish v${s.version.version}? It is validated first, and once ACTIVE it is read-only. To change it later you create a new version.`),
        el("div", { class: "row" }, [
          el("button", { type: "button", class: "primary small", disabled: Boolean(s.busy), onclick: () => publish(root, s) }, "Publish"),
          el("button", { type: "button", class: "small", onclick: () => { s.confirm = null; draw(root, s); } }, "Cancel"),
        ]),
      ])
    );
  }
  return el("div", { class: "studio-banners" }, banners);
}

function buildPalette(root, s) {
  const can = editable(s) && !s.busy;
  return el("aside", { class: "studio-palette", "aria-label": "Palette" }, [
    el("h3", {}, "Add"),
    ...PALETTE.map((entry) =>
      el("button", { type: "button", class: `studio-palette-item studio-palette-${entry.type}`, disabled: !can, title: entry.hint, onclick: () => addNode(root, s, entry.type) }, [
        el("span", { class: "studio-palette-label" }, `＋ ${entry.label}`),
        el("span", { class: "studio-palette-hint" }, entry.hint),
      ])
    ),
    !editable(s) ? el("p", { class: "hint" }, "Only a DRAFT can be edited.") : null,
  ]);
}

function inspectorContext(root, s) {
  const redraw = () => draw(root, s);
  return {
    graph: s.graph,
    editable: editable(s) && !s.busy,
    agentCatalog: s.agentCatalog,
    definitionCatalog: s.definitionCatalog,
    models: s.models,
    modelsById: s.modelsById,
    versionIndex: s.versionIndex,
    definitionIndex: s.definitionIndex,
    ui: s.ui,
    loadPromptVersions: (agentId) => workflowApi.getPromptVersions(agentId),
    redraw,
    onConfig: (nodeId, config) => setConfig(root, s, nodeId, config),
    onConnect: (from, to) => connect(root, s, from, to),
    onDisconnect: (edgeId) => disconnect(root, s, edgeId),
    onDelete: (nodeId) => deleteNode(root, s, nodeId),
    onSelectNode: (id) => {
      s.selection = id ? { kind: "node", id } : { kind: null, id: null };
      s.ui.confirmDeleteNodeId = null;
      redraw();
    },
  };
}

function buildInspectorPanel(root, s) {
  if (s.selection.kind === "node") {
    const node = s.graph.nodes.find((candidate) => candidate.id === s.selection.id);
    if (node) return [buildInspector({ ...inspectorContext(root, s), node })];
  }
  if (s.selection.kind === "edge") {
    const edge = s.graph.edges.find((candidate) => candidate.id === s.selection.id);
    if (edge) return [buildEdgeInspector({ ...inspectorContext(root, s), edge })];
  }
  const attention = s.graph.nodes.filter((node) => isSupportedType(node.type) && nodeHint(node, { versionIndex: s.versionIndex, definitionIndex: s.definitionIndex })).length;
  return [
    el("div", { class: "studio-inspector-body" }, [
      el("h2", {}, "Inspector"),
      el("p", { class: "hint" }, "Select a node to configure it, or a connection to remove it."),
      el("p", { class: "hint" }, `${s.graph.nodes.length} node${s.graph.nodes.length === 1 ? "" : "s"}, ${s.graph.edges.length} connection${s.graph.edges.length === 1 ? "" : "s"}.`),
      editable(s) && attention ? el("p", { class: "studio-attention" }, `⚠ ${attention} node${attention === 1 ? " needs" : "s need"} configuration.`) : null,
      el("p", { class: "hint" }, "To connect: select a node and press “Connect”, then click its target. Or use “Connect to…” in the inspector."),
    ]),
  ];
}

function buildValidationPanel(root, s) {
  const validation = s.validation;
  const body = [];
  if (!validation) {
    body.push(el("p", { class: "hint" }, editable(s) ? "Not validated yet. Validate checks the whole workflow the same way Publish does, without publishing." : "Validation applies to drafts."));
  } else {
    if (validation.stale) body.push(el("p", { class: "hint" }, "The workflow changed since this check. Validate again."));
    if (validation.valid) {
      body.push(el("p", { class: "studio-valid" }, "✓ Workflow valid"));
    } else {
      body.push(el("p", { class: "studio-invalid" }, `${validation.issues.length} issue${validation.issues.length === 1 ? "" : "s"} to fix`));
      body.push(
        el(
          "ul",
          { class: "studio-issues" },
          validation.annotated.map((entry) =>
            el("li", {}, [
              el("span", {}, entry.text),
              ...entry.nodeKeys.map((key) => {
                const node = s.graph.nodes.find((candidate) => candidate.key === key);
                return node
                  ? el("button", { type: "button", class: "link studio-issue-node", title: "Possibly related node (matched by name in the message)", onclick: () => { s.selection = { kind: "node", id: node.id }; draw(root, s); } }, `possibly: ${key}`)
                  : null;
              }),
            ])
          )
        )
      );
    }
  }
  return el("div", { class: "card studio-validation", "aria-live": "polite" }, [el("h2", {}, "Validation"), ...body]);
}

// ---------------------------------------------------------------------------------------------------
// run history (MA7.6B)
// ---------------------------------------------------------------------------------------------------

async function loadHistory(root, s) {
  s.history.loading = true;
  s.history.error = null;
  draw(root, s);
  try {
    s.history.runs = await workflowApi.listWorkflowRuns(s.workflowId);
    s.history.loaded = true;
  } catch (err) {
    s.history.error = normalizeApiError(err).message;
  }
  s.history.loading = false;
  draw(root, s);
}

// Every run of this workflow, across all of its versions -- facts only (status, timing,
// the version each run is bound to, usage and cost when recorded). No ranking, no scores.
function buildHistoryPanel(root, s) {
  const history = s.history;
  const rows = history.runs.map((run) => {
    const info = runStatusInfo(run.status);
    return el("tr", {}, [
      el("td", {}, [
        el("code", { title: run.id }, shortId(run.id)),
        run.assignment_title ? el("div", { class: "hint run-history-title" }, run.assignment_title) : null,
      ]),
      el("td", {}, `v${run.workflow_version}`),
      el("td", {}, el("span", { class: `badge run-tone-${info.tone}` }, info.label)),
      el("td", {}, run.started_at ? formatDateTime(run.started_at) : "—"),
      el("td", {}, run.ended_at ? formatDateTime(run.ended_at) : "—"),
      el("td", {}, usageLine(run.usage)),
      el("td", {}, el("a", { class: "studio-open-link", href: `#/workflow-runs/${encodeURIComponent(run.id)}` }, "Open")),
    ]);
  });
  const details = el("details", { class: "card studio-history" }, [
    el("summary", {}, "Run history"),
    el("div", { class: "row" }, [
      el("button", { type: "button", class: "small", disabled: history.loading, onclick: () => loadHistory(root, s) }, history.loaded ? "Refresh" : "Load"),
      el("span", { class: "hint" }, "Every run of this workflow, across all versions. Select one to open its Control Room."),
    ]),
    history.loading ? el("p", { class: "hint" }, [el("span", { class: "spinner" }), " Loading runs…"]) : null,
    history.error ? el("div", { class: "error-banner" }, history.error) : null,
    history.loaded && !history.runs.length && !history.loading ? el("p", { class: "hint" }, "This workflow has not been run yet.") : null,
    history.runs.length
      ? el("table", {}, [
          el("thead", {}, el("tr", {}, ["Run", "Version", "Status", "Started", "Ended", "Usage and cost", ""].map((label) => el("th", {}, label)))),
          el("tbody", {}, rows),
        ])
      : null,
  ]);
  if (history.open) details.setAttribute("open", "");
  details.addEventListener("toggle", () => {
    if (details.open === history.open) return; // ignore the toggle caused by our own redraw
    history.open = details.open;
    if (details.open && !history.loaded && !history.loading) loadHistory(root, s);
  });
  return details;
}

// ---------------------------------------------------------------------------------------------------
// actions
// ---------------------------------------------------------------------------------------------------

async function runAction(root, s, label, work) {
  if (s.busy) return;
  s.busy = label || true;
  s.error = null;
  s.errorIssues = [];
  s.notice = null;
  draw(root, s);
  try {
    await work();
  } catch (err) {
    const normalized = normalizeApiError(err);
    s.error = normalized.message;
    s.errorIssues = normalized.issues;
    if (normalized.isValidation) {
      s.validation = { valid: false, issues: normalized.issues, annotated: annotateIssues(normalized.issues, s.graph.nodes.map((node) => node.key)), stale: false };
    }
  } finally {
    s.busy = null;
    draw(root, s);
  }
}

function markStale(s) {
  if (s.validation) s.validation.stale = true;
}

function addNode(root, s, type) {
  const entry = PALETTE.find((candidate) => candidate.type === type);
  return runAction(root, s, `Adding ${entry.label}…`, async () => {
    const key = uniqueNodeKey(entry.baseKey, s.graph.nodes.map((node) => node.key));
    const created = await workflowApi.addNode(s.workflowId, s.version.version, buildNodePayload({ key, type, config: defaultConfigFor(type) }));
    await refreshGraph(s);
    s.selection = { kind: "node", id: created.id };
    markStale(s);
  });
}

function deleteNode(root, s, nodeId) {
  const node = s.graph.nodes.find((candidate) => candidate.id === nodeId);
  s.ui.confirmDeleteNodeId = null;
  if (!node) return undefined;
  return runAction(root, s, `Deleting ${node.key}…`, async () => {
    // The server refuses to delete a node that still has connections, so remove them first.
    for (const edge of s.graph.edges.filter((candidate) => candidate.from === nodeId || candidate.to === nodeId)) {
      await workflowApi.deleteEdge(s.workflowId, s.version.version, edge.id);
    }
    await workflowApi.deleteNode(s.workflowId, s.version.version, nodeId);
    await refreshGraph(s);
    markStale(s);
  });
}

function connect(root, s, from, to) {
  s.connectFromId = null;
  const problem = connectionProblem(s.graph, from, to);
  if (problem) {
    s.error = problem;
    s.errorIssues = [];
    draw(root, s);
    return undefined;
  }
  return runAction(root, s, "Connecting…", async () => {
    await workflowApi.addEdge(s.workflowId, s.version.version, from, to);
    await refreshGraph(s);
    markStale(s);
  });
}

function disconnect(root, s, edgeId) {
  return runAction(root, s, "Disconnecting…", async () => {
    await workflowApi.deleteEdge(s.workflowId, s.version.version, edgeId);
    await refreshGraph(s);
    markStale(s);
  });
}

function setConfig(root, s, nodeId, config) {
  return runAction(root, s, "Saving…", async () => {
    const updated = await workflowApi.patchNode(s.workflowId, s.version.version, nodeId, config);
    const node = s.graph.nodes.find((candidate) => candidate.id === nodeId);
    if (node) node.config = updated && updated.config && typeof updated.config === "object" ? updated.config : {};
    markStale(s);
  });
}

function validate(root, s) {
  return runAction(root, s, "Validating…", async () => {
    const result = normalizeValidationResult(await workflowApi.validate(s.workflowId, s.version.version));
    s.validation = { ...result, annotated: annotateIssues(result.issues, s.graph.nodes.map((node) => node.key)), stale: false };
  });
}

function publish(root, s) {
  const number = s.version.version;
  s.confirm = null;
  return runAction(root, s, "Publishing…", async () => {
    await workflowApi.publish(s.workflowId, number);
    s.versions = sortVersionsNewestFirst(await workflowApi.listVersions(s.workflowId));
    const published = s.versions.find((candidate) => candidate.version === number);
    await loadVersion(s, published);
    s.notice = `Published v${number}. It is now ACTIVE and read-only. You can run it, or edit it as a new version.`;
  });
}

function cloneVersion(root, s) {
  const number = s.version.version;
  return runAction(root, s, `Copying v${number} to a new draft…`, async () => {
    const created = await workflowApi.cloneVersion(s.workflowId, number);
    pendingNotice = `Created draft v${created.version} from v${number}. v${number} is unchanged.`;
    navigate(studioHash(s.workflowId, created.version));
  });
}

function insertExample(root, s) {
  return runAction(root, s, "Adding the example structure…", async () => {
    const spec = exampleDag();
    const idByKey = new Map();
    for (const node of spec.nodes) {
      const created = await workflowApi.addNode(s.workflowId, s.version.version, buildNodePayload({ key: node.key, type: node.type, config: defaultConfigFor(node.type) }));
      idByKey.set(node.key, created.id);
    }
    for (const [from, to] of spec.edges) {
      await workflowApi.addEdge(s.workflowId, s.version.version, idByKey.get(from), idByKey.get(to));
    }
    await refreshGraph(s);
    markStale(s);
    s.notice = "Example structure added. Choose an Agent and a model for each Agent node, and an Evaluation Definition for each Evaluation.";
  }).then(async () => {
    // A partial failure still leaves whatever was added: show it.
    if (!s.error) return;
    try {
      await refreshGraph(s);
    } catch {
      // keep the original error
    }
    draw(root, s);
  });
}

// ---------------------------------------------------------------------------------------------------
// run dialog
// ---------------------------------------------------------------------------------------------------

function openRunDialog(s) {
  const dialog = { title: "", description: "", taskId: null, key: newIdempotencyKey(), busy: false, error: null, result: null, mics: null };
  const content = el("div", { class: "studio-run-dialog" });
  const versionNumber = s.version.version;

  const startEnabled = () => !dialog.busy && Boolean(dialog.title.trim());
  const syncStart = () => {
    const button = content.querySelector(".studio-start-btn");
    if (button) button.disabled = !startEnabled();
  };
  // Dictation writes into the (still editable) fields and never submits anything.
  const putText = (field, selector) => (text) => {
    dialog[field] = text;
    const input = content.querySelector(selector);
    if (input) input.value = text;
    syncStart();
  };
  const stopMics = () => {
    if (dialog.mics) for (const mic of Object.values(dialog.mics)) mic.abort();
  };
  function micsOnce() {
    if (!dialog.mics) {
      dialog.mics = {
        title: buildVoiceControl({ label: "the assignment title", getValue: () => dialog.title, setValue: putText("title", "input[aria-label='Assignment title']") }),
        description: buildVoiceControl({ label: "the assignment details", getValue: () => dialog.description, setValue: putText("description", "textarea[aria-label='Assignment description']") }),
      };
    }
    return dialog.mics;
  }
  const close = () => {
    stopMics();
    closeModal();
  };

  function paint() {
    clear(content);
    content.appendChild(
      el("div", { class: "modal-header" }, [
        el("div", {}, [el("h2", {}, dialog.result ? "Workflow started" : `Run “${s.workflow.name}”`), el("div", { class: "modal-subtitle" }, `Version v${versionNumber} (${statusLabel(s.version.status)})`)]),
        el("button", { type: "button", class: "modal-close-btn", "aria-label": "Close", onclick: close }, "✕"),
      ])
    );
    if (dialog.result) {
      stopMics();
      const runId = dialog.result.id;
      content.appendChild(
        el("div", { class: "stack" }, [
          el("p", {}, "The team has been assigned the work."),
          el("div", { class: "studio-run-facts" }, [
            el("div", {}, [el("span", { class: "hint" }, "Run ID"), el("code", { class: "studio-run-id" }, runId)]),
            el("div", {}, [el("span", { class: "hint" }, "Current status"), el("strong", {}, String(dialog.result.status))]),
            el("div", {}, [el("span", { class: "hint" }, "Workflow version"), el("strong", {}, `v${versionNumber}`)]),
          ]),
          el("p", { class: "hint studio-note" }, "Watch the team work, inspect any step, and make the Human Approval decision in the Control Room."),
          el("div", { class: "row" }, [
            el(
              "button",
              {
                type: "button",
                class: "primary studio-open-run",
                onclick: () => {
                  close();
                  navigate(`#/workflow-runs/${encodeURIComponent(runId)}`);
                },
              },
              "Open live run"
            ),
            el("button", { type: "button", onclick: close }, "Close"),
          ]),
        ])
      );
      return;
    }
    const locked = Boolean(dialog.taskId);
    const titleInput = el("input", {
      type: "text",
      value: dialog.title,
      maxlength: "255",
      disabled: locked || dialog.busy,
      placeholder: "What should the team do?",
      "aria-label": "Assignment title",
      oninput: (event) => {
        dialog.title = event.target.value;
        syncStart();
      },
    });
    const descriptionInput = el("textarea", {
      rows: "5",
      disabled: locked || dialog.busy,
      placeholder: "Details, constraints, acceptance criteria…",
      "aria-label": "Assignment description",
      oninput: (event) => {
        dialog.description = event.target.value;
      },
    });
    descriptionInput.value = dialog.description;
    const mics = locked ? null : micsOnce();
    const startButton = el(
      "button",
      {
        type: "button",
        class: "primary studio-start-btn",
        disabled: !startEnabled(),
        onclick: async () => {
          if (!startEnabled()) return;
          stopMics(); // the microphone is never left running once the person submits
          dialog.busy = true;
          dialog.error = null;
          paint();
          try {
            if (!dialog.taskId) {
              const task = await workflowApi.createTask({ projectId: s.workflow.project_id, title: dialog.title.trim(), description: dialog.description.trim() });
              dialog.taskId = task.id;
            }
            dialog.result = await workflowApi.startRun(s.workflowId, versionNumber, dialog.taskId, dialog.key);
          } catch (err) {
            dialog.error = normalizeApiError(err).message;
          }
          dialog.busy = false;
          paint();
        },
      },
      dialog.busy ? "Starting…" : "Start workflow"
    );
    content.appendChild(
      el("div", { class: "stack" }, [
        el("p", { class: "hint" }, "Every Agent in this workflow receives this assignment, along with the output of the steps before it. Type it, or dictate it and edit the text before you start."),
        el("div", {}, [el("label", {}, "Assignment title"), titleInput, mics ? mics.title.element : null]),
        el("div", {}, [el("label", {}, "Description (optional)"), descriptionInput, mics ? mics.description.element : null]),
        locked ? el("p", { class: "hint" }, "The assignment is saved. Retrying starts it without creating another.") : null,
        dialog.error ? el("div", { class: "error-banner", role: "alert" }, dialog.error) : null,
        el("div", { class: "row" }, [startButton, el("button", { type: "button", onclick: close }, "Cancel")]),
      ])
    );
  }
  paint();
  openModal(content, { label: "Run workflow" });
}
