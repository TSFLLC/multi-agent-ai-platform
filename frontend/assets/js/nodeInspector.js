// The Workflow Studio's right-hand inspector (MA7.6A): configuration forms for
// the selected node or connection. DOM only -- every decision about WHAT to
// write lives in workflowGraph.js (pure, tested); this file wires controls to
// those builders and to the callbacks the page provides.
//
// Agent != Model: an Agent node has an Agent section and a visibly separate
// Model section. Choosing one never changes the other. "Use Agent default" is
// offered only when the chosen Agent Version really carries a usable model
// policy of its own; auto routing (MA8) is never offered.
//
// All user-controlled text (keys, names, labels, model ids, criteria, the Agent's
// instruction text) reaches the DOM as text nodes. innerHTML is never used.

import { el } from "./dom.js";
import { activeVersionsOf, roleLabelFor } from "./agentDirectory.js";
import { boundedResults, filterModels, PRICING_FILTER_ALL } from "./modelFilter.js";
import { uniqueDevelopers } from "./modelDeveloper.js";
import { pricingBadgeClass, pricingLabel } from "./format.js";
import {
  NODE_TYPES,
  agentConfig,
  approvalConfig,
  evaluationConfig,
  incomingEdges,
  isSupportedType,
  nodeHint,
  outgoingEdges,
  overrideModelId,
  typeLabel,
  unsupportedApprovalKeys,
  versionHasUsableModel,
} from "./workflowGraph.js";

const PRICING_TABS = [
  { value: "free", label: "FREE" },
  { value: "paid", label: "PAID" },
  { value: "unknown", label: "UNKNOWN" },
  { value: PRICING_FILTER_ALL, label: "ALL" },
];
const MODEL_PAGE = 8;

function section(title, children, { hint = null, cls = "" } = {}) {
  return el("div", { class: `studio-section ${cls}`.trim() }, [
    el("h3", {}, title),
    hint ? el("p", { class: "hint" }, hint) : null,
    ...children,
  ]);
}

function field(label, control) {
  return el("div", { class: "studio-field" }, [el("label", {}, label), control]);
}

function select(options, { disabled = false, onchange } = {}) {
  return el(
    "select",
    { disabled, onchange: onchange ? (event) => onchange(event.target.value) : null },
    options.map((option) => el("option", { value: option.value, selected: Boolean(option.selected) }, option.label))
  );
}

// -- Agent / evaluator agent picker (Agent + Role/version, no model) ------------------------------------

function buildAgentPicker(ctx, { label, versionId, onPick }) {
  const current = ctx.versionIndex.get(versionId) || null;
  const entries = ctx.agentCatalog.filter(
    (entry) => activeVersionsOf(entry).length > 0 || (current && current.agent.id === entry.agent.id)
  );
  const agentSelect = select(
    [
      { value: "", label: "— Choose an Agent —", selected: !current },
      ...entries.map((entry) => ({ value: entry.agent.id, label: entry.agent.name, selected: Boolean(current) && current.agent.id === entry.agent.id }) ),
    ],
    {
      disabled: !ctx.editable,
      onchange: (agentId) => {
        const entry = entries.find((candidate) => candidate.agent.id === agentId);
        if (!entry) return;
        const first = activeVersionsOf(entry)[0];
        if (first) onPick(first.id);
      },
    }
  );

  const controls = [field(label, agentSelect)];
  if (current) {
    const entry = ctx.agentCatalog.find((candidate) => candidate.agent.id === current.agent.id);
    const versions = entry ? activeVersionsOf(entry) : [];
    if (!versions.some((v) => v.id === current.version.id)) versions.push(current.version);
    controls.push(
      field(
        "Role / version",
        select(
          versions.map((version) => ({
            value: version.id,
            label: `${roleLabelFor(version)} · v${version.version}${version.status === "active" ? "" : ` (${version.status})`}`,
            selected: version.id === current.version.id,
          })),
          { disabled: !ctx.editable, onchange: (id) => onPick(id) }
        )
      )
    );
  }
  return controls;
}

// -- read-only prompt preview ------------------------------------------------------------------------------

function buildPromptPreview(ctx, resolved) {
  const body = el("pre", { class: "studio-prompt" }, "Open to load.");
  const details = el("details", { class: "studio-prompt-details" }, [
    el("summary", {}, "Agent instructions (read-only)"),
    body,
    el("p", { class: "hint" }, "Instructions belong to the Agent version. They are not editable per workflow step."),
  ]);
  let loaded = false;
  details.addEventListener("toggle", async () => {
    if (!details.open || loaded) return;
    loaded = true;
    try {
      let versions = ctx.ui.promptCache.get(resolved.agent.id);
      if (!versions) {
        versions = await ctx.loadPromptVersions(resolved.agent.id);
        ctx.ui.promptCache.set(resolved.agent.id, versions);
      }
      const prompt = versions.find((candidate) => candidate.id === resolved.version.prompt_version_id);
      body.textContent = prompt && prompt.content ? prompt.content : "No instructions are recorded for this version.";
    } catch (err) {
      body.textContent = (err && err.message) || "Could not load the instructions.";
    }
  });
  return details;
}

// -- Model section (separate from the Agent) --------------------------------------------------------------

function buildModelPicker(ctx, uiKey, chosenId, onChoose) {
  const ui = (ctx.ui.picker[uiKey] = ctx.ui.picker[uiKey] || { query: "", pricing: "free", developer: "", visible: MODEL_PAGE });
  const developers = uniqueDevelopers(ctx.models);
  const developerKeys = ui.developer ? new Set((developers.find((entry) => entry.label === ui.developer) || { keys: [] }).keys) : null;
  const filtered = filterModels(ctx.models, { query: ui.query, pricing: ui.pricing, developerKeys });
  const { visible, remaining } = boundedResults(filtered, ui.visible);

  const search = el("input", {
    type: "text",
    placeholder: "Search models…",
    value: ui.query,
    "aria-label": "Search models",
    onchange: (event) => {
      ui.query = event.target.value;
      ui.visible = MODEL_PAGE;
      ctx.redraw();
    },
  });
  const developerSelect = select(
    [{ value: "", label: "All developers", selected: !ui.developer }, ...developers.map((entry) => ({ value: entry.label, label: `${entry.label} (${entry.count})`, selected: entry.label === ui.developer }))],
    {
      onchange: (value) => {
        ui.developer = value;
        ui.visible = MODEL_PAGE;
        ctx.redraw();
      },
    }
  );
  const pills = el(
    "div",
    { class: "filter-pills" },
    PRICING_TABS.map((tab) =>
      el(
        "button",
        {
          type: "button",
          class: `filter-pill${ui.pricing === tab.value ? " active" : ""}`,
          onclick: () => {
            ui.pricing = tab.value;
            ui.visible = MODEL_PAGE;
            ctx.redraw();
          },
        },
        tab.label
      )
    )
  );

  const rows = visible.map((model) =>
    el(
      "button",
      {
        type: "button",
        class: `studio-model-option${model.id === chosenId ? " checked" : ""}`,
        "aria-pressed": model.id === chosenId ? "true" : "false",
        onclick: () => onChoose(model.id),
      },
      [
        el("span", { class: "model-name" }, model.canonical_model_id),
        el("span", { class: "model-meta" }, [
          el("span", { class: pricingBadgeClass(model.pricing_classification) }, pricingLabel(model.pricing_classification)),
          model.context_window ? ` ${model.context_window.toLocaleString("en-US")} ctx` : "",
        ]),
      ]
    )
  );

  return el("div", { class: "studio-model-picker" }, [
    el("div", { class: "studio-picker-filters" }, [search, developerSelect]),
    pills,
    filtered.length
      ? el("div", { class: "studio-model-list" }, rows)
      : el("p", { class: "hint" }, "No models match. Try ALL pricing, or refresh the catalog on the Models page."),
    remaining > 0
      ? el(
          "button",
          {
            type: "button",
            class: "small",
            onclick: () => {
              ui.visible += MODEL_PAGE;
              ctx.redraw();
            },
          },
          `Show ${Math.min(MODEL_PAGE, remaining)} more (${remaining} not shown)`
        )
      : null,
  ]);
}

function buildModelSection(ctx, { title, uiKey, config, overrideKey, resolvedVersion, defaultLabel, onChoose }) {
  const chosenId = overrideModelId(config, overrideKey);
  const hasDefault = versionHasUsableModel(resolvedVersion);
  const stored = ctx.ui.modelMode[uiKey];
  const mode = chosenId ? "choose" : stored || (hasDefault ? "default" : "choose");
  const name = `model-source-${uiKey}`;
  const radio = (value, label, { disabled = false } = {}) =>
    el("label", { class: `studio-radio${disabled ? " disabled" : ""}` }, [
      el("input", {
        type: "radio",
        name,
        checked: mode === value,
        disabled: disabled || !ctx.editable,
        onchange: () => {
          ctx.ui.modelMode[uiKey] = value;
          if (value === "default") onChoose(null);
          else ctx.redraw();
        },
      }),
      ` ${label}`,
    ]);

  const chosen = chosenId ? ctx.modelsById.get(chosenId) : null;
  const children = [
    radio("default", hasDefault ? defaultLabel : `${defaultLabel} (this Agent has none)`, { disabled: !hasDefault }),
    radio("choose", "Choose a model"),
  ];
  if (chosenId) {
    children.push(
      el("div", { class: "studio-chosen-model" }, [
        el("span", { class: "hint" }, "Chosen model: "),
        el("strong", {}, chosen ? chosen.canonical_model_id : "(model no longer in the catalog)"),
        chosen ? el("span", { class: pricingBadgeClass(chosen.pricing_classification) }, pricingLabel(chosen.pricing_classification)) : null,
      ])
    );
  }
  if (mode === "choose" && ctx.editable) children.push(buildModelPicker(ctx, uiKey, chosenId, onChoose));
  return section(title, children, {
    cls: "studio-model-section",
    hint: "The intelligence used on this step. Agents and models are chosen independently.",
  });
}

// -- per-type forms -------------------------------------------------------------------------------------------

function buildAgentForm(ctx) {
  const { node } = ctx;
  const resolved = ctx.versionIndex.get(node.config.agent_version_id) || null;
  const save = (next) => ctx.onConfig(node.id, next);
  const sections = [
    section("Agent", buildAgentPicker(ctx, { label: "Agent", versionId: node.config.agent_version_id, onPick: (id) => save(agentConfig(node.config, { agentVersionId: id })) }), {
      hint: "Who does this step: an AI team member with a role.",
    }),
  ];
  if (resolved) sections.push(buildPromptPreview(ctx, resolved));
  sections.push(
    buildModelSection(ctx, {
      title: "Model",
      uiKey: `${node.id}:agent`,
      config: node.config,
      overrideKey: "model_policy_override",
      resolvedVersion: resolved && resolved.version,
      defaultLabel: "Use Agent default",
      onChoose: (modelId) => save(agentConfig(node.config, { modelProviderModelId: modelId })),
    })
  );
  return sections;
}

function buildEvaluationForm(ctx) {
  const { node } = ctx;
  const config = node.config;
  const save = (next) => ctx.onConfig(node.id, next);
  const definitionEntries = ctx.definitionCatalog.filter((entry) => entry.versions.some((v) => v.status === "active"));
  const currentDefinition = ctx.definitionIndex.get(config.evaluation_definition_version_id) || null;
  const activeOf = (entry) => entry.versions.filter((v) => v.status === "active").sort((a, b) => b.version - a.version);

  const definitionChildren = [
    field(
      "Evaluation Definition",
      select(
        [
          { value: "", label: "— Choose an Evaluation Definition —", selected: !currentDefinition },
          ...definitionEntries.map((entry) => ({
            value: entry.definition.id,
            label: entry.definition.name,
            selected: Boolean(currentDefinition) && currentDefinition.definition.id === entry.definition.id,
          })),
        ],
        {
          disabled: !ctx.editable,
          onchange: (definitionId) => {
            const entry = definitionEntries.find((candidate) => candidate.definition.id === definitionId);
            const latest = entry && activeOf(entry)[0];
            if (latest) save(evaluationConfig(config, { definitionVersionId: latest.id }));
          },
        }
      )
    ),
  ];
  if (currentDefinition) {
    const entry = ctx.definitionCatalog.find((candidate) => candidate.definition.id === currentDefinition.definition.id);
    const versions = entry ? activeOf(entry) : [];
    if (!versions.some((v) => v.id === currentDefinition.version.id)) versions.push(currentDefinition.version);
    definitionChildren.push(
      field(
        "Version",
        select(
          versions.map((version) => ({
            value: version.id,
            label: `v${version.version}${version.status === "active" ? "" : ` (${version.status})`}`,
            selected: version.id === currentDefinition.version.id,
          })),
          { disabled: !ctx.editable, onchange: (id) => save(evaluationConfig(config, { definitionVersionId: id })) }
        )
      )
    );
    const criteria = currentDefinition.version.criteria || [];
    if (criteria.length) {
      definitionChildren.push(
        el("div", { class: "studio-criteria" }, [
          el("span", { class: "hint" }, "Criteria assessed:"),
          el("ul", {}, criteria.map((criterion) => el("li", {}, criterion.label || criterion.key))),
        ])
      );
    }
  }

  const evaluator = ctx.versionIndex.get(config.evaluator_agent_version_id) || null;
  return [
    el("p", { class: "hint studio-note" }, "An Evaluation gives structured evidence about ONE upstream Agent's output. It never approves, rejects, scores or ranks anything."),
    section("Evaluation Definition", definitionChildren, { hint: "The rubric: what quality is assessed." }),
    section(
      "Evaluator Agent",
      buildAgentPicker(ctx, { label: "Evaluator Agent", versionId: config.evaluator_agent_version_id, onPick: (id) => save(evaluationConfig(config, { evaluatorAgentVersionId: id })) }),
      { hint: "The AI team member who assesses the output against the rubric." }
    ),
    buildModelSection(ctx, {
      title: "Evaluator Model",
      uiKey: `${node.id}:evaluator`,
      config,
      overrideKey: "evaluator_model_policy_override",
      resolvedVersion: evaluator && evaluator.version,
      defaultLabel: "Use evaluator Agent default",
      onChoose: (modelId) => save(evaluationConfig(config, { evaluatorModelProviderModelId: modelId })),
    }),
  ];
}

function buildApprovalForm(ctx) {
  const { node } = ctx;
  const label = String((node.config || {}).approval_group || "");
  const extras = unsupportedApprovalKeys(node.config);
  const input = el("input", {
    type: "text",
    value: label,
    disabled: !ctx.editable,
    maxlength: "120",
    "aria-label": "Approval label",
    onchange: (event) => ctx.onConfig(node.id, approvalConfig(event.target.value)),
  });
  return [
    section(
      "Approval label",
      [
        field("Label", input),
        el("p", { class: "hint" }, "This is a label shown with the approval request. It does not restrict who can decide: any project member with edit access can approve or reject."),
      ],
      { hint: "A person decides here. Always." }
    ),
    el(
      "p",
      { class: "hint studio-note" },
      "There is no automatic approval, no timeout decision, no AI approver and no score threshold. The workflow waits until a person decides."
    ),
    extras.length
      ? el("p", { class: "error-banner" }, `This node has settings the Studio does not support (${extras.join(", ")}). Saving the label replaces them.`)
      : null,
  ];
}

// -- connections -----------------------------------------------------------------------------------------------------

function buildConnections(ctx) {
  const { node, graph } = ctx;
  const byId = new Map(graph.nodes.map((candidate) => [candidate.id, candidate]));
  const list = (edges, otherOf, emptyText) =>
    edges.length
      ? el(
          "ul",
          { class: "studio-connection-list" },
          edges.map((edge) => {
            const other = byId.get(otherOf(edge));
            return el("li", {}, [
              el("button", { type: "button", class: "link", onclick: () => ctx.onSelectNode(other && other.id) }, other ? other.key : "(missing node)"),
              ctx.editable
                ? el("button", { type: "button", class: "small", "aria-label": `Disconnect ${other ? other.key : "node"}`, onclick: () => ctx.onDisconnect(edge.id) }, "Disconnect")
                : null,
            ]);
          })
        )
      : el("p", { class: "hint" }, emptyText);

  const incoming = incomingEdges(graph, node.id);
  const outgoing = outgoingEdges(graph, node.id);
  const children = [
    el("h4", {}, "Receives from"),
    list(incoming, (edge) => edge.from, "Nothing feeds this node."),
    el("h4", {}, "Sends to"),
    list(outgoing, (edge) => edge.to, "This node feeds nothing."),
  ];

  if (ctx.editable) {
    const connectRow = (labelText, candidates, onPick) => {
      let chosen = "";
      const picker = select(
        [{ value: "", label: labelText, selected: true }, ...candidates.map((candidate) => ({ value: candidate.id, label: candidate.key }))],
        { onchange: (value) => { chosen = value; } }
      );
      return el("div", { class: "row studio-connect-row" }, [
        picker,
        el("button", { type: "button", class: "small", onclick: () => chosen && onPick(chosen) }, "Connect"),
      ]);
    };
    const selectable = graph.nodes.filter((candidate) => candidate.id !== node.id && isSupportedType(candidate.type));
    const targets = selectable.filter((candidate) => !outgoing.some((edge) => edge.to === candidate.id)).sort((a, b) => a.key.localeCompare(b.key));
    const sources = selectable.filter((candidate) => !incoming.some((edge) => edge.from === candidate.id)).sort((a, b) => a.key.localeCompare(b.key));
    children.push(
      connectRow("Connect to…", targets, (targetId) => ctx.onConnect(node.id, targetId)),
      connectRow("Connect from…", sources, (sourceId) => ctx.onConnect(sourceId, node.id))
    );
  }
  return section("Connections", children, { hint: "Prefer not to use the diagram? Connect and disconnect from these lists." });
}

// -- public builders -------------------------------------------------------------------------------------------------

export function buildInspector(ctx) {
  const { node } = ctx;
  const supported = isSupportedType(node.type);
  const hint = supported ? nodeHint(node, ctx) : null;
  const parts = [
    el("div", { class: "studio-inspector-head" }, [
      el("span", { class: `badge studio-type-${node.type}` }, typeLabel(node.type)),
      el("h2", {}, node.key),
    ]),
  ];
  if (!ctx.editable) parts.push(el("p", { class: "hint studio-note" }, "This version is read-only."));
  if (!supported) {
    parts.push(
      el("p", { class: "error-banner" }, `${typeLabel(node.type)} nodes are not supported by this Studio. The node is shown read-only and is never changed automatically. This workflow cannot be run from here.`),
      el("pre", { class: "studio-raw" }, JSON.stringify(node.config || {}, null, 2))
    );
    return el("div", { class: "studio-inspector-body" }, parts);
  }
  if (hint) parts.push(el("p", { class: "studio-attention" }, `⚠ ${hint}`));

  if (node.type === NODE_TYPES.AGENT) parts.push(...buildAgentForm(ctx));
  else if (node.type === NODE_TYPES.EVALUATION) parts.push(...buildEvaluationForm(ctx));
  else if (node.type === NODE_TYPES.APPROVAL) parts.push(...buildApprovalForm(ctx));
  else parts.push(el("p", { class: "hint" }, "This is where the workflow finishes. It needs no configuration."));

  parts.push(buildConnections(ctx));

  if (ctx.editable) {
    const connections = incomingEdges(ctx.graph, node.id).length + outgoingEdges(ctx.graph, node.id).length;
    if (ctx.ui.confirmDeleteNodeId === node.id) {
      parts.push(
        el("div", { class: "studio-confirm" }, [
          el("p", {}, `Delete ${node.key}${connections ? ` and its ${connections} connection${connections === 1 ? "" : "s"}` : ""}?`),
          el("div", { class: "row" }, [
            el("button", { type: "button", class: "danger small", onclick: () => ctx.onDelete(node.id) }, "Delete"),
            el(
              "button",
              {
                type: "button",
                class: "small",
                onclick: () => {
                  ctx.ui.confirmDeleteNodeId = null;
                  ctx.redraw();
                },
              },
              "Cancel"
            ),
          ]),
        ])
      );
    } else {
      parts.push(
        el(
          "button",
          {
            type: "button",
            class: "danger small",
            onclick: () => {
              ctx.ui.confirmDeleteNodeId = node.id;
              ctx.redraw();
            },
          },
          "Delete node"
        )
      );
    }
  }
  return el("div", { class: "studio-inspector-body" }, parts);
}

export function buildEdgeInspector(ctx) {
  const { edge, graph } = ctx;
  const from = graph.nodes.find((candidate) => candidate.id === edge.from);
  const to = graph.nodes.find((candidate) => candidate.id === edge.to);
  return el("div", { class: "studio-inspector-body" }, [
    el("div", { class: "studio-inspector-head" }, [el("span", { class: "badge badge-neutral" }, "Connection"), el("h2", {}, `${from ? from.key : "?"} → ${to ? to.key : "?"}`)]),
    el("p", { class: "hint" }, "Work flows from the first node to the second. A node starts only when everything feeding it has finished."),
    el("div", { class: "row" }, [
      from ? el("button", { type: "button", class: "small", onclick: () => ctx.onSelectNode(from.id) }, `Select ${from.key}`) : null,
      to ? el("button", { type: "button", class: "small", onclick: () => ctx.onSelectNode(to.id) }, `Select ${to.key}`) : null,
    ]),
    ctx.editable
      ? el("button", { type: "button", class: "danger small", onclick: () => ctx.onDisconnect(edge.id) }, "Disconnect")
      : el("p", { class: "hint studio-note" }, "This version is read-only."),
  ]);
}

