// AIL5D.6 — the production Day Learning Workspace:  COURSE NAV | CURRENT LEARNING STEP | AI PROFESSOR
// (phone: Lesson | Professor | Course). One step at a time. The server owns progress, completion, reveals, grading and
// assistance; this page draws what it is told and asks the server to record what the learner does.

import { api } from "../api.js";
import { assessmentDefinitionHref } from "../academyLinks.js";
import { el, mount } from "../dom.js";
import { MODE_COPY, assessmentSummary, canContinue, continueHint, initialStepKey, isDone, isLauncherLab, isSkipped, learningStatusLine, parseWorkspaceHash, progressText, stepIndex, stepMeta, workspaceMode } from "./model.js";
import { professorPanel } from "./professor.js";
import { renderStepBody } from "./steps.js";

const fill = (host, ...kids) => host.replaceChildren(...kids.filter(Boolean));
const errText = (err) => (err && err.message) || "Something went wrong. Try again.";

export async function renderWorkspace(root, day, learning, days) {
  const hashState = parseWorkspaceHash(window.location.hash);
  const itemId = learning.item_id;
  const cleanups = [];
  let stepCleanups = [];
  let helpHooks = [];
  let view = learning;
  let current = initialStepKey(view, hashState.step);
  let practiceId = hashState.practice;
  let checkFeedback = null;
  let showSummary = false;
  let tab = "lesson";
  let profOpen = true;
  let busy = false;
  let message = "";

  const ws = el("div", { class: "d5-ws", "data-tab": tab });
  const left = el("div", { class: "d5-slot-left" });
  const right = el("div", { class: "d5-slot-right" });
  const center = el("main", { class: "d5-center" });
  const tabs = el("nav", { class: "d5-mtabs", role: "tablist", "aria-label": "Workspace sections" });
  ws.append(left, center, right, tabs);
  const shell = el("div", { class: "ws-root" }, [ws]);
  document.body.classList.add("ws-mode", "ws-active");
  mount(root, shell);

  const step = () => view.steps.find((s) => s.key === current) || view.steps[0];
  const setHash = () => { try { history.replaceState(null, "", `#/academy/level-1/${day}?step=${encodeURIComponent(current)}${practiceId && step().type !== "teach" ? `&practice=${encodeURIComponent(practiceId)}` : ""}`); } catch { /* the hash is a convenience */ } };

  async function fetchView() {
    const fresh = await api.get(`/academy/level-1/days/${encodeURIComponent(day)}/learning`);
    if (fresh.item_id !== itemId) { window.location.hash = `#/academy/level-1/${day}`; return null; }
    return fresh;
  }

  // reload({keepStep, quiet, feedback}): re-read the server's view. ``quiet`` refreshes only the chrome (nav, action bar) so
  // a lab in progress is not redrawn under the learner.
  const ctx = {
    get itemId() { return itemId; },
    get day() { return day; },
    get practiceId() { return practiceId; },
    get stepKey() { return current; },
    get checkFeedback() { return checkFeedback; },
    onCleanup: (fn) => stepCleanups.push(fn),
    // The step body can ask to be told when the Professor delivered help (e.g. a practice refreshing "hints used").
    onHelp: (fn) => helpHooks.push(fn),
    async reload({ keepStep = true, quiet = false, feedback } = {}) {
      if (feedback) checkFeedback = feedback;
      const fresh = await fetchView();
      if (!fresh) return;
      view = fresh;
      if (!keepStep) current = initialStepKey(view, null);
      if (quiet) { paintLeft(); paintActions(); } else drawAll();
    },
  };

  // -- course navigation (left) --------------------------------------------------------------------------------------------------
  function paintLeft() {
    const required = view.required_total || 0;
    const pct = required ? Math.round(((view.required_completed || 0) / required) * 100) : 0;
    const sameWeek = (days || []).filter((d) => d.week === view.week && d.day !== view.day);
    const summary = assessmentSummary(view.assessment);
    fill(left, el("nav", { class: "d5-course", "aria-label": "Course navigation" }, [
      el("a", { class: "d5-back", href: "#/academy/level-1" }, "← Program overview"),
      el("div", { class: "d5-course-prog" }, [
        el("small", {}, `WEEK ${view.week}`), el("b", {}, `Day ${view.day} · ${view.title.replace(/^Day \d+:\s*/, "")}`),
        el("div", { class: "d5-bar", role: "progressbar", "aria-valuenow": String(pct), "aria-valuemin": "0", "aria-valuemax": "100" }, [el("span", { style: `width:${pct}%` })]),
        el("span", { class: "d5-muted" }, progressText(view)),
      ]),
      el("ol", { class: "d5-steplist" }, view.steps.map((s) => {
        const st = isDone(s) ? "done" : isSkipped(s) ? "skipped" : s.key === current && !showSummary ? "current" : s.status === "opened" ? "opened" : "todo";
        return el("li", {}, el("button", { type: "button", class: `d5-stepitem st-${st}`, "aria-current": s.key === current && !showSummary ? "step" : null, onclick: () => go(s.key) }, [
          el("span", { class: "d5-stepmark" }, st === "done" ? "✓" : ""),
          el("span", { class: "d5-steptext" }, [el("span", { class: "t" }, s.title), el("small", {}, `${stepMeta(s.type).label} · ${s.estimated_minutes} min · ${s.required ? "required" : "optional"}`)]),
        ]));
      })),
      el("button", { type: "button", class: "d5-course-assess", onclick: () => { showSummary = true; drawAll(); tabTo("lesson"); } }, [
        el("div", {}, [el("b", {}, summary ? "Demonstrate" : "Day summary"), el("small", {}, summary ? `Assessment: ${summary.label}` : learningStatusLine(view).learning)]),
      ]),
      sameWeek.length ? el("details", { class: "d5-course-more" }, [el("summary", {}, "Later this week"), el("ul", {}, sameWeek.map((d) => el("li", {}, [el("a", { href: `#/academy/level-1/${d.day}` }, `Day ${d.day} · ${d.title.replace(/^Day \d+:\s*/, "")}`)])))]) : null,
    ]));
  }

  function go(key) { current = key; showSummary = false; message = ""; checkFeedback = null; setHash(); drawAll(); tabTo("lesson"); }

  // -- the action bar: Previous / hint / Skip / Continue ---------------------------------------------------------------------------
  const actionbar = el("footer", { class: "d5-actionbar" });
  function paintActions() {
    const s = step();
    const i = stepIndex(view.steps, s.key);
    const last = i === view.steps.length - 1;
    const ok = canContinue(s);
    const optionalOpen = !s.required && !isDone(s) && !isSkipped(s);
    const hint = message || (showSummary ? "" : continueHint(s));
    fill(actionbar,
      el("button", { type: "button", class: "d5-btn ghost", disabled: i === 0 && !showSummary || null, onclick: () => (showSummary ? go(view.steps[view.steps.length - 1].key) : go(view.steps[i - 1].key)) }, "← Previous"),
      el("span", { class: "d5-action-hint", role: "status" }, hint),
      ...(showSummary ? [] : [
        optionalOpen && s.type !== "teach" && s.type !== "example" ? el("button", { type: "button", class: "d5-btn ghost", disabled: busy || null, onclick: () => skip(s, last) }, "Skip for now") : null,
        el("button", { type: "button", class: "d5-btn primary", disabled: !ok || busy || null, onclick: () => advance(s, last) }, last ? "Finish learning →" : "Continue →"),
      ]),
    );
  }

  async function advance(s, last) {
    busy = true; message = ""; paintActions();
    try {
      if (!isDone(s) && !isSkipped(s)) await api.post(`/academy/level-1/items/${itemId}/steps/${s.key}/${isLauncherLab(s) ? "skip" : "complete"}`);
      const fresh = await fetchView();
      if (fresh) view = fresh;
      busy = false;
      if (last) { showSummary = true; drawAll(); } else go(view.steps[stepIndex(view.steps, s.key) + 1].key);
    } catch (err) { busy = false; message = errText(err); paintActions(); }
  }
  async function skip(s, last) {
    busy = true; paintActions();
    try {
      await api.post(`/academy/level-1/items/${itemId}/steps/${s.key}/skip`);
      const fresh = await fetchView();
      if (fresh) view = fresh;
      busy = false;
      if (last) { showSummary = true; drawAll(); } else go(view.steps[stepIndex(view.steps, s.key) + 1].key);
    } catch (err) { busy = false; message = errText(err); paintActions(); }
  }

  // -- the centre: one step ------------------------------------------------------------------------------------------------------------
  function summaryPane() {
    const status = learningStatusLine(view);
    const a = assessmentSummary(view.assessment);
    const next = (days || []).find((d) => d.day === view.day + 1);
    return el("article", { class: "d5-step type-summary" }, [
      el("header", { class: "d5-step-head" }, [el("div", { class: "d5-step-meta" }, [el("span", { class: "d5-modechip ind" }, "Day summary")]), el("h1", { class: "d5-step-title" }, view.learning_complete ? "You've finished learning Day " + view.day : "Day " + view.day + " is not finished yet")]),
      el("div", { class: "d5-stack" }, [
        el("ul", { class: "d5-donelist" }, [
          el("li", {}, [el("b", {}, status.learning), view.learning_complete ? " — every required step is done." : ` — ${progressText(view)} so far.`]),
          el("li", {}, [el("b", {}, status.demonstrated), " — demonstrating is separate from learning. Finishing the lesson never counts as having shown you can apply it."]),
          a ? el("li", {}, [el("b", {}, `Assessment: ${a.label}`), ` — ${a.note}`]) : null,
        ]),
        reviewPanel(),
        a ? el("section", { class: "d5-labcard" }, [
          el("h3", {}, "Demonstrate what you learned"),
          el("p", { class: "d5-hint" }, "An authored assessment for this Day, graded separately. The AI Professor pauses while it is open, and nothing is recorded until you submit."),
          el("a", { class: "d5-btn primary", href: assessmentDefinitionHref(a.key) }, a.status === "not_started" ? "Take the assessment" : "Open the assessment"),
        ]) : null,
        el("div", { class: "d5-lockbar" }, [
          next ? el("a", { class: "d5-btn", href: `#/academy/level-1/${next.day}` }, `Day ${next.day} →`) : null,
          el("a", { class: "d5-btn ghost", href: "#/academy/level-1" }, "Program overview"),
        ]),
      ]),
    ]);
  }

  function runStepCleanups() {
    const fns = stepCleanups;
    stepCleanups = [];
    helpHooks = [];
    fns.forEach((fn) => { try { fn(); } catch { /* cleanup must never block the page */ } });
  }

  // The Capstone is one project across ten Days: show the learner's OWN earlier artifacts (read from the server, so they survive
  // navigation and sessions). Everything is inserted as text.
  function capstonePanel() {
    const cap = view.capstone;
    if (!cap || !cap.artifacts.length) return null;
    return el("details", { class: "d5-capstone-sofar", open: cap.artifacts.some((a) => a.text) ? "" : null }, [
      el("summary", {}, `Your Capstone so far · ${cap.artifacts.filter((a) => a.text).length} of ${cap.artifacts.length} earlier artifacts saved`),
      ...cap.artifacts.map((a) => el("section", { class: "d5-capstone-art" }, [
        el("h4", {}, `Day ${a.day} · ${a.stage}`),
        a.text ? el("p", { class: "d5-capstone-text" }, a.text) : el("p", { class: "d5-hint" }, `Not written yet — it will appear here once you save your Day ${a.day} artifact.`),
      ])),
    ]);
  }

  // Day 30: the existing evidence-only review (strengths, what to revisit, recommendations), read from the server.
  function reviewPanel() {
    if (view.day !== 30) return null;
    const host = el("section", { class: "d5-labcard" }, [el("h3", {}, "Your portfolio review and next-learning plan"), el("p", { class: "d5-hint", role: "status" }, "Reading your evidence record…")]);
    api.get("/academy/level-1/days/30/review").then((r) => {
      const list = (title, items) => el("div", {}, [el("h4", {}, title), items.length ? el("ul", {}, items.map((x) => el("li", {}, x))) : el("p", { class: "d5-hint" }, "Nothing yet.")]);
      fill(host, el("h3", {}, "Your portfolio review and next-learning plan"), el("p", { class: "d5-hint" }, `Derived from your own evidence record (${r.assessment_results_consulted} assessment result${r.assessment_results_consulted === 1 ? "" : "s"} consulted). It never grades you or changes what you have demonstrated.`),
        list("Strengths", r.strengths), list("To revisit", r.revisit), list("Recommendations", r.recommendations));
    }).catch((err) => fill(host, el("h3", {}, "Your portfolio review and next-learning plan"), el("p", { class: "d5-err", role: "alert" }, errText(err))));
    return host;
  }

  function paintCenter() {
    runStepCleanups();
    const s = step();
    const mode = workspaceMode(s, { inAssessment: false });
    const meta = stepMeta(s.type);
    const body = showSummary ? summaryPane() : el("article", { class: `d5-step type-${s.type}`, "data-step-type": s.type }, [
      el("header", { class: "d5-step-head" }, [
        el("div", { class: "d5-step-meta" }, [el("span", { class: `d5-typechip t-${s.type}` }, [el("i", { "aria-hidden": "true" }, meta.icon), meta.label]), el("span", { class: "d5-dot" }, "·"), el("span", {}, `${s.estimated_minutes} min`), s.required ? null : el("span", { class: "d5-optional" }, "Optional")]),
        el("h1", { class: "d5-step-title" }, s.title),
      ]),
      renderStepBody(s, ctx),
    ]);
    fill(center,
      el("div", { class: "d5-center-scroll" }, [
        el("div", { class: "d5-center-top" }, [
          el("span", { class: "d5-center-crumb" }, `Day ${view.day} · step ${showSummary ? view.steps.length : stepIndex(view.steps, s.key) + 1} of ${view.steps.length}`),
          el("span", { class: `d5-mode m-${mode}`, title: MODE_COPY[mode].note }, MODE_COPY[mode].label),
          el("button", { type: "button", class: "d5-askpill", onclick: () => { profOpen = true; paintRight(); tabTo("professor"); } }, [el("span", { class: "av sm" }, "P"), "Ask the Professor"]),
        ]),
        el("div", { class: "d5-center-body" }, [showSummary ? null : capstonePanel(), body]),
      ]),
      actionbar);
    paintActions();
  }

  // -- the Professor (right) ---------------------------------------------------------------------------------------------------------------
  function paintRight() {
    if (showSummary) {
      fill(right, el("aside", { class: "d5-prof" }, [el("header", { class: "d5-prof-head" }, [el("span", { class: "av" }, "P"), el("div", {}, [el("b", {}, "AI Professor"), el("small", {}, "Pick a step to ask about it")])]), el("div", { class: "d5-prof-body" }, [el("p", { class: "d5-hint" }, "The Professor helps with one step at a time. Choose a step on the left, or open the assessment — the Professor is paused there.")])]));
      return;
    }
    if (!profOpen) {
      fill(right, el("button", { type: "button", class: "d5-prof-rail", "aria-label": "Open the AI Professor", onclick: () => { profOpen = true; paintRight(); ws.classList.remove("prof-closed"); } }, [el("span", { class: "av" }, "P"), el("span", { class: "v" }, "AI Professor")]));
      ws.classList.add("prof-closed");
      return;
    }
    ws.classList.remove("prof-closed");
    const panel = professorPanel(step(), ctx, { onHelp: () => { helpHooks.forEach((fn) => { try { fn(); } catch { /* a hook must never break the Professor */ } }); } });
    const collapse = el("button", { type: "button", class: "d5-x", "aria-label": "Collapse the Professor", onclick: () => { profOpen = false; paintRight(); } }, "⟩");
    panel.querySelector(".d5-prof-head").appendChild(collapse);
    fill(right, panel);
  }

  // -- mobile tabs: Lesson | Professor | Course ----------------------------------------------------------------------------------------------
  function tabTo(k) {
    tab = k; ws.dataset.tab = k;
    tabs.querySelectorAll("button").forEach((b) => { const on = b.dataset.k === k; b.classList.toggle("on", on); b.setAttribute("aria-selected", String(on)); });
  }
  fill(tabs, ...[["lesson", "Lesson"], ["professor", "Professor"], ["course", "Course"]].map(([k, label]) => el("button", { type: "button", role: "tab", "data-k": k, class: tab === k ? "on" : "", "aria-selected": String(tab === k), onclick: () => tabTo(k) }, label)));

  let openedKey = null;
  function drawAll() {
    paintLeft();
    paintCenter();
    paintRight();
    const s = step();
    // Opening a step only records that it was viewed; it never completes it.
    if (!showSummary && openedKey !== `${view.version}:${s.key}`) {
      openedKey = `${view.version}:${s.key}`;
      api.post(`/academy/level-1/items/${itemId}/steps/${s.key}/open`).catch(() => {});
    }
  }

  setHash();
  drawAll();
  return () => {
    runStepCleanups();
    cleanups.forEach((fn) => { try { fn(); } catch { /* cleanup must never block navigation */ } });
    document.body.classList.remove("ws-mode", "ws-active");
  };
}
