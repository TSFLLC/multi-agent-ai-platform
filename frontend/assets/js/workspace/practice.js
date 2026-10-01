// AIL5D.6 — Guided Lab and Independent Practice, over the AIL5D.5 Practice Instance API.
//
// Nothing here executes anything or decides anything: a run is a governed Task Run the server creates, the lifecycle is
// derived by the server from saved facts, and the Personal Lab handoff (return target, identifiers) is built by the server.
// Model output is untrusted text and is only ever inserted as text.

import { api } from "../api.js";
import { el } from "../dom.js";
import { renderMarkdown } from "../markdown.js";
import { hintsText, isLauncherLab, labStages, limitsText, practiceActionHint, safeReturnHash } from "./model.js";

const md = (source, cls = "") => el("div", { class: `d5-prose ${cls}`.trim(), html: renderMarkdown(String(source || "")) });
const fill = (host, ...kids) => host.replaceChildren(...kids.filter(Boolean));
const txt = (v) => String(v || "").trim();
const errText = (err) => (err && err.message) || "Something went wrong. Try again.";
const RETURN_KEY = "ail5d6.return";
const P = "/academy/practice";

export function rememberReturn(path) {
  try { sessionStorage.setItem(RETURN_KEY, safeReturnHash(path)); } catch { /* storage may be unavailable; the user can still navigate back */ }
}

function stageBar(practice) {
  return el("ol", { class: "d5-phases", "aria-label": "Lab stages" }, labStages(practice).map((s) => el("li", { class: `d5-phase${s.done ? " done" : ""}${s.current ? " current" : ""}`, "aria-current": s.current ? "step" : null }, [el("i", { "aria-hidden": "true" }, s.done ? "✓" : ""), s.label])));
}

function variableControls(practice, values, onChange, disabled) {
  const vars = practice.scenario.variables || [];
  return el("div", { class: "d5-vars" }, vars.map((v) => {
    const label = el("label", {}, v.label);
    let input;
    if (v.kind === "choice") {
      input = el("select", { class: "d5-select-wide", "aria-label": v.label, disabled: disabled || null, onchange: (e) => onChange(v.key, e.target.value) },
        v.options.map((o) => el("option", { value: o.key, selected: values[v.key] === o.key }, o.label)));
    } else if (v.kind === "model") {
      input = el("select", { class: "d5-select-wide", "aria-label": v.label, disabled: disabled || null, onchange: (e) => onChange(v.key, e.target.value) },
        v.options.map((o) => el("option", { value: o, selected: values[v.key] === o }, o)));
    } else {
      input = el("textarea", { class: "d5-textarea slim", rows: "2", maxlength: String(v.max_chars), "aria-label": v.label, disabled: disabled || null, oninput: (e) => onChange(v.key, e.target.value) });
      input.value = values[v.key] || "";
    }
    return el("div", { class: "d5-var" }, [label, input]);
  }));
}

function runCard(run) {
  const stateText = { running: "Running…", completed: "Finished", failed: "Failed — this run counted but gave no answer" }[run.state] || run.state;
  return el("article", { class: `d5-lab-run ${run.state}` }, [
    el("header", {}, [el("b", {}, `Run ${run.seq}`), el("span", { class: "d5-hint" }, ` · ${stateText}`)]),
    el("p", { class: "d5-vars-line" }, Object.entries(run.variables).map(([k, v]) => `${k}: ${v}`).join("  ·  ")),
    run.output ? el("pre", { class: "d5-code d5-output" }, run.output) : null,
  ]);
}

function compareView(view) {
  if (!view || !view.available) return null;
  const col = (title, r) => el("div", { class: "d5-obs-col" }, [el("small", {}, title), el("p", { class: "d5-vars-line" }, Object.entries(r.variables).map(([k, v]) => `${k}: ${v}`).join(" · ")), el("pre", { class: "d5-code d5-output" }, r.output || "")]);
  return el("section", { class: "d5-labcard" }, [
    el("h3", {}, "Compare your runs"),
    el("p", { class: "d5-hint" }, view.changed.length ? `What you changed: ${view.changed.join(", ")}` : "You ran the same thing twice — change one variable to learn from the difference."),
    el("div", { class: "d5-obs" }, [col(`FIRST (RUN ${view.first.seq})`, view.first), col(`LATEST (RUN ${view.latest.seq})`, view.latest)]),
  ]);
}

function noteForm(label, kind, practice, ctx, redraw, { minRuns = 1, defaults }) {
  const completed = practice.runs.filter((r) => r.state === "completed");
  const ta = el("textarea", { class: "d5-textarea", rows: "3", "aria-label": label, placeholder: label });
  const existing = practice.responses[kind];
  ta.value = existing ? existing.text : "";
  const btn = el("button", { type: "button", class: "d5-btn primary", disabled: completed.length < minRuns || null }, existing ? "Update" : "Save");
  const msg = el("p", { class: "d5-err", role: "alert", hidden: "" });
  btn.addEventListener("click", async () => {
    btn.disabled = true; msg.hidden = true;
    try {
      const body = { kind, text: txt(ta.value) };
      if (kind !== "reflection") body.run_ids = defaults(completed);
      redraw(await api.post(`${P}/${practice.instance_id}/responses`, body));
      await ctx.reload({ keepStep: true, quiet: true });
    } catch (err) { msg.textContent = errText(err); msg.hidden = false; btn.disabled = false; }
  });
  return el("div", { class: "d5-noteform" }, [el("label", {}, label), ta, el("div", { class: "d5-lockbar" }, [btn, msg])]);
}

// A lab that runs on an existing platform engine (agent, workflow, build lab ...): the Workspace launches it through the same server
// call the legacy Day page used, remembers the server-built way back to this exact step, and takes the learner to that engine's page.
function renderEngineLab(step, ctx) {
  const msg = el("p", { class: "d5-err", role: "alert", hidden: "" });
  const open = el("button", { type: "button", class: "d5-btn primary" }, Number(ctx.day) >= 21 ? "Open your Capstone project" : "Open the lab");
  open.addEventListener("click", async () => {
    open.disabled = true; msg.hidden = true;
    try {
      if (Number(ctx.day) >= 21) {
        // The Capstone: one project across the Days, opened through the existing Build With Me capstone orchestration.
        const cap = await api.post(`/academy/level-1/days/${encodeURIComponent(ctx.day)}/start-capstone`);
        rememberReturn(`#/academy/level-1/${ctx.day}?step=${step.key}`);
        if (cap.project_url && String(cap.project_url).startsWith("#/")) window.location.hash = cap.project_url;
        else { open.disabled = false; msg.textContent = "Your Capstone project did not open. Try again."; msg.hidden = false; }
        return;
      }
      const lab = await api.post(`/academy/level-1/days/${encodeURIComponent(ctx.day)}/start-lab`);
      rememberReturn(`#/academy/level-1/${ctx.day}?step=${step.key}`);
      if (lab.experiment_id) window.location.hash = `#/ail/lab/experiments/${encodeURIComponent(lab.experiment_id)}`;
      else if (lab.workflow_run_id) window.location.hash = `#/workflow-runs/${encodeURIComponent(lab.workflow_run_id)}`;
      else if (lab.agent_run_id) window.location.hash = `#/model-intelligence/agent-runs/${encodeURIComponent(lab.agent_run_id)}`;
      else if (lab.project_attempt_id) window.location.hash = `#/academy/projects/attempts/${encodeURIComponent(lab.project_attempt_id)}`;
      else { open.disabled = false; msg.textContent = "This lab did not open anything. Try again."; msg.hidden = false; }
    } catch (err) { open.disabled = false; msg.textContent = errText(err); msg.hidden = false; }
  });
  return el("div", { class: "d5-stack d5-practice", "data-mode": "engine" }, [
    el("span", { class: "d5-modechip gui" }, "Lab — runs on the Personal Lab engine"),
    md(step.content.problem_md, "d5-lead"),
    el("div", { class: "d5-lockbar" }, [open, el("span", { class: "d5-hint" }, "Your work is recorded there. Come back here with “Back to your lesson”, then reflect.")]),
    msg,
  ]);
}

export function renderPractice(step, ctx) {
  if (isLauncherLab(step)) return renderEngineLab(step, ctx);
  const independent = step.type === "practice";
  const host = el("div", { class: "d5-stack d5-practice", "data-mode": independent ? "independent" : "guided" });
  const prompt = independent ? step.content.prompt_md : step.content.problem_md;
  const summary = step.practice || { instances: [] };
  let practice = null;
  let values = {};
  let error = "";
  let poll = null;

  const stop = () => { if (poll) { clearInterval(poll); poll = null; } };
  ctx.onCleanup(stop);
  ctx.onHelp(async () => { if (practice) { try { practice = await api.get(`${P}/${practice.instance_id}`); draw(); } catch { /* the next refresh will show it */ } } });

  const load = async (id) => { practice = await api.get(`${P}/${id}`); schedule(); draw(); };
  const schedule = () => {
    stop();
    if (practice && practice.runs.some((r) => r.state === "running")) {
      poll = setInterval(async () => {
        try { practice = await api.get(`${P}/${practice.instance_id}`); if (!practice.runs.some((r) => r.state === "running")) { stop(); await ctx.reload({ keepStep: true, quiet: true }); } draw(); } catch { /* keep polling */ }
      }, 3000);
    }
  };
  const start = async () => {
    error = "";
    try { practice = await api.post(P, { learning_item_id: ctx.itemId, step_key: step.key }); values = {}; schedule(); } catch (err) { error = errText(err); }
    draw();
  };
  const run = async () => {
    error = "";
    try { practice = await api.post(`${P}/${practice.instance_id}/runs`, { variables: values }); schedule(); } catch (err) { error = errText(err); }
    draw();
  };
  const abandon = async () => {
    try { await api.post(`${P}/${practice.instance_id}/abandon`); practice = null; await ctx.reload({ keepStep: true, quiet: true }); } catch (err) { error = errText(err); draw(); }
  };
  const openInLab = async (run) => {
    try {
      const handoff = await api.get(`${P}/${practice.instance_id}/handoff`);
      rememberReturn(handoff.return.path);
      window.location.hash = `#/model-intelligence/agent-runs/${encodeURIComponent(run.agent_run_id)}`;
    } catch (err) { error = errText(err); draw(); }
  };

  const intro = () => el("div", { class: "d5-stack" }, [
    el("span", { class: `d5-modechip ${independent ? "ind" : "gui"}` }, independent ? "Independent practice — you lead, the Professor only hints" : "Guided lab — predict, run, observe, change, rerun, compare, reflect"),
    md(prompt, "d5-lead"),
    independent ? el("p", { class: "d5-note" }, "A fresh case. Less guidance, hints on request, retries allowed, safe to fail. You can practice again with a new case.") : null,
  ]);

  const draw = () => {
    if (!practice) {
      const finished = summary.instances.filter((i) => i.status === "completed").length;
      const active = summary.instances.find((i) => !["completed", "abandoned"].includes(i.status));
      fill(host, intro(),
        finished ? el("p", { class: "d5-hint" }, `Completed ${finished} time${finished === 1 ? "" : "s"}.`) : null,
        el("div", { class: "d5-lockbar" }, [
          el("button", { type: "button", class: "d5-btn primary", onclick: start }, active ? "Continue" : finished ? (independent ? "Practice again" : "Run the lab again") : independent ? "Practice this concept" : "Start the lab"),
          el("span", { class: "d5-hint" }, active ? "Picks up exactly where you left off." : "Opens your own copy; the original lab is never changed."),
        ]),
        error ? el("p", { class: "d5-err", role: "alert" }, error) : null);
      return;
    }
    const p = practice;
    const done = p.status === "completed";
    const running = p.runs.some((r) => r.state === "running");
    const needPredictions = (p.scenario.predictions || []).length > 0 && !p.met.prediction;
    const sc = p.scenario;
    const canRun = !done && p.status !== "abandoned" && !needPredictions && !running && p.limits.runs_left > 0 && !p.limits.budget_exhausted;
    const hints = hintsText(p.assistance);

    const predictions = (sc.predictions || []).length ? el("section", { class: `d5-labcard${done || p.runs.length ? " locked" : ""}` }, [
      el("h3", {}, "1 · Predict before you run anything"),
      ...sc.predictions.map((pr) => {
        const saved = p.responses.predictions[pr.id];
        const ta = el("textarea", { class: "d5-textarea slim", rows: "2", "aria-label": pr.text, disabled: p.runs.length || done ? true : null });
        ta.value = saved ? saved.text : "";
        const msg = el("span", { class: "d5-err", role: "alert", hidden: "" });
        const save = el("button", { type: "button", class: "d5-btn small", disabled: p.runs.length || done ? true : null }, saved ? "Update" : "Save");
        save.addEventListener("click", async () => {
          try { practice = await api.post(`${P}/${p.instance_id}/responses`, { kind: "prediction", prediction_id: pr.id, text: txt(ta.value) }); draw(); } catch (err) { msg.textContent = errText(err); msg.hidden = false; }
        });
        return el("div", { class: "d5-pred" }, [el("label", {}, pr.text), ta, el("div", { class: "d5-lockbar" }, [save, msg])]);
      }),
      p.runs.length ? el("p", { class: "d5-hint" }, "Predictions are locked — you made them before running.") : null,
    ]) : null;

    const control = el("section", { class: `d5-labcard${needPredictions ? " dim" : ""}` }, [
      el("h3", {}, `${(sc.predictions || []).length ? "2 · " : "1 · "}Run it`),
      el("p", { class: "d5-hint" }, independent ? "Choose what to run. Change one thing between runs and see what happens." : "Change ONE variable between runs, so you can tell what caused the difference."),
      variableControls(p, values, (k, v) => { values[k] = v; }, !canRun),
      el("div", { class: "d5-lockbar" }, [
        el("button", { type: "button", class: "d5-btn primary", disabled: !canRun || null, onclick: run }, running ? "Running…" : p.runs.length ? "Run again" : "Run"),
        el("span", { class: "d5-hint" }, `${limitsText(p.limits)}${p.limits.runs_left === 0 ? " — you have used every run of this practice" : ""}`),
      ]),
      running ? el("p", { class: "d5-hint", role: "status" }, "Your run is in progress — this updates by itself.") : null,
    ]);

    const results = p.runs.length ? el("section", { class: "d5-labcard" }, [
      el("h3", {}, "What happened"),
      ...p.runs.map((r) => el("div", {}, [runCard(r), el("button", { type: "button", class: "d5-btn small ghost", onclick: () => openInLab(r) }, "View in Personal Lab ↗")])),
    ]) : null;

    const req = sc.requirements || {};
    const completedRuns = p.runs.filter((r) => r.state === "completed");
    const pick = (list) => list.map((r) => r.run_id);
    const forms = [];
    if (req.require_observation && completedRuns.length) forms.push(noteForm(sc.observation_prompt_md || "What did you observe?", "observation", p, ctx, (v) => { practice = v; draw(); }, { defaults: pick }));
    if (req.require_comparison && p.met.change) forms.push(noteForm(sc.comparison_prompt_md || "How do the runs compare, and why?", "comparison", p, ctx, (v) => { practice = v; draw(); }, { minRuns: 2, defaults: (c) => [c[0].run_id, c[c.length - 1].run_id] }));
    if (req.require_reflection && p.met.runs && p.met.change && (!req.require_observation || p.responses.observation) && (!req.require_comparison || p.responses.comparison)) {
      forms.push(noteForm(sc.reflection_prompt_md || "Reflect on what you learned.", "reflection", p, ctx, (v) => { practice = v; draw(); }, { defaults: () => [] }));
    }

    fill(host, intro(),
      el("div", { class: "d5-phasebar" }, [stageBar(p), hints ? el("span", { class: "d5-hintsused", title: "Counted from your Professor help on this practice" }, hints) : null]),
      sc.objective_md ? md(sc.objective_md) : null,
      el("div", { class: "d5-note" }, [el("b", {}, "Instructions: "), md(sc.instructions_md)]),
      predictions, control, results, compareView(p.comparison_view),
      forms.length ? el("section", { class: "d5-labcard" }, [el("h3", {}, "Make sense of it"), ...forms]) : null,
      done ? el("section", { class: "d5-labdone" }, [
        el("h3", {}, "✓ Practice complete"),
        el("ul", { class: "d5-donelist" }, [
          el("li", {}, [el("b", {}, "Recorded as practice: "), "this builds your skill. It is not an assessment and does not on its own show that you know this."]),
          hints ? el("li", {}, [el("b", {}, "Help used: "), hints]) : null,
        ]),
        p.lab_answer_md ? el("div", { class: "d5-reveal" }, [el("small", {}, "WHAT TO NOTICE"), md(p.lab_answer_md)]) : null,
        el("div", { class: "d5-lockbar" }, [el("button", { type: "button", class: "d5-btn primary", onclick: async () => { practice = null; await start(); } }, independent ? "Practice again" : "Run the lab again")]),
      ]) : el("p", { class: "d5-action-hint", role: "status" }, practiceActionHint(p)),
      !done && p.status !== "abandoned" ? el("div", { class: "d5-lockbar" }, [el("button", { type: "button", class: "d5-btn small ghost", onclick: abandon }, "Start over with a fresh copy"), el("span", { class: "d5-hint" }, "Your earlier work stays on record; this counts as an attempt.")]) : null,
      error ? el("p", { class: "d5-err", role: "alert" }, error) : null);
  };

  const wanted = ctx.practiceId;
  const active = summary.instances.find((i) => !["completed", "abandoned"].includes(i.status));
  const resume = (wanted && summary.instances.find((i) => i.instance_id === wanted && ctx.stepKey === step.key)) || active;
  draw();
  if (resume) load(resume.instance_id).catch((err) => { error = errText(err); draw(); });
  return host;
}
