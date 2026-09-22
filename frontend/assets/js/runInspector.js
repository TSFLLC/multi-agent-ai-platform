// The Control Room's inspector (MA7.6B): what happened at the selected node.
//
// A step's Agent and Model are shown in separate sections (Agent != Model).
// Usage and cost come from the run snapshot (summed from model_calls on the
// server); nothing is estimated or invented here. An Evaluation node shows its
// findings as EVIDENCE -- MET / PARTIAL / NOT MET / NOT APPLICABLE per criterion,
// with no score, ranking, winner, verdict or recommendation. A Human Approval node
// shows the full bound evidence and, only while it is pending, Approve / Reject
// with an explicit confirmation: nothing here (or anywhere) decides for a person.
//
// All user-controlled text (keys, names, rationales, notes, artifact contents) is
// set through textContent. innerHTML is never used.
//
// A failed AGENT step (in a FAILED run) offers "Retry with a different model" (MA7.6B):
// a run/attempt-level model override only -- the published workflow is never edited, and the
// original failed attempt is kept as immutable history alongside the new one. Attempt history
// (every iteration of a step, oldest first) is shown once a step has been retried.
//
// ctx: {
//   node, detail, nowMs,
//   cache: Map, ui: { openOutputs:Set, openEvidence:Set, decision:null|{...}, retry:null|{...} },
//   redraw(), onSelectNodeKey(key),
//   actions: {
//     startDecision(approvalId, kind, fingerprint), cancelDecision(), setNote(text), confirmDecision(),
//     startRetry(nodeRunId), setRetryModel(providerModelId), cancelRetry(), confirmRetry(),
//   }
// }

import { el } from "./dom.js";
import { formatDateTime } from "./format.js";
import { typeLabel } from "./workflowGraph.js";
import { workflowApi } from "./workflowApi.js";
import { normalizeApiError } from "./workflowErrors.js";
import {
  approvalStatusInfo,
  costLabel,
  findingCounts,
  findingInfo,
  formatDuration,
  formatTokenCount,
  nodeState,
  secondsBetween,
  shortId,
} from "./runState.js";

function kv(label, value, { title = null } = {}) {
  return el("div", { class: "run-kv" }, [el("span", { class: "hint run-kv-label" }, label), el("span", { class: "run-kv-value", title }, value == null || value === "" ? "—" : String(value))]);
}

function section(title, children, { hint = null, cls = "" } = {}) {
  return el("div", { class: `studio-section ${cls}`.trim() }, [el("h3", {}, title), hint ? el("p", { class: "hint" }, hint) : null, ...children]);
}

// A tiny read-through cache: returns the entry now, and redraws when the fetch settles.
function load(ctx, key, fetcher) {
  const existing = ctx.cache.get(key);
  if (existing) return existing;
  const entry = { state: "loading", value: null, error: null };
  ctx.cache.set(key, entry);
  Promise.resolve()
    .then(fetcher)
    .then((value) => {
      entry.state = "ready";
      entry.value = value;
    })
    .catch((err) => {
      entry.state = "error";
      entry.error = normalizeApiError(err).message;
    })
    .then(() => ctx.redraw());
  return entry;
}

function loading(text = "Loading…") {
  return el("p", { class: "hint" }, [el("span", { class: "spinner" }), ` ${text}`]);
}

function errorLine(message) {
  return el("p", { class: "error-banner" }, message || "Could not load this.");
}

// -- shared node sections ----------------------------------------------------------------------------------

function buildTiming(ctx) {
  const { node, nowMs } = ctx;
  const live = !node.ended_at && node.started_at ? secondsBetween(node.started_at, null, nowMs) : null;
  const duration = node.duration_seconds != null ? node.duration_seconds : live;
  return section("Timing", [
    kv("Started", node.started_at ? formatDateTime(node.started_at) : "Not started"),
    kv("Ended", node.ended_at ? formatDateTime(node.ended_at) : node.started_at ? "Still running" : "—"),
    kv("Duration", duration != null ? `${formatDuration(duration)}${node.ended_at ? "" : " so far"}` : "—"),
  ]);
}

function buildAgentAndModel(ctx) {
  const { node } = ctx;
  const agent = node.agent;
  const model = agent && agent.model;
  return [
    section(
      "Agent",
      agent
        ? [
            kv("Agent", agent.agent_name),
            kv("Version", agent.agent_version != null ? `v${agent.agent_version}` : "—"),
            kv("Role", agent.role),
            kv("Agent run", `${shortId(agent.agent_run_id)} · ${agent.status}`, { title: agent.agent_run_id }),
          ]
        : [el("p", { class: "hint" }, "No Agent has been assigned to this step yet.")],
      { hint: "Who did (or will do) this step." }
    ),
    section(
      "Model",
      model
        ? [
            kv("Model", model.canonical_model_id),
            kv("Provider", model.provider_name),
            kv("Pricing snapshot", shortId(model.provider_model_snapshot_id), { title: model.provider_model_snapshot_id }),
            agent && agent.agent_run_id
              ? el("a", { href: `#/model-intelligence/agent-runs/${encodeURIComponent(agent.agent_run_id)}` }, "Why this model?")
              : null,
          ]
        : [el("p", { class: "hint" }, "No model has been resolved yet. It is chosen when the step starts.")],
      { hint: "The intelligence the Agent used. Chosen separately from the Agent.", cls: "studio-model-section" }
    ),
  ];
}

function buildUsage(ctx) {
  const usage = ctx.node.usage;
  if (!usage) return section("Usage and cost", [el("p", { class: "hint" }, "No model calls yet, so no usage or cost is recorded.")]);
  const cost = costLabel(usage);
  const children = [
    kv("Tokens in", formatTokenCount(usage.tokens_in)),
    kv("Tokens out", formatTokenCount(usage.tokens_out)),
    kv("Total tokens", formatTokenCount(usage.total_tokens)),
    kv("Cost", cost.text),
    kv("Model calls", usage.model_call_count),
  ];
  if (cost.estimated) children.push(el("p", { class: "hint" }, "This cost is an estimate from the model's published price, not a figure reported by the provider."));
  return section("Usage and cost", children);
}

function buildFailure(ctx) {
  const failure = ctx.node.failure;
  if (!failure) return null;
  const origin = { agent_run: "The Agent's run reported", evaluation: "The evaluation reported", workflow: "The workflow reported" }[failure.source] || "Reported";
  return el("div", { class: "error-banner run-failure", role: "alert" }, [
    el("strong", {}, "Why this step failed"),
    el("div", {}, failure.message ? `${origin}: ${failure.category ? `${failure.category} — ` : ""}${failure.message}` : `${origin} a failure without a message.`),
  ]);
}

// -- failed-step recovery (MA7.6B) -------------------------------------------------------------------------

function retryEligible(ctx) {
  const { node, detail } = ctx;
  return node.node_type === "agent" && node.status === "failed" && detail.status === "failed";
}

function modelLabel(model) {
  if (!model) return null;
  const bits = [model.canonical_model_id, model.pricing_classification];
  if (model.context_window) bits.push(`${model.context_window.toLocaleString("en-US")} ctx`);
  return bits.filter(Boolean).join(" · ");
}

function buildRetryPicker(ctx) {
  const { ui, node, actions } = ctx;
  const retry = ui.retry && ui.retry.nodeRunId === node.node_run_id ? ui.retry : null;

  if (!retry) {
    return [
      el("p", { class: "hint" }, "This Agent step failed. You can retry it with a different, currently active model. Nothing already completed is re-run."),
      el("button", { type: "button", class: "primary", onclick: () => actions.startRetry(node.node_run_id) }, "Retry with a different model…"),
    ];
  }

  const modelsEntry = load(ctx, "models:catalog", () => workflowApi.listModels());
  if (modelsEntry.state === "loading") return [loading("Loading available models…")];
  if (modelsEntry.state === "error") return [errorLine(modelsEntry.error)];

  const active = (modelsEntry.value || []).filter((model) => model.model_status === "active");
  const options = [
    { value: "", label: active.length ? "— Choose a replacement model —" : "No active models are available", selected: !retry.providerModelId },
    ...active.map((model) => ({ value: model.id, label: modelLabel(model), selected: model.id === retry.providerModelId })),
  ];

  return [
    el("p", { class: "hint" }, "Choose a replacement model. The published workflow is never changed -- this applies to this retry attempt only."),
    el(
      "select",
      {
        "aria-label": "Replacement model",
        disabled: retry.busy,
        onchange: (event) => actions.setRetryModel(event.target.value),
      },
      options.map((option) => el("option", { value: option.value, selected: option.selected }, option.label))
    ),
    retry.error ? el("div", { class: "error-banner", role: "alert" }, retry.error) : null,
    el("div", { class: "row" }, [
      el(
        "button",
        { type: "button", class: "primary", disabled: retry.busy || !retry.providerModelId, onclick: () => actions.confirmRetry() },
        retry.busy ? "Retrying…" : "Retry with this model"
      ),
      el("button", { type: "button", disabled: retry.busy, onclick: () => actions.cancelRetry() }, "Cancel"),
    ]),
  ];
}

function buildRetry(ctx) {
  if (!retryEligible(ctx)) return null;
  return section("Retry this step", buildRetryPicker(ctx));
}

function attemptRow(ctx, attempt) {
  const { node } = ctx;
  const isCurrent = attempt.id === node.node_run_id;
  const label = attempt.iteration === 0 ? "Original attempt" : `Retry attempt ${attempt.iteration}`;
  const children = [
    el("div", { class: "row between" }, [
      el("strong", {}, label),
      el("span", { class: `badge run-tone-${attempt.status === "failed" ? "failed" : attempt.status === "completed" ? "done" : "active"}` }, attempt.status),
    ]),
  ];
  if (attempt.agent_run_id) children.push(buildAttemptAgentRun(ctx, attempt.agent_run_id));
  if (!isCurrent) children.push(el("p", { class: "hint" }, "Superseded -- kept as immutable history."));
  return el("li", { class: "run-evidence-row" }, children);
}

function buildAttemptAgentRun(ctx, agentRunId) {
  const runEntry = load(ctx, `agentRun:${agentRunId}`, () => workflowApi.getAgentRun(agentRunId));
  const attemptsEntry = load(ctx, `agentRunAttempts:${agentRunId}`, () => workflowApi.getAgentRunAttempts(agentRunId));
  const modelsEntry = load(ctx, "models:catalog", () => workflowApi.listModels());
  if (runEntry.state === "loading" || attemptsEntry.state === "loading") return loading("Loading attempt…");
  if (runEntry.state === "error") return errorLine(runEntry.error);
  const agentRun = runEntry.value;
  const models = modelsEntry.state === "ready" ? modelsEntry.value || [] : [];
  const model = agentRun.model_id ? models.find((candidate) => candidate.model_id === agentRun.model_id) : null;
  const lastError = attemptsEntry.state === "ready" ? [...(attemptsEntry.value || [])].reverse().find((a) => a.error) : null;
  return el("div", {}, [
    kv("Model", model ? model.canonical_model_id : agentRun.model_id ? shortId(agentRun.model_id) : "not resolved"),
    kv("Agent run", shortId(agentRun.id), { title: agentRun.id }),
    lastError ? el("p", { class: "error-banner" }, `${lastError.error.category ? `${lastError.error.category} — ` : ""}${lastError.error.message || "failed"}`) : null,
  ]);
}

function buildAttemptHistory(ctx) {
  const { node } = ctx;
  if (node.node_type !== "agent" || !node.iteration) return null;
  const entry = load(ctx, `attempts:${node.node_id}`, () => workflowApi.getNodeAttempts(ctx.detail.id, node.node_id));
  const children = [el("p", { class: "hint" }, "Every attempt at this step, oldest first. A retried attempt never mutates the one it replaced.")];
  if (entry.state === "loading") children.push(loading("Loading attempt history…"));
  else if (entry.state === "error") children.push(errorLine(entry.error));
  else children.push(el("ul", { class: "run-evidence" }, (entry.value || []).map((attempt) => attemptRow(ctx, attempt))));
  return section("Attempt history", children);
}

function buildOutput(ctx) {
  const { node, ui } = ctx;
  if (!node.output_artifact_id) return null;
  const id = node.output_artifact_id;
  const open = ui.openOutputs.has(id);
  const children = [
    kv("Artifact", shortId(id), { title: id }),
    el(
      "button",
      {
        type: "button",
        class: "small",
        onclick: () => {
          if (open) ui.openOutputs.delete(id);
          else ui.openOutputs.add(id);
          ctx.redraw();
        },
      },
      open ? "Hide output" : "View output"
    ),
  ];
  if (open) children.push(buildArtifactText(ctx, id));
  return section("Output", children);
}

function buildArtifactText(ctx, artifactId) {
  const entry = load(ctx, `artifact:${artifactId}`, () => workflowApi.getArtifactText(artifactId));
  if (entry.state === "loading") return loading("Loading output…");
  if (entry.state === "error") return errorLine(entry.error);
  return el("pre", { class: "run-output" }, entry.value || "(empty)");
}

// -- evaluation --------------------------------------------------------------------------------------------------

function findingsBlock(evaluation) {
  const results = evaluation.criterion_results || [];
  const counts = findingCounts(results);
  const summary = ["met", "partial", "not_met", "not_applicable"]
    .filter((key) => counts[key] > 0)
    .map((key) => `${counts[key]} ${findingInfo(key).label}`)
    .join(" · ");
  return el("div", { class: "run-findings" }, [
    summary ? el("p", { class: "hint" }, `Findings: ${summary}`) : null,
    results.length
      ? el(
          "ul",
          { class: "run-finding-list" },
          results.map((result) => {
            const info = findingInfo(result.finding);
            const quotes = (result.evidence_refs || []).filter((ref) => ref && typeof ref === "object" && ref.quote);
            return el("li", { class: "run-finding" }, [
              el("div", { class: "row" }, [el("strong", {}, result.criterion_label || result.criterion_key), el("span", { class: `run-finding-pill tone-${info.tone}` }, info.label)]),
              result.rationale ? el("p", { class: "run-rationale" }, result.rationale) : null,
              ...quotes.map((ref) => el("blockquote", { class: "run-quote" }, ref.quote)),
            ]);
          })
        )
      : el("p", { class: "hint" }, "No findings were recorded."),
  ]);
}

function buildEvaluation(ctx) {
  const { node } = ctx;
  if (!node.evaluation_run_id) return null;
  const entry = load(ctx, `eval:${node.evaluation_run_id}:${node.evaluation_status}`, () => workflowApi.getEvaluationRun(node.evaluation_run_id));
  const children = [
    el("p", { class: "hint studio-note" }, "Evaluation is evidence only. It does not approve, reject, score or rank anything, and it never decides the approval."),
  ];
  if (entry.state === "loading") children.push(loading("Loading findings…"));
  else if (entry.state === "error") children.push(errorLine(entry.error));
  else {
    const evaluation = entry.value;
    const definition = evaluation.evaluation_definition;
    const subject = evaluation.subject;
    const subjectNode = ctx.detail.nodes.find((n) => n.agent && n.agent.agent_run_id === subject.agent_run_id);
    children.push(
      kv("Evaluation Definition", definition ? `${definition.evaluation_definition_name} v${definition.evaluation_definition_version}` : "—"),
      kv("Status", evaluation.status),
      kv("Evaluated output of", subject.agent && subject.agent.agent_name ? `${subject.agent.agent_name}${subjectNode ? ` (${subjectNode.node_key})` : ""}` : "—"),
      kv("Subject agent run", shortId(subject.agent_run_id), { title: subject.agent_run_id }),
      kv("Subject artifact", shortId(subject.artifact_id), { title: subject.artifact_id }),
      kv("Artifact hash", shortId(subject.artifact_content_hash), { title: subject.artifact_content_hash })
    );
    if (subjectNode) {
      children.push(el("button", { type: "button", class: "link", onclick: () => ctx.onSelectNodeKey(subjectNode.node_key) }, `Open ${subjectNode.node_key}`));
    }
    if (evaluation.failure_reason) children.push(el("p", { class: "error-banner" }, evaluation.failure_reason));
    children.push(findingsBlock(evaluation));
  }
  return section("Evaluation", children);
}

// -- human approval --------------------------------------------------------------------------------------------

function evidenceRow(ctx, approvalId, entry) {
  const { ui, detail } = ctx;
  const node = detail.nodes.find((candidate) => candidate.node_key === entry.node_key);
  const rowKey = `${approvalId}:${entry.node_key}`;
  const open = ui.openEvidence.has(rowKey);
  const isEvaluation = node && node.node_type === "evaluation";
  const body = [];
  if (open) {
    if (isEvaluation && node.evaluation_run_id) {
      const evaluation = load(ctx, `eval:${node.evaluation_run_id}:${node.evaluation_status}`, () => workflowApi.getEvaluationRun(node.evaluation_run_id));
      if (evaluation.state === "loading") body.push(loading("Loading findings…"));
      else if (evaluation.state === "error") body.push(errorLine(evaluation.error));
      else body.push(findingsBlock(evaluation.value));
    } else if (entry.artifact_id) {
      body.push(buildArtifactText(ctx, entry.artifact_id));
    } else {
      body.push(el("p", { class: "hint" }, "No output is bound for this entry."));
    }
  }
  return el("li", { class: "run-evidence-row" }, [
    el("div", { class: "row between" }, [
      el("div", {}, [
        el("strong", {}, entry.node_key),
        el("span", { class: "hint" }, ` ${node ? typeLabel(node.node_type) : ""}`),
        el("div", { class: "hint run-hash", title: entry.content_hash || "" }, `artifact ${shortId(entry.artifact_id)} · sha256 ${shortId(entry.content_hash)}`),
      ]),
      el("div", { class: "row" }, [
        el("span", { class: `badge ${entry.content_intact ? "badge-done" : "badge-failed"}` }, entry.content_intact ? "✓ intact" : "⚠ changed"),
        el(
          "button",
          {
            type: "button",
            class: "small",
            onclick: () => {
              if (open) ui.openEvidence.delete(rowKey);
              else ui.openEvidence.add(rowKey);
              ctx.redraw();
            },
          },
          open ? "Hide" : isEvaluation ? "Show findings" : "Show output"
        ),
      ]),
    ]),
    ...body,
  ]);
}

function decisionBlock(ctx, approval, evidence) {
  const { ui, actions } = ctx;
  const decision = ui.decision && ui.decision.approvalId === approval.id ? ui.decision : null;
  const evidenceOk = evidence.fingerprint_matches !== false;
  if (!decision) {
    return el("div", { class: "run-decision" }, [
      !evidenceOk
        ? el("p", { class: "error-banner" }, "The evidence changed after this approval was requested. Approving is refused. You can still reject.")
        : null,
      el("div", { class: "row" }, [
        el("button", { type: "button", class: "primary", disabled: !evidenceOk, onclick: () => actions.startDecision(approval.id, "approve", approval.action_fingerprint) }, "Approve…"),
        el("button", { type: "button", class: "danger", onclick: () => actions.startDecision(approval.id, "reject", approval.action_fingerprint) }, "Reject…"),
      ]),
      el("p", { class: "hint" }, "Only you can decide this. No Agent, evaluation finding, timeout or restart can approve or reject it."),
    ]);
  }
  const approving = decision.kind === "approve";
  const note = el("textarea", {
    rows: "3",
    maxlength: "4000",
    disabled: decision.busy,
    placeholder: "Optional note, kept with the decision",
    "aria-label": "Decision note",
    oninput: (event) => actions.setNote(event.target.value),
  });
  note.value = decision.note || "";
  return el("div", { class: "run-decision studio-confirm", role: "group", "aria-label": approving ? "Confirm approval" : "Confirm rejection" }, [
    el("p", {}, el("strong", {}, approving ? "Approve and continue the workflow?" : "Reject and stop the workflow?")),
    el(
      "p",
      { class: "hint" },
      approving
        ? "You are approving the outputs and evaluations listed above. The workflow will continue. Your decision is recorded and cannot be changed."
        : "The workflow will stop and be marked Failed. Finished steps and their evidence are kept. Your decision is recorded and cannot be changed."
    ),
    el("div", {}, [el("label", {}, "Note (optional)"), note]),
    decision.error ? el("div", { class: "error-banner", role: "alert" }, decision.error) : null,
    el("div", { class: "row" }, [
      el("button", { type: "button", class: approving ? "primary" : "danger", disabled: decision.busy, onclick: () => actions.confirmDecision() }, decision.busy ? "Recording…" : approving ? "Yes, approve" : "Yes, reject"),
      el("button", { type: "button", disabled: decision.busy, onclick: () => actions.cancelDecision() }, "Go back"),
    ]),
  ]);
}

function buildApproval(ctx) {
  const { node } = ctx;
  if (!node.approval_id) {
    return section("Human Approval", [el("p", { class: "hint" }, "This step waits for a person once everything feeding it has finished. Nothing decides it automatically.")]);
  }
  const approvalKey = `approval:${node.approval_id}:${node.approval_status}`;
  const approvalEntry = load(ctx, approvalKey, () => workflowApi.getApproval(node.approval_id));
  const evidenceEntry = load(ctx, `evidence:${node.approval_id}:${node.approval_status}`, () => workflowApi.getApprovalEvidence(node.approval_id));
  const children = [];
  const info = approvalStatusInfo(node.approval_status);
  children.push(el("div", { class: `run-approval-banner tone-${info.tone}` }, node.approval_status === "pending" ? "⏳ Waiting for your decision" : info.label));
  if (approvalEntry.state === "loading" || evidenceEntry.state === "loading") {
    children.push(loading("Loading the evidence…"));
    return section("Human Approval", children);
  }
  if (approvalEntry.state === "error" || evidenceEntry.state === "error") {
    children.push(errorLine(approvalEntry.error || evidenceEntry.error));
    return section("Human Approval", children);
  }
  const approval = approvalEntry.value;
  const evidence = evidenceEntry.value;
  if (approval.resolved_at) children.push(kv("Decided", formatDateTime(approval.resolved_at)));
  if (approval.resolution_note) children.push(el("blockquote", { class: "run-quote" }, approval.resolution_note));
  children.push(
    el("h4", {}, `Evidence for this decision (${evidence.evidence.length})`),
    el("p", { class: "hint" }, "Exactly what was bound when this approval was requested, in a fixed order. Each entry's file is re-checked against its recorded hash."),
    el("ul", { class: "run-evidence" }, evidence.evidence.map((entry) => evidenceRow(ctx, approval.id, entry))),
    kv("Evidence fingerprint", shortId(approval.action_fingerprint), { title: approval.action_fingerprint })
  );
  if (approval.status === "pending") children.push(decisionBlock(ctx, approval, evidence));
  return section("Human Approval", children);
}

// -- public ---------------------------------------------------------------------------------------------------------

export function buildRunInspector(ctx) {
  const { node } = ctx;
  const state = nodeState(node);
  const parts = [
    el("div", { class: "studio-inspector-head" }, [el("span", { class: `badge run-tone-${state.tone}` }, state.label), el("h2", {}, node.node_key)]),
    el("p", { class: "hint" }, `${typeLabel(node.node_type)} · ${state.detail}`),
  ];
  parts.push(buildFailure(ctx));
  parts.push(buildRetry(ctx));
  parts.push(buildTiming(ctx));
  if (node.node_type === "human_approval") parts.push(buildApproval(ctx));
  else if (node.node_type === "terminal") parts.push(el("p", { class: "hint" }, "The workflow finishes here."));
  else {
    parts.push(...buildAgentAndModel(ctx));
    parts.push(buildUsage(ctx));
    if (node.node_type === "evaluation") parts.push(buildEvaluation(ctx));
    else parts.push(buildOutput(ctx));
  }
  parts.push(buildAttemptHistory(ctx));
  return el("div", { class: "studio-inspector-body run-inspector-body" }, parts);
}
