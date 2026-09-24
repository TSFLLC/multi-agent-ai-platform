import { api } from "../api.js";
import { el, mount } from "../dom.js";
import {
  CONCLUSION_OPTIONS,
  RESULT_SECTIONS,
  canConclude,
  conceptName,
  conclusionSummary,
  countedMessage,
  differencesView,
  evaluationView,
  nextSteps,
  qualificationView,
  validateConclusionDraft,
} from "../labLearning.js";
import { navigate } from "../router.js";

const TYPE_LABELS = {
  model_comparison: "Model Face-off",
  prompt_comparison: "Agent Version Comparison",
  variance: "Consistency Check",
};

export function typeLabel(value) {
  return TYPE_LABELS[value] || value || "Experiment";
}

export function statusLabel(value) {
  return String(value || "").replaceAll("_", " ");
}

export function overallLabel(experiment) {
  const status = experiment?.overall_status || experiment?.status;
  if (status === "EVALUATING" && experiment?.evaluation_status === "RUNNING") return "Evaluating";
  if (status === "EVALUATING" && experiment?.evaluation_status === "PENDING") return "Execution complete — evaluation pending";
  return {
    RUNNING: "Running experiment",
    EVALUATING: "Evaluating",
    COMPLETED: "Completed",
    EVALUATION_FAILED: "Execution complete — evaluation failed",
    PARTIAL: "Partial results",
    FAILED: "Execution failed",
    CANCELLED: "Cancelled",
  }[status] || statusLabel(status);
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

// -- Results page: fixed section hierarchy ------------------------------------

function section(id, title, children) {
  return el("section", { class: "lab-section", "data-section": id }, [el("h2", {}, [title]), ...children]);
}

function linkTo(href, label) {
  return el("a", { href }, [label]);
}

function inlineStatus(text = "", isError = false) {
  return el("p", { class: isError ? "error-text" : "muted", role: "status" }, [text]);
}

function testedSection({ data, development }) {
  const experiment = data.experiment || {};
  const facts = [
    `${(experiment.models || []).length} model(s)`,
    `${(experiment.agent_version_ids || []).length} agent version(s)`,
    `${experiment.repetitions || 1} repetition(s)`,
  ].join(" · ");
  return section("tested", "What I tested", [
    el("p", {}, [typeLabel(experiment.experiment_type)]),
    experiment.hypothesis ? el("p", {}, [`My question: ${experiment.hypothesis}`]) : null,
    el("p", { class: "muted" }, [facts]),
    experiment.development_id
      ? el("p", { class: "muted", "data-origin": "radar" }, [
        "Started from Radar: ",
        linkTo(`#/ail/radar/developments/${encodeURIComponent(experiment.development_id)}`, development?.title || "a Radar development"),
      ])
      : null,
  ]);
}

function happenedSection({ data }) {
  const progress = data.progress || {};
  const runItems = (data.runs || []).map((run) => el("li", {}, [
    `Task ${run.task_position + 1}, repetition ${run.repetition}, ${run.label}: ${statusLabel(run.status)}`,
    run.model_id ? ` · model ${run.model_id}` : "",
    run.tokens_in || run.tokens_out ? ` · ${run.tokens_in} in / ${run.tokens_out} out tokens` : "",
  ]));
  return section("happened", "What happened", [
    el("p", {}, [`Status: ${overallLabel(data.experiment)}`]),
    el("p", {}, [`${progress.completed || 0} completed, ${progress.failed || 0} failed, ${progress.cancelled || 0} cancelled of ${progress.total || 0} runs.`]),
    el("p", {}, [`Tokens: ${progress.tokens_in || 0} input / ${progress.tokens_out || 0} output · Cost: ${progress.cost_kind || "UNKNOWN"}${progress.cost ? ` (${progress.cost})` : ""}`]),
    runItems.length ? el("ul", {}, runItems) : message("No execution evidence has been recorded yet."),
  ]);
}

function evaluationSection({ data }) {
  const view = evaluationView(data.experiment);
  return section("evaluation", "Evaluation", [
    el("p", {}, [view.headline]),
    view.detail ? el("p", { class: "muted" }, [view.detail]) : null,
  ]);
}

function differedSection({ data }) {
  const view = differencesView(data.runs);
  const rows = view.rows.map((row) => el("li", {}, [
    `${row.label}: ${row.completed} of ${row.runs} runs completed`,
    row.failed ? `, ${row.failed} failed` : "",
    row.cancelled ? `, ${row.cancelled} cancelled` : "",
    ` · ${row.tokens_in} in / ${row.tokens_out} out tokens`,
  ]));
  return section("differed", "Where they differed", [
    view.comparable ? el("ul", {}, rows) : message("There is only one candidate here, so there is nothing to compare."),
    el("p", { class: "muted" }, [view.note]),
  ]);
}

function conclusionSection({ data }, handlers) {
  const experiment = data.experiment || {};
  const saved = conclusionSummary(experiment.conclusion);
  const children = [];
  if (saved) {
    children.push(el("div", { class: "lab-conclusion", "data-conclusion": experiment.conclusion.type }, [
      el("p", {}, [el("strong", {}, [saved.label])]),
      saved.text ? el("p", {}, [saved.text]) : null,
      el("p", { class: "muted" }, [saved.note]),
    ]));
  }
  if (!canConclude(experiment)) {
    children.push(message("You can write your conclusion once the experiment has finished and there are results to read."));
    return section("conclusion", "My conclusion", children);
  }
  const chosen = experiment.conclusion?.type || "inconclusive";
  const select = el("select", { "aria-label": "How would you describe the result?" }, CONCLUSION_OPTIONS.map((option) => el("option", {
    value: option.value, selected: option.value === chosen,
  }, [option.label])));
  select.value = chosen;
  const textarea = el("textarea", { rows: "3", maxlength: "4000", "aria-label": "Your conclusion in your own words" }, [experiment.conclusion?.text || ""]);
  const status = inlineStatus(handlers.notice?.conclusion || "");
  const save = el("button", { type: "button", class: "primary small" }, [saved ? "Update my conclusion" : "Save my conclusion"]);
  save.addEventListener("click", async () => {
    const draft = { type: select.value, text: textarea.value };
    const problem = validateConclusionDraft(draft);
    if (problem) {
      status.textContent = problem;
      return;
    }
    save.disabled = true;
    try {
      await handlers.saveConclusion(draft);
    } catch (err) {
      status.textContent = err.message || "Could not save your conclusion.";
      save.disabled = false;
    }
  });
  children.push(
    el("p", { class: "muted" }, ["Your conclusion is your own reading of the results. Choosing one never has to name a winner, and it does not change the evaluation."]),
    el("label", {}, ["How would you describe the result?", select]),
    el("label", {}, ["In your own words (optional)", textarea]),
    el("div", { class: "lab-actions" }, [save]),
    status,
  );
  return section("conclusion", "My conclusion", children);
}

function countSection({ data, qualification, concepts, conceptResults }, handlers) {
  const experiment = data.experiment || {};
  const view = qualificationView(qualification);
  const children = [
    el("div", { class: `lab-qualification lab-qualification-${view.tone}`, "data-qualification": view.status }, [
      el("p", {}, [el("strong", {}, [view.title])]),
      view.message && view.message !== view.title ? el("p", {}, [view.message]) : null,
      view.hint ? el("p", { class: "muted" }, [view.hint]) : null,
    ]),
  ];
  if (handlers.notice?.count) children.push(el("p", { class: "success-text", role: "status" }, [handlers.notice.count]));

  const conceptLine = experiment.concept_id
    ? `Concept: ${conceptName(concepts, experiment.concept_id)}`
    : "No Concept chosen yet.";
  children.push(el("p", { "data-concept": experiment.concept_id || "" }, [conceptLine]));
  if (view.counted) {
    children.push(el("p", { class: "muted" }, ["The Concept is locked because this experiment has already been counted."]));
  } else {
    const input = el("input", { type: "search", placeholder: "Search Concepts", "aria-label": "Search Concepts" });
    const status = inlineStatus(handlers.notice?.concept || "");
    const results = el("ul", { class: "lab-concept-results" }, (conceptResults || []).map((concept) => {
      const choose = el("button", { type: "button", class: "small" }, ["Use this Concept"]);
      choose.addEventListener("click", async () => {
        choose.disabled = true;
        try {
          await handlers.bindConcept(concept.id);
        } catch (err) {
          status.textContent = err.message || "Could not choose that Concept.";
          choose.disabled = false;
        }
      });
      return el("li", {}, [`${concept.name} `, choose]);
    }));
    const search = el("button", { type: "button", class: "small" }, ["Search"]);
    search.addEventListener("click", async () => {
      search.disabled = true;
      try {
        await handlers.searchConcepts(input.value);
      } catch (err) {
        status.textContent = err.message || "Search is unavailable.";
        search.disabled = false;
      }
    });
    children.push(el("div", { class: "lab-actions" }, [input, search]), results, status);
  }
  if (view.canCount) {
    const count = el("button", { type: "button", class: "primary" }, ["Count toward learning"]);
    const status = inlineStatus();
    count.addEventListener("click", async () => {
      count.disabled = true;
      try {
        await handlers.countToward();
      } catch (err) {
        status.textContent = err.message || "Could not count this experiment.";
        count.disabled = false;
      }
    });
    children.push(el("div", { class: "lab-actions" }, [count]), status);
  }
  return section("count", "Count toward learning", children);
}

function nextSection({ data, qualification }) {
  return section("next", "What I can do next", [
    el("ul", {}, nextSteps(data.experiment, qualification).map((step) => el("li", {}, [linkTo(step.href, step.label)]))),
  ]);
}

const SECTION_BUILDERS = {
  tested: testedSection,
  happened: happenedSection,
  evaluation: evaluationSection,
  differed: differedSection,
  conclusion: conclusionSection,
  count: countSection,
  next: nextSection,
};

// Sections in the fixed hierarchy. There is no Professor section: no canonical
// Professor exists, so nothing here may pretend one does.
export function buildResultSections(state, handlers = {}) {
  return RESULT_SECTIONS.map((id) => SECTION_BUILDERS[id](state, handlers));
}

export async function renderExperimentDetail(root, params) {
  mount(root, message("Loading experiment results…"));
  const id = encodeURIComponent(params.id);
  const notice = {};
  let concepts = [];
  let conceptResults = [];

  async function load() {
    const data = await api.get(`/lab/experiments/${id}/results`);
    const [qualification, development] = await Promise.all([
      api.get(`/lab/experiments/${id}/learning-qualification`).catch(() => null),
      data.experiment?.development_id
        ? api.get(`/radar/developments/${encodeURIComponent(data.experiment.development_id)}`).catch(() => null)
        : Promise.resolve(null),
    ]);
    return { data, qualification, development };
  }

  async function show() {
    try {
      const state = { ...(await load()), concepts, conceptResults };
      const handlers = {
        notice,
        saveConclusion: async ({ type, text }) => {
          await api.put(`/lab/experiments/${id}/conclusion`, { conclusion_type: type, conclusion_text: text || null });
          notice.conclusion = "Conclusion saved.";
          await show();
        },
        searchConcepts: async (query) => {
          const trimmed = query && query.trim();
          conceptResults = await api.get(`/radar/concepts?limit=25${trimmed ? `&q=${encodeURIComponent(trimmed)}` : ""}`);
          concepts = [...concepts, ...conceptResults.filter((c) => !concepts.some((known) => known.id === c.id))];
          await show();
        },
        bindConcept: async (conceptId) => {
          await api.put(`/lab/experiments/${id}/concept`, { concept_id: conceptId });
          conceptResults = [];
          await show();
        },
        countToward: async () => {
          const result = await api.post(`/lab/experiments/${id}/count-toward-learning`);
          notice.count = countedMessage(result);
          await show();
        },
      };
      mount(root, el("section", { class: "page-section" }, [
        el("a", { href: "#/ail/lab" }, ["← Personal AI Lab"]),
        el("h1", {}, [typeLabel(state.data.experiment?.experiment_type)]),
        ...buildResultSections(state, handlers),
      ]));
    } catch (err) {
      mount(root, message(err.message || "Results are unavailable.", "error-banner"));
    }
  }

  await show();
}
