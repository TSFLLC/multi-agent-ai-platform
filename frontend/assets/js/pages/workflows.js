// Workflows -- the list of workflows in an explicitly selected Project, and
// creating a new one (MA7.6A). The project is chosen here and remembered; unlike
// the older pages this one never silently takes projects[0] when there are
// several.

import { el, mount, clear } from "../dom.js";
import { navigate } from "../router.js";
import { workflowApi } from "../workflowApi.js";
import { normalizeApiError } from "../workflowErrors.js";
import { readStoredProjectId, resolveSelectedProject, writeStoredProjectId } from "../projectSelection.js";
import { sortVersionsNewestFirst, statusLabel } from "../workflowGraph.js";

function statusBadgeClass(status) {
  if (status === "active") return "badge badge-done";
  if (status === "draft") return "badge badge-running";
  return "badge badge-neutral";
}

export async function renderWorkflows(root) {
  const state = { projects: [], project: null, workflows: [], versions: new Map(), loading: false, error: null, creating: false, name: "" };
  mount(root, el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading projects…"]));

  try {
    state.projects = await workflowApi.listProjects();
  } catch (err) {
    mount(root, el("div", { class: "error-banner" }, normalizeApiError(err).message));
    return;
  }
  state.project = resolveSelectedProject(state.projects, readStoredProjectId());
  if (state.project) writeStoredProjectId(state.project.id);
  draw(root, state);
  if (state.project) await loadWorkflows(root, state);
}

async function loadWorkflows(root, state) {
  state.loading = true;
  state.error = null;
  draw(root, state);
  try {
    state.workflows = await workflowApi.listWorkflows(state.project.id);
    const versionLists = await Promise.all(state.workflows.map((workflow) => workflowApi.listVersions(workflow.id).catch(() => [])));
    state.versions = new Map(state.workflows.map((workflow, index) => [workflow.id, sortVersionsNewestFirst(versionLists[index])]));
  } catch (err) {
    state.workflows = [];
    state.error = normalizeApiError(err).message;
  }
  state.loading = false;
  draw(root, state);
}

function draw(root, state) {
  clear(root);
  const container = el("div", { class: "stack" });
  container.appendChild(
    el("div", { class: "page-header" }, [
      el("h1", {}, "Workflows"),
      el("div", { class: "subtitle" }, "How your AI team works together: who does what, in what order, with quality evidence and a human decision."),
    ])
  );

  const projectSelect = el(
    "select",
    {
      "aria-label": "Project",
      onchange: async (event) => {
        const chosen = state.projects.find((project) => project.id === event.target.value) || null;
        state.project = chosen;
        writeStoredProjectId(chosen ? chosen.id : null);
        state.workflows = [];
        if (chosen) await loadWorkflows(root, state);
        else draw(root, state);
      },
    },
    [
      !state.project ? el("option", { value: "", selected: true }, "Select a project…") : null,
      ...state.projects.map((project) => el("option", { value: project.id, selected: Boolean(state.project) && state.project.id === project.id }, project.name)),
    ]
  );
  container.appendChild(el("div", { class: "card" }, [el("label", {}, "Project"), projectSelect]));

  if (!state.projects.length) {
    container.appendChild(el("div", { class: "card" }, el("p", {}, "You do not belong to any project yet.")));
    mount(root, container);
    return;
  }
  if (!state.project) {
    container.appendChild(el("div", { class: "card" }, el("p", {}, "Choose a project to see its workflows.")));
    mount(root, container);
    return;
  }

  if (state.error) container.appendChild(el("div", { class: "error-banner" }, state.error));

  const nameInput = el("input", {
    type: "text",
    placeholder: "e.g. Build, review and approve a change",
    value: state.name,
    "aria-label": "New workflow name",
    maxlength: "255",
    oninput: (event) => {
      state.name = event.target.value;
      createButton.disabled = state.creating || !state.name.trim();
    },
    onkeydown: (event) => {
      if (event.key === "Enter" && state.name.trim() && !state.creating) createButton.click();
    },
  });
  const createButton = el(
    "button",
    {
      type: "button",
      class: "primary",
      disabled: state.creating || !state.name.trim(),
      onclick: async () => {
        if (state.creating || !state.name.trim()) return;
        state.creating = true;
        createButton.disabled = true;
        try {
          const workflow = await workflowApi.createWorkflow(state.project.id, state.name.trim());
          navigate(`#/workflows/${encodeURIComponent(workflow.id)}`);
        } catch (err) {
          state.creating = false;
          state.error = normalizeApiError(err).message;
          draw(root, state);
        }
      },
    },
    "Create workflow"
  );
  container.appendChild(el("div", { class: "card" }, [el("h2", {}, "New workflow"), el("div", { class: "row" }, [nameInput, createButton])]));

  if (state.loading) {
    container.appendChild(el("div", { class: "card" }, [el("span", { class: "spinner" }), " Loading workflows…"]));
  } else if (!state.workflows.length) {
    container.appendChild(
      el("div", { class: "card" }, el("div", { class: "empty-state" }, [el("div", { class: "icon" }, "🧭"), el("p", {}, "No workflows in this project yet. Create the first one above.")]))
    );
  } else {
    const rows = state.workflows.map((workflow) => {
      const versions = state.versions.get(workflow.id) || [];
      return el("tr", {}, [
        el("td", {}, workflow.name),
        el(
          "td",
          {},
          versions.length
            ? el(
                "div",
                { class: "studio-version-chips" },
                versions.map((version) => el("span", { class: statusBadgeClass(version.status) }, `v${version.version} ${statusLabel(version.status)}`))
              )
            : el("span", { class: "hint" }, "no versions")
        ),
        el("td", {}, el("a", { class: "studio-open-link", href: `#/workflows/${encodeURIComponent(workflow.id)}` }, "Open Studio")),
      ]);
    });
    container.appendChild(
      el("div", { class: "card" }, [
        el("h2", {}, "Workflows"),
        el("table", {}, [el("thead", {}, el("tr", {}, [el("th", {}, "Name"), el("th", {}, "Versions"), el("th", {}, "")])), el("tbody", {}, rows)]),
      ])
    );
  }
  mount(root, container);
}
