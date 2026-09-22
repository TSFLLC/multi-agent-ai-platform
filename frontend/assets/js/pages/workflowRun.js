// Live Control Room for one WorkflowRun (MA7.6B): watch the AI team execute a
// workflow, inspect any step, and intervene at Human Approval.
//
//   Project -> Workflow -> Agents working -> Evaluations -> Human Approval -> Complete
//
// This is an EXECUTION view, not an editor: the graph is the exact version the run
// is bound to, drawn with the same deterministic layout as the Studio, with every
// node showing its runtime state. Nothing here edits nodes or edges, and the only
// consequential actions are the ones the backend already enforces: a person's
// approve/reject decision and cancelling the run.
//
// Live updates: one snapshot endpoint (GET /workflow-runs/{id}/detail) polled
// sequentially and stopped when the run is terminal or the page is left
// (runPolling.js). No WebSocket / EventSource: the API needs the Authorization
// header, which EventSource cannot send.

import { el, mount } from "../dom.js";
import { formatDateTime } from "../format.js";
import { computeLayout } from "../dagLayout.js";
import { createDagCanvas } from "../dagCanvas.js";
import { buildRunInspector } from "../runInspector.js";
import { createPoller } from "../runPolling.js";
import { workflowApi } from "../workflowApi.js";
import { normalizeApiError } from "../workflowErrors.js";
import { describeNode } from "../workflowGraph.js";
import {
  costLabel,
  decisionErrorMessage,
  formatDuration,
  formatTokenCount,
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
} from "../runState.js";

const LEGEND = [
  ["Waiting", "Starts when everything before it has finished."],
  ["Queued", "Assigned to an Agent, waiting for a worker to pick it up."],
  ["Running", "An Agent is working on it now."],
  ["Waiting for Human Approval", "The team has done what it can. A person must approve or reject."],
  ["Completed", "Finished. Its output and evidence are kept."],
  ["Failed", "The step failed or a person rejected the approval. Finished work is kept."],
  ["Cancelled", "The run was cancelled before this step finished."],
];

function toneBadge(tone) {
  return `badge run-tone-${tone}`;
}

export async function renderWorkflowRun(root, params) {
  const s = {
    runId: params.id,
    detail: null,
    serialized: "",
    error: null,
    notice: null,
    pollState: "idle",
    lastUpdated: null,
    selectedNodeId: null,
    autoSelected: false,
    confirmCancel: false,
    busy: null,
    disposed: false,
    cache: new Map(),
    ui: { openOutputs: new Set(), openEvidence: new Set(), decision: null, retry: null },
  };
  mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Opening the Control Room…"]));

  const redraw = () => {
    if (!s.disposed && s.detail) draw(root, s, poller);
  };

  const poller = createPoller({
    fetchOnce: () => workflowApi.getRunDetail(s.runId),
    onData: (data) => {
      s.error = null;
      s.lastUpdated = new Date();
      const serialized = JSON.stringify(data);
      if (serialized === s.serialized && s.detail) return; // nothing changed: do not repaint (keeps scroll, focus and typed notes)
      s.serialized = serialized;
      s.detail = data;
      if (!s.autoSelected) {
        s.autoSelected = true;
        const waiting = pendingApprovalNodes(data);
        if (waiting.length) s.selectedNodeId = waiting[0].node_id; // bring the decision to the front
      }
      if (!s.disposed) draw(root, s, poller);
    },
    onError: (err) => {
      s.error = normalizeApiError(err).message;
      if (s.detail) redraw();
    },
    onState: (state) => {
      s.pollState = state;
      if (s.detail && state !== "stopped") redraw();
    },
    isTerminal: (data) => isTerminalRun(data.status),
    intervalFor: (data, opts) => pollIntervalFor(data, opts),
    isHidden: () => typeof document !== "undefined" && document.hidden,
  });

  const onVisible = () => {
    if (!document.hidden && s.detail && poller.state === "live") poller.refresh(); // catch up as soon as the tab is back
  };
  document.addEventListener("visibilitychange", onVisible);

  await poller.start();
  if (!s.detail) {
    mount(root, el("div", { class: "error-banner" }, s.error || "This run could not be loaded."));
  }
  return () => {
    s.disposed = true;
    poller.stop();
    document.removeEventListener("visibilitychange", onVisible);
  };
}

// ---------------------------------------------------------------------------------------------------
// actions
// ---------------------------------------------------------------------------------------------------

function invalidateCaches(s, { onlyErrors = false } = {}) {
  for (const [key, entry] of [...s.cache.entries()]) {
    if (onlyErrors ? entry.state === "error" : !key.startsWith("artifact:")) s.cache.delete(key);
  }
}

async function cancelRun(root, s, poller) {
  if (s.busy) return;
  s.busy = "Cancelling…";
  s.confirmCancel = false;
  s.error = null;
  draw(root, s, poller);
  try {
    await workflowApi.cancelRun(s.runId);
    s.notice = "Cancellation requested. Running steps are being asked to stop; finished work is kept.";
    invalidateCaches(s);
    await poller.refresh();
  } catch (err) {
    s.error = normalizeApiError(err).message;
  } finally {
    s.busy = null;
    if (!s.disposed) draw(root, s, poller);
  }
}

function decisionActions(root, s, poller) {
  const redraw = () => draw(root, s, poller);
  return {
    startDecision(approvalId, kind, fingerprint) {
      s.ui.decision = { approvalId, kind, fingerprint, note: "", busy: false, error: null };
      redraw();
    },
    cancelDecision() {
      s.ui.decision = null;
      redraw();
    },
    setNote(text) {
      if (s.ui.decision) s.ui.decision.note = text; // no repaint: typing must not lose focus
    },
    async confirmDecision() {
      const decision = s.ui.decision;
      if (!decision || decision.busy) return; // one submission at a time
      decision.busy = true;
      decision.error = null;
      redraw();
      try {
        await workflowApi.resolveApproval(decision.approvalId, {
          approve: decision.kind === "approve",
          actionFingerprint: decision.fingerprint,
          notes: decision.note,
        });
        s.notice = decision.kind === "approve" ? "Approved. The workflow is continuing." : "Rejected. The workflow has stopped; finished work and evidence are kept.";
        s.ui.decision = null;
        invalidateCaches(s);
        await poller.refresh();
      } catch (err) {
        decision.busy = false;
        decision.error = decisionErrorMessage(err);
        if (err && err.status === 409) invalidateCaches(s); // show what was actually recorded
        redraw();
      }
    },
  };
}

function retryActions(root, s, poller) {
  const redraw = () => draw(root, s, poller);
  return {
    startRetry(nodeRunId) {
      s.ui.retry = { nodeRunId, providerModelId: "", busy: false, error: null };
      redraw();
    },
    cancelRetry() {
      s.ui.retry = null;
      redraw();
    },
    setRetryModel(providerModelId) {
      if (s.ui.retry) {
        s.ui.retry.providerModelId = providerModelId;
        redraw();
      }
    },
    async confirmRetry() {
      const retry = s.ui.retry;
      if (!retry || retry.busy || !retry.providerModelId) return; // one submission at a time
      retry.busy = true;
      retry.error = null;
      redraw();
      try {
        await workflowApi.retryNode(s.runId, retry.nodeRunId, retry.providerModelId);
        s.notice = "Retry started with the replacement model. If it succeeds, the workflow continues automatically.";
        s.ui.retry = null;
        invalidateCaches(s);
        await poller.refresh();
      } catch (err) {
        retry.busy = false;
        retry.error = retryErrorMessage(err);
        redraw();
      }
    },
    // MA7.8: a step interrupted by a worker restart is retried with its original configuration.
    async retryInterrupted(nodeRunId) {
      if (s.ui.retry && s.ui.retry.busy) return; // one submission at a time
      const retry = { nodeRunId, providerModelId: "", busy: true, error: null };
      s.ui.retry = retry;
      redraw();
      try {
        const queued = await workflowApi.retryNode(s.runId, nodeRunId, null);
        s.notice =
          queued && queued.status === "pending" && s.detail && s.detail.status === "failed"
            ? "Retry queued. The workflow resumes once every other failed step has been retried."
            : "Retry started. If it succeeds, the workflow continues automatically.";
        s.ui.retry = null;
        invalidateCaches(s);
        await poller.refresh();
      } catch (err) {
        retry.busy = false;
        retry.error = retryErrorMessage(err);
        redraw();
      }
    },
  };
}

// ---------------------------------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------------------------------

function viewModel(s) {
  const { detail } = s;
  const layout = computeLayout(
    detail.nodes.map((node) => ({ id: node.node_id, key: node.node_key })),
    detail.edges.map((edge) => ({ id: edge.id, from: edge.from_node_id, to: edge.to_node_id }))
  );
  const completed = new Set(detail.nodes.filter((node) => node.status === "completed").map((node) => node.node_id));
  const keyById = new Map(detail.nodes.map((node) => [node.node_id, node.node_key]));
  return {
    nodes: detail.nodes.map((node) => {
      const card = describeRunNode(node);
      const state = nodeState(node);
      const needsYou = node.status === "waiting_for_approval" && node.approval_status === "pending";
      return {
        id: node.node_id,
        type: node.node_type,
        title: card.title,
        tag: card.tag,
        lines: card.lines,
        unsupported: card.unsupported,
        hint: needsYou ? "Needs your decision" : null,
        status: { key: state.key, label: state.label },
      };
    }),
    edges: detail.edges.map((edge) => ({ id: edge.id, from: edge.from_node_id, to: edge.to_node_id, label: `${keyById.get(edge.from_node_id) || "?"} → ${keyById.get(edge.to_node_id) || "?"}`, done: completed.has(edge.from_node_id) })),
    positions: layout.positions,
    selectedNodeId: s.selectedNodeId,
    selectedEdgeId: null,
    connectFromId: null,
    flaggedIds: new Set(),
    editable: false,
  };
}

// Card lines for an executed step: the Agent and the Model on separate lines.
function describeRunNode(node) {
  const base = describeNode({ key: node.node_key, type: node.node_type, config: {} }, {});
  if (base.unsupported) return { ...base, lines: ["Not shown by this build", "Type not supported"] };
  if (node.agent) {
    const agent = node.agent;
    const model = agent.model && agent.model.canonical_model_id;
    const who = `${node.node_type === "evaluation" ? "Evaluator" : "Agent"}: ${agent.agent_name || "—"}${agent.agent_version != null ? ` v${agent.agent_version}` : ""}`;
    return { ...base, lines: [who, `Model: ${model || "not resolved yet"}`] };
  }
  if (node.node_type === "human_approval") return { ...base, lines: ["A person decides", node.approval_status ? `Decision: ${node.approval_status}` : "Waits for a person"] };
  if (node.node_type === "terminal") return { ...base, lines: ["The workflow finishes here"] };
  return { ...base, lines: ["Not started yet"] };
}

function draw(root, s, poller) {
  const previousCanvas = root.querySelector(".studio-canvas");
  const scroll = previousCanvas ? { left: previousCanvas.scrollLeft, top: previousCanvas.scrollTop } : null;
  const inspectorPanel = root.querySelector(".studio-inspector");
  const inspectorScroll = inspectorPanel ? inspectorPanel.scrollTop : 0;
  const { detail } = s;
  const redraw = () => {
    if (!s.disposed) draw(root, s, poller);
  };

  const canvas = createDagCanvas(
    {
      onSelectNode: (id) => {
        if (id !== s.selectedNodeId) {
          s.ui.decision = null; // a confirmation belongs to the step it was opened on
          s.ui.retry = null;
        }
        s.selectedNodeId = id;
        redraw();
      },
    },
    { draggable: false }
  );
  canvas.render(viewModel(s));

  const selected = detail.nodes.find((node) => node.node_id === s.selectedNodeId);
  const inspector = selected
    ? buildRunInspector({
        node: selected,
        detail,
        nowMs: Date.now(),
        cache: s.cache,
        ui: s.ui,
        redraw,
        onSelectNodeKey: (key) => {
          const target = detail.nodes.find((node) => node.node_key === key);
          if (target) {
            s.selectedNodeId = target.node_id;
            redraw();
          }
        },
        actions: { ...decisionActions(root, s, poller), ...retryActions(root, s, poller) },
      })
    : buildIdleInspector(s);

  const shell = el("div", { class: "studio run-room" }, [
    buildHeader(root, s, poller),
    buildBanners(root, s, poller),
    buildSummary(s),
    el("div", { class: "run-grid" }, [
      el("div", { class: "studio-canvas-wrap" }, [canvas.element]),
      el("aside", { class: "studio-inspector", "aria-label": "Step details" }, [inspector]),
    ]),
    buildLegend(),
  ]);
  mount(root, shell);
  const fresh = root.querySelector(".studio-canvas");
  if (fresh && scroll) {
    fresh.scrollLeft = scroll.left;
    fresh.scrollTop = scroll.top;
  }
  const freshInspector = root.querySelector(".studio-inspector");
  if (freshInspector) freshInspector.scrollTop = inspectorScroll;
}

function liveText(s) {
  switch (s.pollState) {
    case "live":
      return `● Live: updating automatically${s.lastUpdated ? ` (last ${s.lastUpdated.toLocaleTimeString()})` : ""}`;
    case "finished":
      return `Finished: live updates stopped${s.lastUpdated ? ` (as of ${s.lastUpdated.toLocaleTimeString()})` : ""}`;
    case "paused":
      return "Live updates paused. Press Refresh to continue.";
    default:
      return "";
  }
}

function buildHeader(root, s, poller) {
  const { detail } = s;
  const info = runStatusInfo(detail.status);
  const terminal = isTerminalRun(detail.status);
  const disabled = Boolean(s.busy);
  return el("div", { class: "studio-toolbar run-header" }, [
    el("div", { class: "studio-toolbar-left" }, [
      el("a", { class: "studio-back", href: `#/workflows/${encodeURIComponent(detail.workflow_id)}?v=${detail.workflow_version}` }, "← Back to workflow"),
      el("h1", { class: "studio-title" }, detail.workflow_name),
      el("span", { class: "badge badge-neutral", title: `Bound to workflow version ${detail.workflow_version_id}` }, `Version v${detail.workflow_version}`),
      el("span", { class: toneBadge(info.tone), title: info.help }, info.label),
    ]),
    el("div", { class: "studio-toolbar-right" }, [
      el("span", { class: "hint run-live" }, liveText(s)),
      el("button", { type: "button", disabled, onclick: async () => { invalidateCaches(s, { onlyErrors: true }); s.notice = null; await poller.refresh(); } }, "Refresh"),
      !terminal && detail.status !== "cancelling"
        ? el("button", { type: "button", class: "danger", disabled, onclick: () => { s.confirmCancel = true; draw(root, s, poller); } }, "Cancel workflow")
        : null,
    ]),
  ]);
}

function buildBanners(root, s, poller) {
  const { detail } = s;
  const banners = [];
  const facts = [
    detail.assignment_title ? el("span", {}, [el("span", { class: "hint" }, "Assignment "), el("strong", {}, detail.assignment_title)]) : null,
    el("span", { title: detail.id }, [el("span", { class: "hint" }, "Run "), el("code", {}, shortId(detail.id))]),
    detail.started_at ? el("span", {}, [el("span", { class: "hint" }, "Started "), formatDateTime(detail.started_at)]) : null,
    detail.ended_at ? el("span", {}, [el("span", { class: "hint" }, "Ended "), formatDateTime(detail.ended_at)]) : null,
    secondsBetween(detail.started_at, detail.ended_at) != null ? el("span", {}, [el("span", { class: "hint" }, "Took "), formatDuration(secondsBetween(detail.started_at, detail.ended_at))]) : null,
  ];
  banners.push(el("div", { class: "run-facts-row" }, facts));

  if (s.busy) banners.push(el("div", { class: "card studio-banner" }, [el("span", { class: "spinner" }), ` ${s.busy}`]));
  if (s.error) banners.push(el("div", { class: "error-banner studio-banner", role: "alert" }, s.detail ? `Could not refresh: ${s.error}` : s.error));
  if (s.notice && !s.error) banners.push(el("div", { class: "card studio-banner studio-notice", role: "status" }, s.notice));

  const waiting = pendingApprovalNodes(detail);
  if (waiting.length) {
    banners.push(
      el("div", { class: "run-attention", role: "status" }, [
        el("div", {}, [el("strong", {}, "⏳ Waiting for your decision"), el("div", { class: "hint" }, "Nothing continues until you approve or reject. It will wait as long as needed; it never decides on its own.")]),
        el(
          "div",
          { class: "row" },
          waiting.map((node) =>
            el("button", { type: "button", class: "primary", onclick: () => { s.selectedNodeId = node.node_id; draw(root, s, poller); } }, waiting.length > 1 ? `Review ${node.node_key}` : "Review and decide")
          )
        ),
      ])
    );
  }

  if (s.confirmCancel) {
    banners.push(
      el("div", { class: "card studio-banner studio-confirm", role: "group", "aria-label": "Confirm cancel" }, [
        el("p", {}, [el("strong", {}, "Cancel this workflow? "), "Running steps are asked to stop and steps not yet started are cancelled. Finished work and its evidence are kept. A pending approval is closed without a decision. This cannot be undone."]),
        el("div", { class: "row" }, [
          el("button", { type: "button", class: "danger small", onclick: () => cancelRun(root, s, poller) }, "Yes, cancel the workflow"),
          el("button", { type: "button", class: "small", onclick: () => { s.confirmCancel = false; draw(root, s, poller); } }, "Keep it running"),
        ]),
      ])
    );
  }
  return el("div", { class: "studio-banners" }, banners);
}

function buildSummary(s) {
  const { detail } = s;
  const progress = runProgress(detail.nodes);
  const parts = [`${progress.completed} of ${progress.total} steps complete`];
  if (progress.running) parts.push(`${progress.running} in progress`);
  if (progress.awaiting_decision) parts.push(`${progress.awaiting_decision} waiting for you`);
  if (progress.failed) parts.push(`${progress.failed} failed`);
  if (progress.cancelled) parts.push(`${progress.cancelled} cancelled`);
  const usage = detail.usage;
  const cost = costLabel(usage);
  return el("div", { class: "card run-summary" }, [
    el("div", { class: "run-summary-line" }, [el("strong", {}, parts.join(" · "))]),
    el("div", { class: "hint" }, nextStepText(detail)),
    el(
      "div",
      { class: "run-totals" },
      usage
        ? [
            el("span", {}, [el("span", { class: "hint" }, "Tokens "), formatTokenCount(usage.total_tokens), el("span", { class: "hint" }, ` (${formatTokenCount(usage.tokens_in)} in / ${formatTokenCount(usage.tokens_out)} out)`)]),
            el("span", {}, [el("span", { class: "hint" }, "Cost "), cost.text]),
            el("span", { class: "hint" }, `${usage.model_call_count} model call${usage.model_call_count === 1 ? "" : "s"}`),
            cost.estimated ? el("span", { class: "hint" }, "The total includes estimated cost.") : null,
          ]
        : [el("span", { class: "hint" }, "No model calls yet, so no usage or cost is recorded.")]
    ),
  ]);
}

function buildIdleInspector(s) {
  const { detail } = s;
  return el("div", { class: "studio-inspector-body" }, [
    el("h2", {}, "Step details"),
    el("p", { class: "hint" }, "Select a step to see who did it, which model it used, its output, evidence, usage and cost."),
    pendingApprovalNodes(detail).length ? el("p", { class: "studio-attention" }, "⏳ A step is waiting for your decision. Select it to review the evidence.") : null,
    el("p", { class: "hint" }, `This is a view of version v${detail.workflow_version} exactly as it ran. Nothing here can be edited.`),
  ]);
}

function buildLegend() {
  return el("details", { class: "card run-legend" }, [
    el("summary", {}, "What do these states mean?"),
    el("dl", {}, LEGEND.flatMap(([term, text]) => [el("dt", {}, term), el("dd", {}, text)])),
    el("p", { class: "hint" }, "Evaluations are evidence about one step's output. They never approve, reject, score or rank anything. Only a person decides a Human Approval."),
  ]);
}
