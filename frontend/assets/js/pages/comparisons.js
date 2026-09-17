// Comparisons list (Section 9) — reopen any previous comparison and see
// every candidate's answers/evidence.

import { api } from "../api.js";
import { el, mount } from "../dom.js";
import { getProjectId } from "../state.js";
import { formatDateTime, formatCost, phaseLabel } from "../format.js";

export async function renderComparisons(root) {
  mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading comparisons…"]));

  let projectId;
  let comparisons;
  try {
    projectId = await getProjectId();
    comparisons = await api.get(`/comparisons?project_id=${encodeURIComponent(projectId)}`);
  } catch (err) {
    mount(root, el("div", { class: "error-banner" }, err.message || "Failed to load comparisons."));
    return;
  }

  const container = el("div", { class: "stack" });
  container.appendChild(
    el("div", { class: "page-header" }, [
      el("h1", {}, "Comparisons"),
      el("div", { class: "subtitle" }, "Every Parallel Comparison run in this project, newest first."),
    ])
  );

  if (!comparisons.length) {
    container.appendChild(
      el("div", { class: "card" }, [
        el("div", { class: "empty-state" }, [
          el("div", { class: "icon" }, "🗂️"),
          el("h3", {}, "No comparisons yet"),
          el("p", {}, "Run your first comparison from Ask Agents."),
          el("a", { href: "#/ask" }, [el("button", { class: "primary" }, "Go to Ask Agents →")]),
        ]),
      ])
    );
    mount(root, container);
    return;
  }

  const table = el("table", {}, [
    el("thead", {}, [
      el("tr", {}, [
        el("th", {}, "Created"),
        el("th", {}, "Phase"),
        el("th", {}, "Candidates"),
        el("th", {}, "Total tokens"),
        el("th", {}, "Total cost"),
      ]),
    ]),
    el(
      "tbody",
      {},
      comparisons.map((c) =>
        el(
          "tr",
          { class: "clickable", onclick: () => (window.location.hash = `#/comparisons/${c.id}`) },
          [
            el("td", {}, formatDateTime(c.created_at)),
            el("td", {}, el("span", { class: badgeClassForPhase(c.phase) }, phaseLabel(c.phase))),
            el("td", {}, String(c.candidates.length)),
            el("td", {}, String(c.total_tokens_in + c.total_tokens_out)),
            el("td", {}, formatCost(c.total_cost)),
          ]
        )
      )
    ),
  ]);

  container.appendChild(el("div", { class: "card" }, table));
  mount(root, container);
}

function badgeClassForPhase(phase) {
  if (phase === "completed" || phase === "ready_for_selection") return "badge badge-done";
  if (phase === "failed") return "badge badge-failed";
  if (phase === "cancelled") return "badge badge-neutral";
  return "badge badge-running";
}
