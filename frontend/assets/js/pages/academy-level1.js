import { api } from "../api.js";
import { el, mount } from "../dom.js";
import { navigate } from "../router.js";

export async function renderAcademy(root) {
  mount(root, el("section", { class: "page-section academy-page" }, [
    el("h1", {}, ["AI Academy"]),
    el("p", { class: "page-intro" }, ["Practical AI Foundations · 30 days · evidence over completion."]),
    el("div", { class: "academy-content" }, [el("p", {}, ["Loading Academy…"])]),
  ]));
  const content = root.querySelector(".academy-content");
  try {
    let days = await api.get("/academy/level-1/days");
    if (!days.length) {
      await api.post("/academy/level-1/provision");
      days = await api.get("/academy/level-1/days");
    }
    const next = days.find((day) => !day.evidence_earned) || days[days.length - 1];
    mount(content, el("div", {}, [
      el("div", { class: "academy-continue" }, [
        el("h2", {}, ["Continue Learning"]),
        el("p", {}, [`Day ${next.day} · Week ${next.week} · ${next.kind === "lab" ? "Personal Lab" : "Lecture"} · ${next.estimated_minutes || 60} min`]),
        el("button", { class: "primary", type: "button", onclick: () => navigate(`#/academy/level-1/${next.day}`) }, ["Continue"]),
      ]),
      el("h2", {}, ["Days 1–30"]),
      el("ol", { class: "academy-day-list" }, days.map((day) => el("li", {}, [
        el("a", { href: `#/academy/level-1/${day.day}` }, [`Day ${day.day}: ${day.title}`]),
        el("span", { class: "muted" }, [` · ${day.kind === "lab" ? "Lab" : "Lecture"} · ${day.state}`]),
      ]))),
    ]));
  } catch (err) {
    mount(content, el("p", { class: "error-banner" }, [err.message || "Academy is unavailable."]));
  }
}

export async function renderAcademyDay(root, { day }) {
  mount(root, el("section", { class: "page-section academy-lesson" }, [el("p", {}, ["Loading lesson…"])]));
  try {
    const item = await api.get(`/academy/level-1/days/${encodeURIComponent(day)}`);
    await api.post(`/academy/level-1/items/${encodeURIComponent(item.id)}/open`);
    const children = [
      el("p", { class: "muted" }, [`Day ${item.day} · Week ${item.week} · ${item.kind === "lab" ? "Personal Lab" : "Lecture"} · ${item.estimated_minutes || 60} min`]),
      el("h1", {}, [item.title]),
      item.capability_boundary ? el("p", { class: "notice" }, [item.capability_boundary]) : null,
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
