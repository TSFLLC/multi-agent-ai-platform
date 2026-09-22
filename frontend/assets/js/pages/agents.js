// Agents — read-only in V1 (Section 9: "editing would materially expand
// scope"). Shows the real Agent Registry so an operator can see what's
// available without ever touching a UUID directly.
//
// MA7.7D: rows are now clickable, opening a read-only Agent Detail view
// (name/role/description, the active version's full responsibility/
// instructions and configuration, and version history). Still no editor
// -- "Agent = team member, Responsibility = job description, Agent
// Version = version of job definition, Model = interchangeable
// brain/resource" (Section 12).

import { api } from "../api.js";
import { el, mount } from "../dom.js";
import { getProjectId } from "../state.js";
import { openModal } from "../modal.js";
import {
  selectActiveVersion,
  sortVersionsDescending,
  resolvePromptContent,
  formatList,
  formatJson,
  formatToolGrants,
} from "../agentDetailView.js";

export async function renderAgents(root) {
  mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading agents…"]));

  let agents;
  try {
    const projectId = await getProjectId();
    agents = await api.get(`/agents?project_id=${encodeURIComponent(projectId)}`);
  } catch (err) {
    mount(root, el("div", { class: "error-banner" }, err.message || "Failed to load agents."));
    return;
  }

  const container = el("div", { class: "stack" });
  container.appendChild(
    el("div", { class: "page-header" }, [
      el("h1", {}, "Agents"),
      el("div", { class: "subtitle" }, "The Agent Registry for this project (read-only in this console)."),
    ])
  );

  if (!agents.length) {
    container.appendChild(el("div", { class: "card" }, el("p", {}, "No agents exist yet.")));
    mount(root, container);
    return;
  }

  const table = el("table", {}, [
    el("thead", {}, el("tr", {}, [el("th", {}, "Name"), el("th", {}, "Role"), el("th", {}, "Status")])),
    el(
      "tbody",
      {},
      agents.map((a) =>
        el(
          "tr",
          {
            class: "clickable",
            tabindex: "0",
            role: "button",
            "aria-label": `View details for ${a.name}`,
            onClick: () => openAgentDetail(a.id),
            onKeydown: (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openAgentDetail(a.id);
              }
            },
          },
          [
            el("td", {}, a.name),
            el("td", {}, a.role),
            el("td", {}, el("span", { class: statusBadgeClass(a.current_status) }, a.current_status || "draft")),
          ]
        )
      )
    ),
  ]);

  container.appendChild(el("div", { class: "card" }, table));
  mount(root, container);
}

async function openAgentDetail(agentId) {
  const panel = el("div", { class: "stack" }, [el("span", { class: "spinner" }), " Loading agent…"]);
  openModal(panel, { label: "Agent detail", size: "large" });

  let agent, versions, promptVersions;
  try {
    [agent, versions, promptVersions] = await Promise.all([
      api.get(`/agents/${agentId}`),
      api.get(`/agents/${agentId}/versions`),
      api.get(`/agents/${agentId}/prompt-versions`),
    ]);
  } catch (err) {
    mount(panel, el("div", { class: "error-banner" }, err.message || "Failed to load agent."));
    return;
  }

  mount(panel, buildAgentDetail(agent, versions, promptVersions));
}

function buildAgentDetail(agent, versions, promptVersions) {
  const sorted = sortVersionsDescending(versions);
  const active = selectActiveVersion(versions);

  const children = [
    el("div", { class: "page-header" }, [
      el("h2", {}, agent.name),
      el("div", { class: "subtitle" }, agent.role),
    ]),
  ];

  if (agent.description) {
    children.push(el("p", {}, agent.description));
  }

  if (active) {
    const activePrompt = resolvePromptContent(promptVersions, active.prompt_version_id);
    children.push(
      el("div", { class: "card" }, [
        el("div", { class: "row between" }, [
          el("strong", {}, `Active Version: v${active.version}`),
          el("span", { class: statusBadgeClass(active.status) }, active.status),
        ]),
        el("h3", {}, "Responsibility / Instructions"),
        el("pre", { class: "run-output" }, activePrompt || "Not configured"),
        el("h3", {}, "Configuration"),
        kv("Capabilities", formatList(active.capabilities)),
        kv("Model Policy", formatJson(active.model_policy)),
        kv("Default Model Strategy", active.default_model_strategy),
        kv("Context Policy", formatJson(active.context_policy)),
        kv("Memory Policy", formatJson(active.memory_policy)),
        kv("Budget Policy", formatJson(active.budget_policy)),
        kv("Retry Policy", formatJson(active.retry_policy)),
        kv("Approval Requirements", formatJson(active.approval_requirements)),
        kv("Timeout", active.timeout_seconds != null ? `${active.timeout_seconds}s` : null),
        kv("Tool Grants", formatToolGrants(active.tool_grants)),
      ])
    );
  } else {
    children.push(el("div", { class: "card" }, el("p", {}, "No published version yet.")));
  }

  children.push(
    el("div", { class: "card" }, [
      el("h3", {}, "Version History"),
      el(
        "ul",
        {},
        sorted.map((v) =>
          el("li", {}, [
            `v${v.version} — `,
            el("span", { class: statusBadgeClass(v.status) }, v.status),
            v.id === (active && active.id) ? null : el("span", { class: "hint" }, "  (historical, read-only)"),
          ])
        )
      ),
    ])
  );

  return el("div", { class: "stack" }, children);
}

function kv(label, value) {
  return el("div", { class: "run-kv" }, [
    el("span", { class: "hint run-kv-label" }, label),
    el("span", { class: "run-kv-value" }, value === null || value === undefined || value === "" ? "Not configured" : value),
  ]);
}

function statusBadgeClass(status) {
  if (status === "active") return "badge badge-done";
  if (status === "retired" || status === "deprecated") return "badge badge-neutral";
  return "badge badge-unknown";
}
