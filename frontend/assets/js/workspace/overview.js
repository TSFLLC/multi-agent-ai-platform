// AIL5D.6 — Program Overview: 30-Day Practical AI Foundations. Learning status and demonstrated status are shown
// separately, and opening a Day is never shown as completing it.

import { api } from "../api.js";
import { assessmentDefinitionHref, ASSESSMENT_CENTER_HREF } from "../academyLinks.js";
import { el, mount } from "../dom.js";
import { dayGroups, dayLearned, dayStatus, nextDay, programProgress } from "./model.js";

const STATUS = {
  demonstrated: ["★", "Demonstrated", "g-ready"],
  learned: ["✓", "Learning complete", "g-done"],
  current: ["▶", "Up next", "g-current"],
  upcoming: ["", "Not started", "g-upcoming"],
};
const KIND = { lab: "Lab", lecture: "Lesson" };
const clean = (title) => String(title || "").replace(/^Day \d+:\s*/, "");

function dayRow(d, upNext) {
  const status = dayStatus(d, upNext);
  const [glyph, label, cls] = STATUS[status];
  return el("li", { class: `d5-dayrow s-${status}` }, [
    el("a", { class: "d5-dayrow-link", href: `#/academy/level-1/${d.day}` }, [
      el("span", { class: `d5-glyph ${cls}`, "aria-hidden": "true" }, glyph),
      el("span", { class: "d5-dayrow-main" }, [el("b", {}, `Day ${d.day} · ${clean(d.title)}`), el("small", {}, `${KIND[d.kind] || d.kind} · ${d.estimated_minutes || 60} min${d.capstone_stage ? ` · Capstone: ${d.capstone_stage}` : ""}`)]),
      el("span", { class: "d5-dayrow-status" }, label),
    ]),
  ]);
}

export async function renderProgramOverview(root) {
  mount(root, el("div", { class: "ws-root" }, [el("section", { class: "d5-page" }, [el("p", { class: "d5-muted", role: "status" }, "Loading your program…")])]));
  const page = root.querySelector(".d5-page");
  try {
    let days = await api.get("/academy/level-1/days");
    if (!days.length) {
      await api.post("/academy/level-1/provision");
      days = await api.get("/academy/level-1/days");
    }
    const next = nextDay(days);
    const p = programProgress(days);
    const weeks = dayGroups(days.filter((d) => !d.capstone_stage));
    const capstone = days.filter((d) => d.capstone_stage);
    mount(page, el("div", { class: "d5-stack" }, [
      el("header", {}, [el("p", { class: "d5-crumbs" }, "AI Academy"), el("h1", { class: "d5-pagetitle" }, "30-Day Practical AI Foundations"), el("p", { class: "d5-sub" }, "Learn it, practice it, then demonstrate it. Finishing a lesson is not the same as demonstrating that you know it.")]),
      el("section", { class: "d5-overview-top" }, [
        el("div", { class: "d5-ringcard" }, [
          el("div", { class: "d5-bar big", role: "progressbar", "aria-valuenow": String(p.pct), "aria-valuemin": "0", "aria-valuemax": "100" }, [el("span", { style: `width:${p.pct}%` })]),
          el("p", {}, [el("b", {}, `${p.learned} of ${p.total} days learned`), ` · ${p.demonstrated} demonstrated`]),
          el("small", { class: "d5-muted" }, "Learned = every required step done. Demonstrated = shown in an assessment."),
        ]),
        el("div", { class: "d5-continue" }, [
          el("small", {}, p.learned === p.total ? "PROGRAM LEARNED" : "UP NEXT"),
          el("b", {}, `Day ${next.day} · ${clean(next.title)}`),
          el("small", { class: "d5-muted" }, `${KIND[next.kind] || next.kind} · ${next.estimated_minutes || 60} min`),
          el("a", { class: "d5-btn primary", href: `#/academy/level-1/${next.day}` }, dayLearned(next) ? "Review" : "Continue"),
        ]),
      ]),
      ...weeks.map((g) => el("section", { class: "d5-week" }, [
        el("h2", {}, `Week ${g.week}`),
        el("ul", { class: "d5-daylist" }, g.days.map((d) => dayRow(d, next.day))),
      ])),
      capstone.length ? el("section", { class: "d5-week d5-capstone" }, [
        el("h2", {}, "Capstone · Days 21–30"),
        el("p", { class: "d5-sub" }, "One meaningful project, built progressively: define, design, build, test, diagnose, improve, evaluate, explain, demonstrate, review."),
        el("ul", { class: "d5-daylist" }, capstone.map((d) => dayRow(d, next.day))),
      ]) : null,
      el("p", { class: "d5-muted" }, [el("a", { href: ASSESSMENT_CENTER_HREF }, "Assessment Center"), " · assessments are graded separately, with the AI Professor paused."]),
    ]));
  } catch (err) {
    mount(page, el("p", { class: "d5-err", role: "alert" }, (err && err.message) || "Academy is unavailable."));
  }
  void assessmentDefinitionHref;
}
