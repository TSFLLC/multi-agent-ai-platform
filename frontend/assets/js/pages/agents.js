// Agents — read-only in V1 (Section 9: "editing would materially expand
// scope"). Shows the real Agent Registry so an operator can see what's
// available without ever touching a UUID directly.

import { api } from "../api.js";
import { el, mount } from "../dom.js";
import { getProjectId } from "../state.js";

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
        el("tr", {}, [
          el("td", {}, a.name),
          el("td", {}, a.role),
          el("td", {}, el("span", { class: statusBadgeClass(a.current_status) }, a.current_status || "draft")),
        ])
      )
    ),
  ]);

  container.appendChild(el("div", { class: "card" }, table));
  mount(root, container);
}

function statusBadgeClass(status) {
  if (status === "active") return "badge badge-done";
  if (status === "retired" || status === "deprecated") return "badge badge-neutral";
  return "badge badge-unknown";
}
