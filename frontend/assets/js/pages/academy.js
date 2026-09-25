import { api } from "../api.js";
import { clear, el } from "../dom.js";

const stateLabel = (state) => state ? state.replaceAll("_", " ") : "not started";

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
  loading(root);
  try {
    const attempt = await api.get(`/academy/build-with-me/attempts/${encodeURIComponent(params.id)}`);
    const evidence = await api.get(`/academy/build-with-me/attempts/${encodeURIComponent(params.id)}/evidence`);
    clear(root);
    root.appendChild(el("div", { class: "page-header" }, [el("h1", {}, "Build With Me workspace"), el("p", { class: "subtitle" }, "Understand · plan · build · test · debug · explain · reflect") ]));
    root.appendChild(el("section", { class: "card" }, [el("h2", {}, "Milestone progress"), el("div", { class: "stack" }, (attempt.milestones || []).map((m) => el("p", {}, `${m.status} · ${m.attempts_count} tries · assistance ${m.max_assistance_level || "h0"}`)))]));
    root.appendChild(el("section", { class: "card" }, [el("h2", {}, "Evidence summary"), el("p", {}, `Project activity: ${(evidence.project_activity || []).length}`), el("p", {}, `Candidate evidence: ${(evidence.candidate_evidence || []).length}`), el("p", {}, `Qualified learning evidence: ${(evidence.qualified_learning_evidence || []).length}`), el("p", {}, `Explain-back responses: ${(evidence.explain_back || []).length}`)]));
    root.appendChild(el("p", { class: "hint" }, "Mentor assistance is bounded by the deterministic H0–H5 policy. It never grades or changes learner state directly."));
  } catch (err) { error(root, err); }
}
