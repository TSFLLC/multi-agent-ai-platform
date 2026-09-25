import { api } from "../api.js";
import { clear, el } from "../dom.js";

const stateLabel = (state) => state ? state.replaceAll("_", " ") : "not started";

export const assistanceDescription = (level) => ({ h0: "Independent", h1: "Socratic clue", h2: "Targeted pointer", h3: "Partial structure", h4: "Guided steps", h5: "Worked solution assistance" }[String(level || "h0").toLowerCase()] || "Assistance");
export const studyModeLabel = (eligible) => eligible ? "Study Mode available: worked example plus a different variant retry" : "Study Mode unlocks after the deterministic assistance path";
export const evidenceSummaryText = (summary) => `Project activity ${summary.project_activity || 0} · Candidate evidence ${summary.candidate_evidence || 0} · Qualified learning evidence ${summary.qualified_learning_evidence || 0} · Learner State is recomputed from qualified evidence.`;
export const assessmentStatusText = (submission) => submission ? `READY FOR ASSESSMENT · Grader invoked: ${submission.grader_invoked ? "yes" : "no"}` : "Not submitted; no grade or pass/fail is issued in AIL.5B.";

export function academyProgressText(progress) {
  return `${progress.completed_items} of ${progress.required_items} required items complete; ${progress.demonstrated_concepts} of ${progress.required_concepts} required concepts demonstrated.`;
}

export function groupAcademyItems(items) {
  return items.reduce((groups, item) => {
    const key = item.module_key || "unassigned";
    (groups[key] ||= []).push(item);
    return groups;
  }, {});
}

export function academyEnrollmentHref(enrollmentId, view = "today") {
  return `#/academy/enrollments/${encodeURIComponent(enrollmentId)}/${view}`;
}

function loading(root) {
  clear(root);
  root.appendChild(el("div", { class: "loading-state" }, "Loading Academy…"));
}

function error(root, err) {
  clear(root);
  root.appendChild(el("div", { class: "error-banner" }, err.message || "Academy could not be loaded."));
}

function progressCard(progress) {
  return el("div", { class: "card academy-progress-card" }, [
    el("div", { class: "row between" }, [el("h2", {}, "Progress"), el("span", { class: "badge badge-neutral" }, progress.complete ? "Eligible to complete" : "In progress")]),
    el("p", {}, `${progress.completed_items} of ${progress.required_items} required items complete.`),
    el("p", {}, `${progress.demonstrated_concepts} of ${progress.required_concepts} required concepts demonstrated; ${progress.practiced_or_better} practiced or better.`),
    el("p", { class: "hint" }, `${progress.exposed} exposed · ${progress.understood_or_better} understood · ${progress.review_due} review due`),
  ]);
}

function itemCard(item) {
  return el("article", { class: "card academy-item" }, [
    el("div", { class: "row between" }, [
      el("div", { class: "eyebrow" }, `Day ${item.day} · ${item.module_key}`),
      item.state ? el("span", { class: "badge badge-neutral" }, stateLabel(item.state)) : null,
    ]),
    el("h3", {}, item.title || item.module_key),
    item.purpose_text ? el("p", {}, item.purpose_text) : null,
    item.estimated_minutes ? el("p", { class: "hint" }, `${item.estimated_minutes} minutes`) : null,
    item.overlays && item.overlays.length ? el("p", { class: "hint" }, item.overlays.join(" · ")) : null,
  ]);
}

function navLinks(enrollmentId) {
  return el("div", { class: "row" }, [
    el("a", { class: "button-link", href: "#/academy" }, "Academy Home"),
    el("a", { class: "button-link", href: "#/academy/assessments" }, "Assessment Center"),
    enrollmentId ? el("a", { class: "button-link", href: `#/academy/enrollments/${encodeURIComponent(enrollmentId)}/today` }, "Today") : null,
    enrollmentId ? el("a", { class: "button-link", href: `#/academy/enrollments/${encodeURIComponent(enrollmentId)}/progress` }, "Progress") : null,
  ]);
}

export async function renderAcademyHome(root) {
  loading(root);
  try {
    const enrollments = await api.get("/academy/enrollments");
    if (!enrollments.length) {
      const programs = await api.get("/academy/programs");
      clear(root);
      root.appendChild(el("div", { class: "page-header" }, [el("h1", {}, "AI Academy"), el("p", { class: "subtitle" }, "Build capability through concepts, practice, evidence, and reflection.")]));
      root.appendChild(el("section", { class: "stack" }, programs.length ? programs.map((program) => el("article", { class: "card" }, [el("h2", {}, program.title), el("p", {}, program.description || ""), el("a", { class: "button-link", href: `#/academy/programs/${encodeURIComponent(program.id)}` }, "View program")])) : [el("div", { class: "empty-state" }, "No published Academy programs are available yet.")]));
      return;
    }
    const details = await Promise.all(enrollments.map((e) => api.get(`/academy/enrollments/${encodeURIComponent(e.id)}`)));
    clear(root);
    root.appendChild(el("div", { class: "page-header" }, [el("h1", {}, "AI Academy"), el("p", { class: "subtitle" }, "Your learning is measured by evidence, not page views.")]));
    root.appendChild(el("div", { class: "stack" }, details.map((detail) => el("article", { class: "card" }, [
      el("div", { class: "row between" }, [el("div", { class: "eyebrow" }, detail.enrollment.status), el("span", { class: "badge badge-neutral" }, `${detail.program.versions[0]?.duration_days || ""} days`)]),
      el("h2", {}, detail.program.title),
      progressCard(detail.progress),
      navLinks(detail.enrollment.id),
    ]))));
  } catch (err) { error(root, err); }
}

export async function renderAcademyProgram(root, params) {
  loading(root);
  try {
    const program = await api.get(`/academy/programs/${encodeURIComponent(params.id)}`);
    const version = program.versions.find((v) => v.status === "published") || program.versions[program.versions.length - 1];
    clear(root);
    root.appendChild(el("div", { class: "page-header" }, [el("h1", {}, program.title), el("p", { class: "subtitle" }, program.description || ""), el("p", { class: "hint" }, `${version?.duration_days || 0} days · version ${version?.version || "draft"}`)]));
    const grouped = groupAcademyItems(version?.items || []);
    root.appendChild(el("div", { class: "stack" }, Object.entries(grouped).map(([module, items]) => el("section", { class: "card" }, [el("h2", {}, module), el("div", { class: "stack" }, items.map(itemCard))]))));
  } catch (err) { error(root, err); }
}

export async function renderAcademyToday(root, params) {
  loading(root);
  try {
    const data = await api.get(`/academy/enrollments/${encodeURIComponent(params.id)}/today`);
    clear(root);
    root.appendChild(el("div", { class: "page-header" }, [el("h1", {}, `Today's Learning · Day ${data.day}`), el("p", { class: "subtitle" }, "Work from the current evidence-backed state and prerequisites."), navLinks(params.id)]));
    root.appendChild(progressCard(data.progress));
    root.appendChild(el("div", { class: "stack" }, data.items.length ? data.items.map(itemCard) : [el("div", { class: "empty-state" }, "Nothing is scheduled for today.")]));
  } catch (err) { error(root, err); }
}

export async function renderAcademyProgress(root, params) {
  loading(root);
  try {
    const detail = await api.get(`/academy/enrollments/${encodeURIComponent(params.id)}`);
    clear(root);
    root.appendChild(el("div", { class: "page-header" }, [el("h1", {}, "Academy Progress"), el("p", { class: "subtitle" }, detail.program.title), navLinks(params.id)]));
    root.appendChild(progressCard(detail.progress));
  } catch (err) { error(root, err); }
}

export async function renderProjectLibrary(root) {
  loading(root);
  try {
    const projects = await api.get("/academy/build-with-me/projects");
    clear(root);
    root.appendChild(el("div", { class: "page-header" }, [el("h1", {}, "Build With Me"), el("p", { class: "subtitle" }, "Choose a project and build it with a coach. Your work stays primary.")]));
    root.appendChild(el("div", { class: "stack" }, projects.map((project) => el("article", { class: "card" }, [
      el("div", { class: "row between" }, [el("h2", {}, project.title), el("span", { class: "badge badge-neutral" }, project.availability)]),
      el("p", {}, `${project.ladder_level.toUpperCase()} · ${project.est_minutes_min}–${project.est_minutes_max} minutes`),
      el("p", { class: "hint" }, (project.concepts || []).map((c) => c.role).join(" · ") || "Concept-guided project"),
      el("a", { class: "button-link", href: `#/academy/projects/${encodeURIComponent(project.id)}` }, "View project"),
    ]))));
  } catch (err) { error(root, err); }
}

export async function renderProjectOverview(root, params) {
  loading(root);
  try {
    const project = await api.get(`/academy/build-with-me/projects/${encodeURIComponent(params.id)}`);
    clear(root);
    root.appendChild(el("div", { class: "page-header" }, [el("h1", {}, project.title), el("p", { class: "subtitle" }, project.brief)]));
    root.appendChild(el("section", { class: "card" }, [el("h2", {}, "Milestones"), el("ol", {}, (project.milestones || []).map((m) => el("li", {}, [el("strong", {}, m.title), el("p", {}, m.instructions)])))]));
    root.appendChild(el("p", { class: "hint" }, `Estimated ${project.estimated_minutes?.min || ""}–${project.estimated_minutes?.max || ""} minutes · ${project.availability || "available"}`));
    const button = el("button", { class: "button", type: "button" }, "Start project");
    button.addEventListener("click", async () => { const result = await api.post(`/academy/build-with-me/attempts?template_id=${encodeURIComponent(project.id)}`); window.location.hash = `#/academy/projects/attempts/${encodeURIComponent(result.attempt_id)}`; });
    root.appendChild(button);
  } catch (err) { error(root, err); }
}

export async function renderBuildWorkspace(root, params) {
  const id = encodeURIComponent(params.id);
  const assistanceHelp = { h0: "Independent", h1: "Socratic clue", h2: "Targeted pointer", h3: "Partial structure", h4: "Guided steps", h5: "Worked solution assistance" };
  async function reload() {
    loading(root);
    try {
      const attempt = await api.get(`/academy/build-with-me/attempts/${id}`);
      const evidence = await api.get(`/academy/build-with-me/attempts/${id}/evidence`);
      const experiments = await api.get(`/academy/build-with-me/attempts/${id}/experiments/eligible`).catch(() => []);
      const submission = await api.get(`/academy/build-with-me/attempts/${id}/submission`).catch(() => null);
      clear(root);
      root.appendChild(el("div", { class: "page-header" }, [el("h1", {}, "Build With Me workspace"), el("p", { class: "subtitle" }, "Understand · plan · build · test · debug · improve · explain · reflect") ]));
      const milestoneRows = attempt.milestones || [];
      const progress = el("section", { class: "card" }, [el("h2", {}, "Milestone progress")]);
      const list = el("div", { class: "stack" });
      progress.appendChild(list);
      for (const m of milestoneRows) {
        const row = el("article", { class: "card milestone-row" }, [el("div", { class: "row between" }, [el("strong", {}, `${m.status}`), el("span", { class: "badge badge-neutral" }, `${m.attempts_count} tries`)]), el("p", {}, `Assistance used: ${m.max_assistance_level || "h0"}`), el("p", { class: "hint" }, `Maximum currently unlocked: ${(m.maximum_unlocked_level || "h0").toUpperCase()} · ${assistanceDescription(m.maximum_unlocked_level)}`)]);
        const start = el("button", { class: "button", type: "button" }, "Start / resume");
        start.addEventListener("click", async () => { await api.post(`/academy/build-with-me/attempts/${id}/milestones/${encodeURIComponent(m.milestone_id)}/start`); await reload(); });
        const test = el("button", { class: "button-secondary", type: "button" }, "Test result");
        test.addEventListener("click", async () => { const result = await api.post(`/academy/build-with-me/attempts/${id}/milestones/${encodeURIComponent(m.milestone_id)}/test`); row.appendChild(el("p", { class: "hint" }, result.message || "Test result recorded.")); await reload(); });
        const mentor = el("button", { class: "button-secondary", type: "button" }, "Ask Mentor");
        mentor.addEventListener("click", () => showMentor(row, m, id, reload));
        row.appendChild(el("div", { class: "row" }, [start, test, mentor]));
        row.appendChild(el("p", { class: "hint" }, `Current: ${m.max_assistance_level || "h0"}. The server determines the maximum unlocked level.`));
        list.appendChild(row);
      }
      root.appendChild(progress);
      root.appendChild(evidenceCard(evidence, assistanceHelp));
      root.appendChild(debugCard(milestoneRows));
      root.appendChild(studyCard(id, milestoneRows, reload));
      root.appendChild(countLearningCard(id, experiments, reload));
      root.appendChild(submissionCard(id, submission, reload));
    } catch (err) { error(root, err); }
  }
  await reload();
}

function showMentor(root, milestone, attemptId, reload) {
  const levels = ["h1", "h2", "h3", "h4", "h5"];
  const select = el("select", {}); levels.forEach((level) => select.appendChild(el("option", { value: level }, level.toUpperCase())));
  const question = el("textarea", { rows: "3", placeholder: "What are you stuck on?" });
  const response = el("div", { class: "mentor-response" });
  const explain = el("textarea", { rows: "3", placeholder: "Explain back what you think is happening (optional)." });
  const saveExplain = el("button", { class: "button-secondary", type: "button" }, "Save explain-back");
  saveExplain.addEventListener("click", async () => { await api.post(`/academy/build-with-me/attempts/${attemptId}/milestones/${encodeURIComponent(milestone.milestone_id)}/explain-back`, { question: "What is your current explanation?", response: explain.value }); saveExplain.textContent = "Explain-back saved"; });
  const close = el("button", { class: "button-secondary", type: "button" }, "Close");
  const send = el("button", { class: "button", type: "button" }, "Request help");
  send.addEventListener("click", async () => { const result = await api.post(`/academy/build-with-me/attempts/${attemptId}/milestones/${encodeURIComponent(milestone.milestone_id)}/mentor`, { question: question.value, assistance_level: select.value }); response.appendChild(el("p", {}, result.response?.direct_answer || "Mentor response recorded.")); response.appendChild(el("p", { class: "hint" }, `Authorized assistance: ${result.assistance_level}. AgentRun provenance recorded: ${Boolean(result.provenance?.agent_run_id)}.`)); });
  const panel = el("div", { class: "card mentor-panel" }, [el("h3", {}, "Mentor"), el("p", { class: "hint" }, "The deterministic policy controls eligibility; the Mentor cannot choose a higher level."), select, question, el("div", { class: "row" }, [send]), response, el("h4", {}, "Explain-back"), explain, saveExplain, close ]);
  close.addEventListener("click", () => panel.remove());
  root.appendChild(panel);
}

function evidenceCard(evidence, assistanceHelp) {
  const rows = evidence.candidate_evidence || [];
  return el("section", { class: "card evidence-summary" }, [el("h2", {}, "Evidence summary"), el("p", {}, `Project activity: ${(evidence.project_activity || []).length}`), el("p", {}, `Candidate evidence: ${rows.length}`), el("p", {}, `Qualified learning evidence: ${(evidence.qualified_learning_evidence || []).length}`), el("p", {}, `Learner State is recomputed only from qualified evidence.`), rows.length ? el("div", { class: "stack" }, rows.map((row) => el("p", { class: "hint" }, `${row.id}: ${row.qualified ? "qualified" : "candidate only"} · ${assistanceHelp[row.assistance_level] || row.assistance_level || "independent"}`))) : el("p", { class: "hint" }, "No candidate evidence yet."), el("p", { class: "hint" }, `${(evidence.explain_back || []).length} explain-back/reflection response(s) captured; not formally graded.`)]);
}

function debugCard(milestones) {
  return el("section", { class: "card" }, [el("h2", {}, "Debug With Me history"), milestones.length ? el("div", { class: "stack" }, milestones.map((m) => el("p", {}, `${m.status}: ${m.attempts_count} attempt(s); prior attempts remain preserved.`))) : el("p", { class: "hint" }, "No debugging attempts recorded yet."), el("p", { class: "hint" }, "Actual execution evidence is shown only when the platform provides it. MA9-dependent execution remains capability-gated.")]);
}

function studyCard(attemptId, milestones, reload) {
  const eligible = milestones.find((m) => m.max_assistance_level === "h5");
  const card = el("section", { class: "card" }, [el("h2", {}, "Study Mode"), el("p", { class: "hint" }, `${studyModeLabel(Boolean(eligible))}. A variant does not grant Demonstrated.`)]);
  if (eligible) {
    const button = el("button", { class: "button", type: "button" }, "Enter Study Mode");
    button.addEventListener("click", async () => { const result = await api.post(`/academy/build-with-me/attempts/${attemptId}/milestones/${encodeURIComponent(eligible.milestone_id)}/study-mode`); card.appendChild(el("div", { class: "study-example" }, [el("h3", {}, "Worked example"), el("p", {}, result.worked_example_ref), el("p", {}, "Variant retry created. Perform your own work, then record the actual result/evidence."), el("p", { class: "hint" }, `Variant attempt: ${result.variant_attempt_id}; state outcome comes from evidence qualification.`)])); button.disabled = true; });
    card.appendChild(button);
  }
  return card;
}

function countLearningCard(attemptId, experiments, reload) {
  const card = el("section", { class: "card" }, [el("h2", {}, "Count This Toward My Learning"), el("p", { class: "hint" }, "This is always explicit. Select eligible Personal Lab work, confirm, and the existing qualification service decides whether evidence is created.")]);
  if (!experiments.length) { card.appendChild(el("p", { class: "hint" }, "No eligible Personal Lab experiments are available.")); return card; }
  experiments.forEach((experiment) => { const button = el("button", { class: "button-secondary", type: "button" }, `Count ${experiment.hypothesis || "experiment"}`); button.disabled = !experiment.qualification?.ready; button.title = experiment.qualification?.message || "Not eligible"; button.addEventListener("click", async () => { if (!window.confirm("Count this selected experiment toward your learning?")) return; await api.post(`/academy/build-with-me/attempts/${attemptId}/experiments/${encodeURIComponent(experiment.id)}/count-toward-learning`, { confirmed: true }); await reload(); }); card.appendChild(button); });
  return card;
}

function submissionCard(attemptId, submission, reload) {
  const card = el("section", { class: "card" }, [el("h2", {}, "Assessment-ready submission"), el("p", { class: "hint" }, assessmentStatusText(submission))]);
  const button = el("button", { class: "button", type: "button" }, submission ? "Refresh submission snapshot" : "Prepare READY FOR ASSESSMENT");
  button.addEventListener("click", async () => { await api.post(`/academy/build-with-me/attempts/${attemptId}/submit`); await reload(); });
  card.appendChild(button); return card;
}
