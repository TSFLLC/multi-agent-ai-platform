// Activity — useful Flight Recorder information scoped to one comparison
// at a time (Section 9: "without becoming a full observability product").
// There is no global event feed yet (app.api.routers.events is a 501
// stub), so this reuses the existing Task Run-scoped events endpoint
// against the comparison's bookkeeping run and every candidate's own run.

import { api } from "../api.js";
import { el, mount, clear } from "../dom.js";
import { getProjectId } from "../state.js";
import { formatDateTime } from "../format.js";

export async function renderActivity(root) {
  mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading activity…"]));

  let projectId;
  let comparisons;
  try {
    projectId = await getProjectId();
    comparisons = await api.get(`/comparisons?project_id=${encodeURIComponent(projectId)}`);
  } catch (err) {
    mount(root, el("div", { class: "error-banner" }, err.message || "Failed to load activity."));
    return;
  }

  const state = { selectedId: comparisons[0] ? comparisons[0].id : null };
  draw(root, comparisons, state);
}

function draw(root, comparisons, state) {
  clear(root);
  const container = el("div", { class: "stack" });
  container.appendChild(
    el("div", { class: "page-header" }, [
      el("h1", {}, "Activity"),
      el("div", { class: "subtitle" }, "Flight Recorder trace for a single comparison — its own execution evidence, not a global feed."),
    ])
  );

  if (!comparisons.length) {
    container.appendChild(el("div", { class: "card" }, el("p", {}, "No comparisons have run yet.")));
    mount(root, container);
    return;
  }

  const select = el(
    "select",
    {
      onchange: (e) => {
        state.selectedId = e.target.value;
        loadEvents(root, comparisons, state);
      },
    },
    comparisons.map((c) => el("option", { value: c.id, selected: c.id === state.selectedId }, `${formatDateTime(c.created_at)} — ${c.id}`))
  );

  container.appendChild(el("div", { class: "card" }, [el("label", {}, "Comparison"), select]));
  container.appendChild(el("div", { class: "card", id: "activity-events" }, el("span", { class: "spinner" })));

  mount(root, container);
  loadEvents(root, comparisons, state);
}

async function loadEvents(root, comparisons, state) {
  const holder = document.getElementById("activity-events");
  if (!holder) return;
  clear(holder);
  holder.appendChild(el("span", { class: "spinner" }));

  const comparison = comparisons.find((c) => c.id === state.selectedId);
  if (!comparison) return;

  try {
    const runIds = [comparison.task_run_id, ...comparison.candidates.map((c) => c.task_run_id).filter(Boolean)];
    const uniqueRunIds = [...new Set(runIds)];
    const eventLists = await Promise.all(
      uniqueRunIds.map((runId) => api.get(`/tasks/${comparison.task_id}/runs/${runId}/events`).catch(() => []))
    );
    const events = eventLists.flat().sort((a, b) => a.sequence_number - b.sequence_number);

    clear(holder);
    if (!events.length) {
      holder.appendChild(el("p", {}, "No Flight Recorder events recorded yet for this comparison."));
      return;
    }
    const list = el(
      "div",
      { class: "stack", style: "gap:8px" },
      events.map((e) =>
        el("div", { class: "row", style: "align-items:flex-start;gap:14px" }, [
          el("span", { class: "hint", style: "min-width:170px" }, formatDateTime(e.occurred_at)),
          el("div", {}, [
            el("div", { style: "font-weight:600;font-size:0.85rem" }, e.event_type),
            e.decision_summary ? el("div", { class: "hint" }, e.decision_summary) : null,
          ]),
        ])
      )
    );
    holder.appendChild(list);
  } catch (err) {
    clear(holder);
    holder.appendChild(el("div", { class: "error-banner" }, err.message || "Failed to load events."));
  }
}
