import { api } from "../api.js";
import { el, mount } from "../dom.js";
import { navigate } from "../router.js";

const TYPE_LABELS = {
  model_comparison: "Model Face-off",
  prompt_comparison: "Agent Version Comparison",
  variance: "Consistency Check",
};

function typeLabel(value) {
  return TYPE_LABELS[value] || value || "Experiment";
}

function statusLabel(value) {
  return String(value || "").replaceAll("_", " ");
}

function message(text, className = "empty-state") {
  return el("div", { class: className }, [el("p", {}, [text])]);
}

export async function renderLab(root) {
  mount(root, el("section", { class: "page-section" }, [
    el("h1", {}, ["Personal AI Lab"]),
    el("p", { class: "page-intro" }, ["Run a frozen Test Kit through the existing execution system and inspect factual results."]),
    el("div", { class: "lab-toolbar" }, [
      el("button", { type: "button", onclick: async () => {
        const button = document.activeElement;
        if (button) button.disabled = true;
        try {
          await api.post("/lab/test-kits/starter");
          await renderLab(root);
        } catch (err) {
          mount(root.querySelector(".lab-content"), message(err.message, "error-banner"));
        }
      } }, ["Prepare starter Test Kits"]),
    ]),
    el("div", { class: "lab-content" }, [message("Loading Personal AI Lab…")]),
  ]));

  const content = root.querySelector(".lab-content");
  try {
    const [kits, experiments] = await Promise.all([
      api.get("/lab/test-kits"),
      api.get("/lab/experiments"),
    ]);
    const items = experiments?.items || [];
    const kitItems = (kits || []).map((kit) => el("li", {}, [
      el("strong", {}, [kit.name]),
      kit.description ? el("span", { class: "muted" }, [` — ${kit.description}`]) : null,
    ]));
    const experimentItems = items.map((experiment) => el("li", { class: "lab-experiment" }, [
      el("div", {}, [
        el("strong", {}, [typeLabel(experiment.experiment_type)]),
        el("span", { class: "muted" }, [` · ${statusLabel(experiment.status)}`]),
      ]),
      el("div", { class: "lab-actions" }, [
        el("button", { type: "button", onclick: () => navigate(`#/ail/lab/experiments/${encodeURIComponent(experiment.id)}`) }, ["View results"]),
        ["draft", "estimated", "approved"].includes(String(experiment.status))
          ? el("button", { type: "button", onclick: async (event) => {
            event.currentTarget.disabled = true;
            try {
              await api.post(`/lab/experiments/${encodeURIComponent(experiment.id)}/run`);
              await renderLab(root);
            } catch (err) {
              event.currentTarget.disabled = false;
              mount(content, message(err.message, "error-banner"));
            }
          } }, ["Run Experiment"])
          : null,
      ]),
    ]));
    mount(content, el("div", {}, [
      el("h2", {}, ["Test Kits"]),
      kitItems.length ? el("ul", {}, kitItems) : message("No Test Kits yet. Prepare the controlled starter kits to begin."),
      el("h2", {}, ["Experiments"]),
      experimentItems.length ? el("ul", {}, experimentItems) : message("No experiments yet. Create a draft from the Personal Lab API or a later Lab workflow."),
    ]));
  } catch (err) {
    mount(content, message(err.message || "Personal AI Lab is unavailable.", "error-banner"));
  }
}

export async function renderExperimentDetail(root, params) {
  mount(root, message("Loading experiment results…"));
  try {
    const data = await api.get(`/lab/experiments/${encodeURIComponent(params.id)}/results`);
    const progress = data.progress || {};
    const runItems = (data.runs || []).map((run) => el("li", {}, [
      `Task ${run.task_position + 1}, repetition ${run.repetition}, ${run.label}: ${statusLabel(run.status)}`,
      run.model_id ? ` · model ${run.model_id}` : "",
      run.tokens_in || run.tokens_out ? ` · ${run.tokens_in} in / ${run.tokens_out} out tokens` : "",
    ]));
    mount(root, el("section", { class: "page-section" }, [
      el("a", { href: "#/ail/lab" }, ["← Personal AI Lab"]),
      el("h1", {}, [typeLabel(data.experiment?.experiment_type)]),
      el("p", {}, [`Status: ${statusLabel(data.experiment?.status)}`]),
      el("h2", {}, ["What happened"]),
      el("p", {}, [`${progress.completed || 0} completed, ${progress.failed || 0} failed, ${progress.cancelled || 0} cancelled of ${progress.total || 0} runs.`]),
      el("p", {}, [`Tokens: ${progress.tokens_in || 0} input / ${progress.tokens_out || 0} output · Cost: ${progress.cost_kind || "UNKNOWN"}${progress.cost ? ` (${progress.cost})` : ""}`]),
      el("h2", {}, ["Run evidence"]),
      runItems.length ? el("ul", {}, runItems) : message("No execution evidence has been recorded yet."),
      el("p", { class: "muted" }, ["Personal AI Lab reports evidence and does not choose a winner. Human conclusions are a later phase."]),
    ]));
  } catch (err) {
    mount(root, message(err.message || "Results are unavailable.", "error-banner"));
  }
}
