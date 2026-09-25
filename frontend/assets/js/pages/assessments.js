// AIL.5C learner experience: Assessment Center, instructions, workspace,
// result / needs-work, Demonstration Record, review request, reviewer queue.
// All decisions live in ../assessments.js; this module only draws and calls the API.

import { api } from "../api.js";
import { clear, el } from "../dom.js";
import {
  DECLARATIONS, REVIEW_SEES, assessmentModeBanner, attemptStatusText, buildDraft, centerModel, confidenceLabel, costLabel,
  declarationNote, decisionLabel, effectLabel, emptyCenterText, findingLabel, helpLabel, kindLabel, outcomeInfo, readinessModel,
  recordFileName, recordModel, remediationLink, resultModel, reviewPayload, sourceWorkText, timeLeftText, validateSubmit,
} from "../assessments.js";

const BASE = "/academy/assessments";
const enc = encodeURIComponent;

const loading = (root, text = "Loading assessments…") => { clear(root); root.appendChild(el("div", { class: "loading-state" }, text)); };
const failure = (root, err) => { clear(root); root.appendChild(el("div", { class: "error-banner" }, (err && err.message) || "Assessments could not be loaded.")); };
const nav = () => el("div", { class: "row" }, [
  el("a", { class: "button-link", href: "#/academy" }, "Academy Home"),
  el("a", { class: "button-link", href: "#/academy/assessments" }, "Assessment Center"),
]);
const header = (title, subtitle) => el("div", { class: "page-header" }, [el("h1", {}, title), subtitle ? el("p", { class: "subtitle" }, subtitle) : null, nav()]);
const badge = (text, tone = "neutral") => el("span", { class: `badge badge-${tone === "good" ? "done" : tone === "warn" ? "paid" : "neutral"}` }, text);
const hashQuery = () => new URLSearchParams(window.location.hash.split("?")[1] || "");
const withBusy = async (button, work) => { button.disabled = true; try { return await work(); } finally { button.disabled = false; } };

// -- A. Assessment Center ------------------------------------------------------------------------------------------------

function centerCard(section, item) {
  const key = section.key;
  if (key === "ready_for_assessment" || key === "not_yet_ready") {
    const card = el("article", { class: "card assessment-card" }, [
      el("div", { class: "row between" }, [el("h3", {}, item.title), badge(kindLabel(item.kind))]),
      el("p", {}, item.why_offered),
      item.fresh_required ? el("p", { class: "hint" }, "A fresh challenge will be part of this assessment.") : null,
      item.available_after_ma9 ? el("p", { class: "hint" }, "Available after MA9.") : null,
    ]);
    const params = item.project_attempt_id ? `?project_attempt_id=${enc(item.project_attempt_id)}` : "";
    card.appendChild(el("a", { class: "button-link", href: `#/academy/assessments/definitions/${enc(item.definition_key)}${params}` }, key === "ready_for_assessment" ? "See what is assessed" : "What is missing"));
    return card;
  }
  if (key === "demonstrated") {
    return el("article", { class: "card assessment-card" }, [
      el("div", { class: "row between" }, [el("h3", {}, item.title), badge(item.status === "valid" ? "Current" : item.status.replaceAll("_", " "), item.status === "valid" ? "good" : "warn")]),
      el("p", {}, item.concepts.join(" · ")),
      item.status_reasons.length ? el("p", { class: "hint" }, item.status_reasons.join(" ")) : null,
      el("div", { class: "row" }, [
        el("a", { class: "button-link", href: `#/academy/assessments/records/${enc(item.result_id)}` }, "Open Demonstration Record"),
        item.reassess ? el("a", { class: "button-link", href: `#/academy/assessments/definitions/${enc(item.reassess.definition_key)}` }, "Reassess") : null,
      ]),
    ]);
  }
  const info = outcomeInfo(item.outcome);
  return el("article", { class: "card assessment-card" }, [
    el("div", { class: "row between" }, [el("h3", {}, item.title), badge(item.outcome ? info.label : attemptStatusText({ status: item.status }), info.tone)]),
    el("p", { class: "hint" }, `${kindLabel(item.kind)} · v${item.definition_version}${item.finalized_at ? ` · finished ${String(item.finalized_at).slice(0, 10)}` : ""}`),
    item.gaps && item.gaps.length ? el("ul", {}, item.gaps.map((g) => el("li", {}, `${g.label}: ${g.detail || findingLabel(g.finding)}`))) : null,
    el("a", { class: "button-link", href: `#/academy/assessments/attempts/${enc(item.attempt_id)}` }, key === "in_progress" ? "Continue" : "Open result"),
  ]);
}

export async function renderAssessmentCenter(root) {
  loading(root);
  try {
    const model = centerModel(await api.get(`${BASE}/center`));
    clear(root);
    root.appendChild(header("Assessment Center", "Show that you can explain and apply what you learned — on your own, with evidence."));
    if (model.empty) { root.appendChild(el("div", { class: "empty-state" }, emptyCenterText)); return; }
    model.visible.forEach((section) => {
      const body = el("div", { class: "stack" }, section.items.map((item) => centerCard(section, item)));
      const wrap = section.key === "not_yet_ready" || section.key === "history"
        ? el("details", { class: "card" }, [el("summary", {}, `${section.title} (${section.items.length})`), el("p", { class: "hint" }, section.hint), body])
        : el("section", { class: "stack", "data-section": section.key }, [el("h2", {}, section.title), el("p", { class: "hint" }, section.hint), body]);
      root.appendChild(wrap);
    });
  } catch (err) { failure(root, err); }
}

// -- B + C. Ready for assessment + instructions ---------------------------------------------------------------------------------

export async function renderAssessmentDefinition(root, params) {
  loading(root);
  try {
    const projectAttemptId = hashQuery().get("project_attempt_id");
    const query = projectAttemptId ? `?project_attempt_id=${enc(projectAttemptId)}` : "";
    const readiness = await api.get(`${BASE}/definitions/${enc(params.key)}/readiness${query}`);
    const model = readinessModel(readiness);
    const def = readiness.definition;
    clear(root);
    root.appendChild(header(def ? def.title : "Assessment", def ? `${kindLabel(def.kind)} · version ${def.version}` : "This assessment is not available."));
    root.appendChild(el("section", { class: "card" }, [
      el("h2", {}, model.ready ? "You are ready" : "Not ready yet"),
      el("ul", { class: "checklist" }, readiness.checks.map((c) => el("li", { class: c.met ? "check-met" : "check-unmet" }, [
        el("strong", {}, `${c.met ? "✓" : "✗"} ${c.label}`), c.detail ? el("span", { class: "hint" }, ` — ${c.detail}`) : null,
      ]))),
      sourceWorkText(model.sourceWork) ? el("p", {}, sourceWorkText(model.sourceWork)) : null,
      model.fresh.required ? el("p", { class: "callout" }, model.fresh.why) : null,
    ]));
    if (!def) return;
    root.appendChild(el("section", { class: "card" }, [
      el("h2", {}, "What will be assessed"),
      el("div", { class: "prose" }, def.instructions_md),
      el("ul", {}, def.criteria.map((c) => el("li", {}, [el("strong", {}, c.label), ` — decided by ${c.decided_by}${c.required ? "" : " (optional)"}`, c.description ? el("span", { class: "hint" }, ` ${c.description}`) : null]))),
    ]));
    root.appendChild(el("section", { class: "card" }, [
      el("h2", {}, "The rules"),
      el("ul", {}, [
        el("li", {}, `Allowed: ${def.allowed_resources.join("; ") || "your lessons and your own work"}. Not allowed: AI assistants.`),
        el("li", {}, "The Mentor and Professor are paused for this Concept while the assessment is open. Nothing is deleted."),
        el("li", {}, `Time: about ${def.time_limit_hours} hours on a server-side clock. You can leave at any time.`),
        el("li", {}, `If it needs more work you can try again after ${def.retake_cooldown_hours} hours, with a different challenge.`),
      ]),
      el("h3", {}, "What is recorded"),
      el("ul", {}, def.evidence_collected.map((line) => el("li", {}, line))),
      el("p", { class: "hint" }, "No camera, screen, keyboard or clipboard monitoring. No copy detection. Fairness comes from fresh challenges and the record of your help levels."),
    ]));
    const actions = el("div", { class: "row" });
    if (model.resumeAttemptId) {
      actions.appendChild(el("a", { class: "button", href: `#/academy/assessments/attempts/${enc(model.resumeAttemptId)}` }, "Resume your attempt"));
    } else {
      const begin = el("button", { class: "button", type: "button" }, "Begin assessment");
      begin.disabled = !model.ready;
      begin.addEventListener("click", () => withBusy(begin, async () => {
        try {
          const attempt = await api.post(`${BASE}/attempts`, { definition_key: params.key, project_attempt_id: projectAttemptId || undefined }, { headers: { "Idempotency-Key": `start-${params.key}-${Date.now()}` } });
          window.location.hash = `#/academy/assessments/attempts/${enc(attempt.id)}`;
        } catch (err) { actions.appendChild(el("p", { class: "error-banner" }, err.message)); }
      }));
      actions.appendChild(begin);
    }
    root.appendChild(actions);
  } catch (err) { failure(root, err); }
}

// -- D. Workspace (draft) ------------------------------------------------------------------------------------------------------------

function challengeBlock(challenge, state, onChange) {
  const items = (challenge && challenge.items) || [];
  if (!items.length) return null;
  const fixed = items.filter((i) => i.fixed);
  const drawn = items.filter((i) => !i.fixed);
  const render = (item) => {
    const box = el("article", { class: "card challenge-item" }, [el("h3", {}, item.title || item.prompt || (item.prompt_md ? "Explain in your own words" : "Challenge"))]);
    if (item.statement_md) box.appendChild(el("p", { class: "prose" }, item.statement_md));
    if (Array.isArray(item.options)) {
      box.appendChild(el("p", {}, item.prompt));
      const chosen = new Set(state.items[item.entry_key] || []);
      item.options.forEach((option, index) => {
        const input = el("input", { type: item.multiple ? "checkbox" : "radio", name: `q-${item.entry_key}`, id: `q-${item.entry_key}-${index}` });
        input.checked = chosen.has(index);
        input.addEventListener("change", () => {
          const now = new Set(state.items[item.entry_key] || []);
          if (!item.multiple) now.clear();
          if (input.checked) now.add(index); else now.delete(index);
          state.items[item.entry_key] = [...now]; onChange();
        });
        box.appendChild(el("label", { class: "option", for: `q-${item.entry_key}-${index}` }, [input, ` ${option}`]));
      });
    } else if (item.prompt_md) {
      box.appendChild(el("p", { class: "prose" }, item.prompt_md));
      const area = el("textarea", { rows: "6", placeholder: "In your own words…", "aria-label": item.prompt_md });
      area.value = state.items[item.entry_key] || "";
      area.addEventListener("input", () => { state.items[item.entry_key] = area.value; onChange(); });
      box.appendChild(area);
    }
    return box;
  };
  return el("section", { class: "stack" }, [
    el("h2", {}, "Your challenge"),
    ...drawn.map(render),
    fixed.length ? el("h3", {}, "Explain-back set") : null,
    ...fixed.map(render),
  ]);
}

function fieldInput(def, state, onChange) {
  const isLong = def.input === "long_text";
  const control = isLong ? el("textarea", { rows: "8", "aria-label": def.label }) : el("input", { type: "text", "aria-label": def.label });
  control.value = state.fields[def.path] || "";
  control.addEventListener("input", () => { state.fields[def.path] = control.value; onChange(); });
  return el("div", { class: "field" }, [
    el("label", {}, def.label),
    def.sections ? el("p", { class: "hint" }, `Include these sections: ${def.sections.join(", ")}.`) : null,
    control,
  ]);
}

function hydrate(attempt) {
  const state = { items: {}, fields: {}, reflection: "" };
  const draft = attempt.draft || {};
  for (const item of (attempt.challenge && attempt.challenge.items) || []) {
    const saved = (draft.responses || {})[item.entry_key];
    if (saved) state.items[item.entry_key] = saved.selected !== undefined ? saved.selected : saved.text;
  }
  for (const def of attempt.definition.response_fields || []) {
    const path = def.path.split(".");
    let value = draft; for (const part of path) value = value && value[part];
    if (value !== undefined && value !== null) state.fields[def.path] = Array.isArray(value) ? value.join(", ") : value;
  }
  state.reflection = ((draft.fields || {}).reflection) || "";
  return state;
}

export async function renderAssessmentWorkspace(root, attempt) {
  const def = attempt.definition;
  const state = hydrate(attempt);
  const status = el("p", { class: "hint", role: "status" }, "Saved automatically as you type.");
  let timer = null;
  const collect = () => buildDraft(attempt.challenge, def.response_fields, { items: state.items, fields: state.fields, reflection: state.reflection });
  const save = async () => { try { await api.put(`${BASE}/attempts/${enc(attempt.id)}/draft`, { draft: collect() }); status.textContent = "Draft saved."; } catch (err) { status.textContent = err.message; } };
  const onChange = () => { status.textContent = "Saving…"; clearTimeout(timer); timer = setTimeout(save, 800); };

  clear(root);
  root.appendChild(header(def.title, `${kindLabel(def.kind)} · ${attemptStatusText(attempt)}`));
  const banner = assessmentModeBanner(attempt);
  root.appendChild(el("aside", { class: "assessment-banner", role: "note" }, [el("strong", {}, banner.title), ...banner.lines.map((l) => el("p", {}, l))]));
  root.appendChild(el("p", { class: "hint" }, timeLeftText(attempt)));
  root.appendChild(el("section", { class: "card" }, [
    el("h2", {}, "What is being assessed"),
    el("ul", {}, def.criteria.map((c) => el("li", {}, [el("strong", {}, c.label), ` — decided by ${c.decided_by}${c.required ? "" : " (optional)"}`]))),
    attempt.fresh_required ? el("p", { class: "callout" }, "This is a fresh challenge: it shows what you can do independently.") : null,
  ]));
  const challenge = challengeBlock(attempt.challenge, state, onChange);
  if (challenge) root.appendChild(challenge);
  if ((def.response_fields || []).length) {
    root.appendChild(el("section", { class: "card" }, [el("h2", {}, "Your work"), ...def.response_fields.map((f) => fieldInput(f, state, onChange))]));
  }
  const reflection = el("textarea", { rows: "3", placeholder: "Optional: what was hardest? This is kept for you and is never graded." });
  reflection.value = state.reflection;
  reflection.addEventListener("input", () => { state.reflection = reflection.value; onChange(); });
  root.appendChild(el("section", { class: "card" }, [el("h2", {}, "Reflection (optional)"), el("p", { class: "hint" }, "Kept for you and never graded."), reflection]));
  root.appendChild(status);

  const declaration = el("select", { "aria-label": "Declaration about the help you used" }, [el("option", { value: "" }, "Choose…"), ...DECLARATIONS.map(([v, l]) => el("option", { value: v }, l))]);
  const note = el("p", { class: "hint" }, "");
  declaration.addEventListener("change", () => { note.textContent = declarationNote(declaration.value); });
  const problem = el("p", { class: "error-banner", hidden: "hidden" });
  const submit = el("button", { class: "button", type: "button" }, "Submit for assessment");
  submit.addEventListener("click", () => withBusy(submit, async () => {
    const check = validateSubmit({ declaration: declaration.value });
    if (!check.ok) { problem.textContent = check.message; problem.removeAttribute("hidden"); return; }
    clearTimeout(timer);
    try {
      await api.put(`${BASE}/attempts/${enc(attempt.id)}/draft`, { draft: collect() });
      submit.textContent = "Submitting… grading can take a moment";
      await api.post(`${BASE}/attempts/${enc(attempt.id)}/submit`, { attestation: { declaration: declaration.value } });
      await renderAssessmentAttempt(root, { id: attempt.id });
    } catch (err) { problem.textContent = err.message; problem.removeAttribute("hidden"); submit.textContent = "Submit for assessment"; }
  }));
  const leave = el("button", { class: "button-secondary", type: "button" }, "Leave assessment");
  leave.addEventListener("click", () => withBusy(leave, async () => { await api.post(`${BASE}/attempts/${enc(attempt.id)}/abandon`); window.location.hash = "#/academy/assessments"; }));
  root.appendChild(el("section", { class: "card" }, [el("h2", {}, "Before you submit"), el("label", {}, "Help I used"), declaration, note, problem, el("div", { class: "row" }, [submit, leave])]));
}

// -- E/F/H. Result, needs work, review ----------------------------------------------------------------------------------------------

function factBlock(fact) {
  return el("section", { class: "card block-fact" }, [
    el("div", { class: "row between" }, [el("h2", {}, "Platform facts"), badge("PLATFORM FACT")]),
    el("p", { class: "hint" }, "Determined by code from your recorded work. The Grader cannot change these."),
    el("ul", {}, fact.deterministic_checks.map((c) => el("li", {}, [el("strong", {}, `${findingLabel(c.finding)} — `), c.label, c.detail ? el("span", { class: "hint" }, ` (${c.detail})`) : null]))),
    el("h3", {}, "Help and independence"),
    el("ul", {}, [
      el("li", {}, fact.assistance.source_levels.length ? `Your source work: ${fact.assistance.source_levels.map(helpLabel).join(", ")}.` : "No source-work help level was involved."),
      el("li", {}, fact.independence.challenge_issued ? "You completed a fresh challenge in Assessment Mode (recorded as independent, H0)." : "This used your existing project work."),
      fact.independence.fresh_required ? el("li", {}, "A fresh challenge was required for this assessment.") : null,
      fact.independence.declaration ? el("li", {}, `Your declaration (self-reported): ${fact.independence.declaration.replaceAll("_", " ")}.`) : null,
      fact.execution_verification ? el("li", {}, `Execution verification: ${fact.execution_verification.replaceAll("_", " ")}.`) : null,
    ]),
  ]);
}

function judgmentBlock(judgment) {
  const body = judgment.ran
    ? [
      el("p", { class: "hint" }, `An AI Grader judged only the criteria that need judgment. Contract ${judgment.grading_contract_version}${judgment.crosscheck_ran ? "; a second model cross-checked it" : ""}.`),
      ...judgment.criteria.map((c) => el("article", { class: "judged" }, c.status === "skipped"
        ? [el("strong", {}, c.label), el("p", { class: "hint" }, "Not judged because a required platform check was not met.")]
        : [
          el("div", { class: "row between" }, [el("strong", {}, c.label), badge(`${findingLabel(c.finding)} · ${confidenceLabel(c.confidence)}`)]),
          c.agreement === "disagree" ? el("p", { class: "callout" }, "The two judgments disagreed, so a person will review this.") : null,
          el("p", {}, c.rationale),
          c.quotes && c.quotes.length ? el("blockquote", {}, c.quotes.join(" … ")) : null,
          c.gap ? el("p", { class: "hint" }, `Gap: ${c.gap}`) : null,
        ])),
      judgment.runs.length ? el("p", { class: "hint" }, judgment.runs.map((r) => `${r.slot}: model ${r.model_id || "unknown"} · ${costLabel(r.cost_status)}`).join(" · ")) : null,
    ]
    : [el("p", { class: "hint" }, "No AI judgment was needed for this assessment.")];
  return el("section", { class: "card block-judgment" }, [el("div", { class: "row between" }, [el("h2", {}, "Grader judgment"), badge("GRADER JUDGMENT")]), ...body]);
}

function reflectionBlock(reflection) {
  return el("section", { class: "card block-reflection" }, [
    el("div", { class: "row between" }, [el("h2", {}, "Your reflection"), badge("LEARNER REFLECTION")]),
    reflection.reflection ? el("p", {}, reflection.reflection) : el("p", { class: "hint" }, "You did not add a reflection. Reflections are kept for you and never graded."),
    ...(reflection.earlier_explain_back || []).map((e) => el("article", {}, [el("strong", {}, e.label), el("p", { class: "hint" }, `Written ${helpLabel(e.assistance_level)}`), el("p", {}, e.response)])),
  ]);
}

function coachingBlock(model) {
  return el("section", { class: "card block-coaching" }, [
    el("div", { class: "row between" }, [el("h2", {}, "Professor coaching"), badge("PROFESSOR COACHING")]),
    el("p", { class: "hint" }, model.coaching.note),
    el("a", { class: "button-link", href: model.coachingHref }, "Ask the Professor about this result"),
  ]);
}

function needsWorkBlock(model, ctx) {
  if (!model.gaps.length && !model.remediation.length) return null;
  return el("section", { class: "card block-needswork" }, [
    el("h2", {}, "What to work on"),
    model.gaps.length ? el("ul", {}, model.gaps.map((g) => el("li", {}, [el("strong", {}, g.label), ` — ${g.detail || findingLabel(g.finding)} `, badge(g.source === "platform" ? "PLATFORM FACT" : g.source === "grader" ? "GRADER JUDGMENT" : "REVIEWER")]))) : null,
    el("h3", {}, "Your next steps"),
    el("ul", {}, model.remediation.map((s) => el("li", {}, el("a", { href: remediationLink(s, ctx) }, s.label || s.kind)))),
    el("p", { class: "hint" }, "Every attempt is kept. Trying again gives you a different challenge."),
  ]);
}

function reviewBlock(model, attemptId, reload) {
  const card = el("section", { class: "card block-review" }, [el("h2", {}, "Ask for a review")]);
  if (model.open) {
    card.appendChild(el("p", {}, model.summary));
    if (model.needsConsent) {
      const consent = el("button", { class: "button", type: "button" }, "I agree to share this attempt with a reviewer");
      consent.addEventListener("click", () => withBusy(consent, async () => { await api.post(`${BASE}/reviews/${enc(model.open.id)}/consent`); await reload(); }));
      card.appendChild(consent);
    }
    const withdraw = el("button", { class: "button-secondary", type: "button" }, "Withdraw request");
    withdraw.addEventListener("click", () => withBusy(withdraw, async () => { try { await api.post(`${BASE}/reviews/${enc(model.open.id)}/withdraw`); await reload(); } catch (err) { card.appendChild(el("p", { class: "error-banner" }, err.message)); } }));
    card.appendChild(withdraw);
    return card;
  }
  if (model.latest && model.latest.decision) {
    card.appendChild(el("p", {}, `Last review: ${decisionLabel(model.latest.decision)}. ${model.latest.decision_rationale || ""}`));
  }
  card.appendChild(el("p", { class: "hint" }, "A reviewer would see:"));
  card.appendChild(el("ul", {}, REVIEW_SEES.map((line) => el("li", {}, line))));
  const reason = el("textarea", { rows: "3", placeholder: "Why do you think this result should be reviewed?" });
  const consent = el("input", { type: "checkbox", id: "review-consent" });
  const problem = el("p", { class: "error-banner", hidden: "hidden" });
  const send = el("button", { class: "button", type: "button" }, "Request review");
  send.addEventListener("click", () => withBusy(send, async () => {
    const check = reviewPayload({ reason: reason.value, consent: consent.checked });
    if (!check.ok) { problem.textContent = check.message; problem.removeAttribute("hidden"); return; }
    try { await api.post(`${BASE}/attempts/${enc(attemptId)}/review`, check.body); await reload(); } catch (err) { problem.textContent = err.message; problem.removeAttribute("hidden"); }
  }));
  card.appendChild(reason);
  card.appendChild(el("label", { for: "review-consent" }, [consent, " I agree to share this attempt with a reviewer."]));
  card.appendChild(problem);
  card.appendChild(send);
  return card;
}

export async function renderAssessmentResult(root, attemptId, view) {
  const model = resultModel(view);
  const reload = () => renderAssessmentAttempt(root, { id: attemptId });
  clear(root);
  const title = view.attempt.definition.title;
  root.appendChild(header(title, `${kindLabel(view.attempt.definition.kind)} · ${attemptStatusText(view.attempt)}`));
  if (!model.finalized) {
    const box = el("section", { class: "card" }, [el("h2", {}, "Your attempt is saved"), el("p", {}, model.pending ? model.pending.message : "Your attempt is being checked.")]);
    if (model.pending) box.appendChild(el("ul", {}, model.pending.deterministic_checks.map((c) => el("li", {}, `${findingLabel(c.finding)} — ${c.label}`))));
    if (model.canRetryGrading) {
      const retry = el("button", { class: "button", type: "button" }, "Try grading again");
      retry.addEventListener("click", () => withBusy(retry, async () => { retry.textContent = "Grading… this can take a moment"; try { await api.post(`${BASE}/attempts/${enc(attemptId)}/grade`, {}); } finally { await reload(); } }));
      box.appendChild(retry);
    }
    root.appendChild(box);
    return;
  }
  root.appendChild(el("section", { class: `card outcome outcome-${model.info.tone}` }, [
    el("div", { class: "row between" }, [el("h2", {}, model.info.label), badge(model.effect, model.info.tone)]),
    el("p", {}, model.headline),
  ]));
  root.appendChild(el("section", { class: "card" }, [
    el("h2", {}, "Did my Learner State change?"),
    ...model.stateChanges.map((s) => el("p", {}, `${s.concept_name}: ${String(s.before).replaceAll("_", " ")} → ${String(s.after).replaceAll("_", " ")}${s.changed ? " (changed)" : " (unchanged)"}${s.overlays.length ? ` · ${s.overlays.join(", ")}` : ""}`)),
    el("p", { class: "hint" }, "Learner State is recomputed from your evidence. An assessment never sets it directly."),
  ]));
  root.appendChild(factBlock(model.fact));
  root.appendChild(judgmentBlock(model.judgment));
  root.appendChild(reflectionBlock(model.reflection));
  const ctx = { projectAttemptId: view.attempt.project_attempt_id, definitionKey: view.attempt.definition.definition_key };
  const work = needsWorkBlock(model, ctx);
  if (work) root.appendChild(work);
  root.appendChild(coachingBlock(model));
  const review = model.answers.review_later || {};
  const next = Object.values(review).filter(Boolean).map((r) => (r.next_review_at ? `Next review around ${String(r.next_review_at).slice(0, 10)}.` : "")).filter(Boolean);
  if (next.length) root.appendChild(el("p", { class: "hint" }, next.join(" ")));
  if (model.hasRecord) root.appendChild(el("a", { class: "button", href: `#/academy/assessments/records/${enc(model.recordResultId)}` }, "Open my Demonstration Record"));
  root.appendChild(reviewBlock(model.review, attemptId, reload));
  root.appendChild(el("details", { class: "card" }, [el("summary", {}, `History of this attempt (${model.history.length} records)`), el("ul", {}, model.history.map((h) => el("li", {}, `${h.kind}${h.outcome ? ` — ${outcomeInfo(h.outcome).label}` : ""}`)))]));
}

export async function renderAssessmentAttempt(root, params) {
  loading(root, "Loading your attempt…");
  try {
    const view = await api.get(`${BASE}/attempts/${enc(params.id)}/result`);
    if (view.status === "draft") { await renderAssessmentWorkspace(root, view.attempt); return; }
    await renderAssessmentResult(root, params.id, view);
  } catch (err) { failure(root, err); }
}

// -- G. Demonstration Record ------------------------------------------------------------------------------------------------------------------

async function download(name, mime, content) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const link = el("a", { href: url, download: name });
  document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
}

export async function renderDemonstrationRecord(root, params) {
  loading(root, "Loading your record…");
  try {
    const payload = await api.get(`${BASE}/records/${enc(params.id)}`);
    const m = recordModel(payload);
    clear(root);
    root.appendChild(header(m.title, m.notice));
    root.appendChild(el("section", { class: "card" }, [
      el("div", { class: "row between" }, [el("h2", {}, m.assessment), badge(m.status)]),
      m.statusReasons.length ? el("p", { class: "callout" }, m.statusReasons.join(" ")) : null,
      el("ul", {}, m.concepts.map((c) => el("li", {}, c))),
      m.project ? el("p", {}, `Project: ${m.project}`) : null,
    ]));
    root.appendChild(el("section", { class: "card" }, [el("h2", {}, "Evidence"), el("ul", {}, m.evidence.map((e) => el("li", {}, e)))]));
    root.appendChild(el("section", { class: "card" }, [
      el("h2", {}, "What was verified"),
      el("h3", {}, "Verified by the platform"), el("ul", {}, m.verified.map((v) => el("li", {}, v))),
      el("h3", {}, "Self-reported"), el("ul", {}, m.selfReported.map((v) => el("li", {}, v))),
      el("p", { class: "hint" }, m.grader),
    ]));
    const md = el("button", { class: "button", type: "button" }, "Export Markdown");
    md.addEventListener("click", () => withBusy(md, async () => download(recordFileName(payload, "md"), "text/markdown", await api.getText(`${BASE}/records/${enc(params.id)}?format=md`))));
    const json = el("button", { class: "button-secondary", type: "button" }, "Export JSON");
    json.addEventListener("click", () => download(recordFileName(payload, "json"), "application/json", JSON.stringify(payload, null, 2)));
    root.appendChild(el("div", { class: "row" }, [md, json]));
    root.appendChild(el("p", { class: "hint" }, `Record fingerprint ${String(m.hash || "").slice(0, 12)}… It stays private to you.`));
  } catch (err) { failure(root, err); }
}

// -- I. Reviewer (org owner / admin) -------------------------------------------------------------------------------------------------------------------

export async function renderReviewQueue(root) {
  loading(root, "Loading reviews…");
  try {
    const queue = await api.get(`${BASE}/reviews`);
    clear(root);
    root.appendChild(header("Assessment reviews", "Only what a learner has agreed to share is shown."));
    root.appendChild(queue.length ? el("div", { class: "stack" }, queue.map((r) => el("article", { class: "card" }, [
      el("div", { class: "row between" }, [el("strong", {}, r.trigger.replaceAll("_", " ")), badge(r.consented ? "Shared" : "Waiting for consent")]),
      el("a", { class: "button-link", href: `#/academy/assessments/reviews/${enc(r.id)}` }, "Open"),
    ]))) : el("div", { class: "empty-state" }, "No open reviews."));
  } catch (err) { failure(root, err); }
}

export async function renderReviewDetail(root, params) {
  loading(root, "Loading review…");
  try {
    const d = await api.get(`${BASE}/reviews/${enc(params.id)}`);
    clear(root);
    root.appendChild(header("Review an assessment", d.consented ? "You can see only this attempt." : "The learner has not shared this attempt yet."));
    if (!d.consented) { root.appendChild(el("div", { class: "empty-state" }, d.message)); return; }
    root.appendChild(el("section", { class: "card" }, [
      el("h2", {}, `${d.definition.title} (v${d.definition.version})`),
      d.reason_text ? el("p", {}, `Learner's reason: ${d.reason_text}`) : null,
      el("p", {}, `Result: ${outcomeInfo(d.result.outcome).label} · ${effectLabel(d.result.effect)}`),
      el("h3", {}, "Checks and judgments"),
      el("ul", {}, d.result.criteria.map((c) => el("li", {}, `${c.label}: ${findingLabel(c.finding)}${c.confidence ? ` (${confidenceLabel(c.confidence)})` : ""}${c.human_override ? " — overridden" : ""}`))),
      el("h3", {}, "The learner's responses"), el("pre", {}, JSON.stringify(d.submission, null, 2)),
      el("p", { class: "hint" }, `Declaration: ${(d.attestation || {}).declaration || "none"} · fingerprint ${d.fingerprint.slice(0, 12)}…`),
    ]));
    const decision = el("select", { "aria-label": "Decision" }, d.allowed_decisions.map((v) => el("option", { value: v }, decisionLabel(v))));
    const rationale = el("textarea", { rows: "3", placeholder: "Why? (required, at least 10 characters)" });
    const problem = el("p", { class: "error-banner", hidden: "hidden" });
    const send = el("button", { class: "button", type: "button" }, "Record decision");
    send.addEventListener("click", () => withBusy(send, async () => {
      try { await api.post(`${BASE}/reviews/${enc(params.id)}/decision`, { decision: decision.value, rationale: rationale.value }); window.location.hash = "#/academy/assessments/reviews"; }
      catch (err) { problem.textContent = err.message; problem.removeAttribute("hidden"); }
    }));
    root.appendChild(el("section", { class: "card" }, [el("h2", {}, "Your decision"), el("p", { class: "hint" }, "A decision adds a new record; the original result is never edited. A required platform check cannot be overridden."), decision, rationale, problem, send]));
  } catch (err) { failure(root, err); }
}
