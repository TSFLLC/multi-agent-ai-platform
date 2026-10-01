import { api } from "../api.js";
import { el, mount } from "../dom.js";
import { navigate } from "../router.js";
import { academyEntryLinks, level1AssessmentAction } from "../academyLinks.js";
import { renderWorkspace } from "../workspace/workspace.js";

export { renderProgramOverview as renderAcademy } from "../workspace/overview.js";

export async function renderAcademyDay(root, { day }) {
  mount(root, el("section", { class: "page-section academy-lesson" }, [el("p", {}, ["Loading lesson…"])]));
  try {
    // AIL5D.6: a structured Day opens in the Learning Workspace; a Day that has not been converted keeps the legacy page.
    const learning = await api.get(`/academy/level-1/days/${encodeURIComponent(day)}/learning`);
    if (learning.structured) {
      const days = await api.get("/academy/level-1/days");
      return await renderWorkspace(root, day, learning, days);
    }
    const item = await api.get(`/academy/level-1/days/${encodeURIComponent(day)}`);
    await api.post(`/academy/level-1/items/${encodeURIComponent(item.id)}/open`);
    const assessment = level1AssessmentAction(item);
    const children = [
      el("p", { class: "muted" }, [`Day ${item.day} · Week ${item.week} · ${item.kind === "lab" ? "Personal Lab" : "Lecture"} · ${item.estimated_minutes || 60} min`]),
      el("h1", {}, [item.title]),
      academyEntryLinks({ current: "level1" }),
      item.capability_boundary ? el("p", { class: "notice" }, [item.capability_boundary]) : null,
      assessment ? el("section", { class: "card level1-assessment-action" }, [
        el("h2", {}, [assessment.heading]),
        el("p", { class: "hint" }, [assessment.hint]),
        el("a", { class: "button primary", href: assessment.href }, [assessment.label]),
      ]) : null,
      el("div", { class: "markdown-body" }, [item.body_md || "This authored activity is being prepared."]),
    ];
    if (item.kind === "lab") {
      children.push(el("button", { class: "primary", type: "button", onclick: async (event) => {
        event.currentTarget.disabled = true;
        try {
          const lab = await api.post(`/academy/level-1/days/${encodeURIComponent(day)}/start-lab`);
          if (lab.experiment_id) {
            navigate(`#/ail/lab/experiments/${encodeURIComponent(lab.experiment_id)}`);
          } else if (lab.workflow_run_id) {
            navigate(`#/workflow-runs/${encodeURIComponent(lab.workflow_run_id)}`);
          } else if (lab.agent_run_id) {
            navigate(`#/model-intelligence/agent-runs/${encodeURIComponent(lab.agent_run_id)}`);
          } else if (lab.project_attempt_id) {
            navigate(`#/academy/projects/attempts/${encodeURIComponent(lab.project_attempt_id)}`);
          } else {
            navigate(`#/academy/level-1/${encodeURIComponent(day)}`);
          }
        } catch (err) {
          event.currentTarget.disabled = false;
          event.currentTarget.textContent = err.message || "Personal Lab unavailable";
        }
      } }, ["Start Lab / Continue Lab"]));
    }
    if (item.capstone_stage) {
      children.push(el("button", { class: "primary", type: "button", onclick: async (event) => {
        event.currentTarget.disabled = true;
        try {
          const result = await api.post(`/academy/level-1/days/${encodeURIComponent(day)}/start-capstone`);
          navigate(result.assessment_center_url && Number(day) === 29 ? result.assessment_center_url : result.project_url);
        } catch (err) {
          event.currentTarget.disabled = false;
          event.currentTarget.textContent = err.message || "Capstone activity unavailable";
        }
      } }, [Number(day) === 29 ? "Open Assessment Center" : `Start Capstone: ${item.capstone_stage}`]));
    }
    children.push(el("button", { type: "button", onclick: () => navigate("#/academy/level-1") }, ["Return to Academy"]));
    mount(root, el("article", { class: "page-section" }, children));
  } catch (err) {
    mount(root, el("p", { class: "error-banner" }, [err.message || "Lesson unavailable."]));
  }
}
