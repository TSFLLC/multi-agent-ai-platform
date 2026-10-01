// AIL5D.6 — step renderers. One per structured step type; none is "a Markdown dump". Everything the server owns
// (completion, reveal, grading, evidence) is asked of the server; this file only draws and submits.

import { api } from "../api.js";
import { el } from "../dom.js";
import { renderMarkdown } from "../markdown.js";
import { assessmentDefinitionHref } from "../academyLinks.js";
import { checkAnswered, checkComplete, checkParts, checkPayload } from "./model.js";
import { renderPractice } from "./practice.js";

export const md = (source, cls = "") => el("div", { class: `d5-prose ${cls}`.trim(), html: renderMarkdown(String(source || "")) });
const fill = (host, ...kids) => host.replaceChildren(...kids.filter(Boolean));
const txt = (v) => String(v || "").trim();
const words = (t) => (txt(t).match(/\S+/g) || []).length;
const errText = (err) => (err && err.message) || "Something went wrong. Try again.";

function callout(tone, text, title) {
  return el("aside", { class: `d5-callout ${tone}` }, [title ? el("strong", {}, title) : null, md(text)]);
}

export function segmented(name, options, value, onPick, disabled) {
  return el("div", { class: "d5-seg", role: "radiogroup", "aria-label": name }, options.map(([val, label]) => el("button", {
    type: "button", role: "radio", "aria-checked": String(value === val), class: `d5-seg-btn${value === val ? " on" : ""}`, disabled: disabled || null, onclick: () => onPick(val),
  }, label)));
}

function tabs(items) {
  let cur = 0;
  const host = el("div", { class: "d5-tabs" });
  const draw = () => fill(host,
    el("div", { class: "d5-tablist", role: "tablist" }, items.map((it, i) => el("button", { type: "button", role: "tab", "aria-selected": String(i === cur), class: `d5-tab${i === cur ? " on" : ""}`, onclick: () => { cur = i; draw(); } },
      [el("span", {}, it.label), it.sub ? el("small", {}, it.sub) : null]))),
    el("div", { class: "d5-tabpanel", role: "tabpanel" }, [md(items[cur].text)]));
  draw();
  return host;
}

function accordion(items) {
  const opened = new Set();
  const host = el("div", { class: "d5-accordion" });
  const draw = () => fill(host, ...items.map((it, i) => el("div", { class: `d5-acc-item${opened.has(i) ? " open" : ""}` }, [
    el("button", { type: "button", class: "d5-acc-head", "aria-expanded": String(opened.has(i)), onclick: () => { if (opened.has(i)) opened.delete(i); else opened.add(i); draw(); } },
      [el("span", { class: "d5-acc-n" }, String(i + 1)), el("span", {}, it.title), el("span", { class: "d5-acc-chev" }, opened.has(i) ? "–" : "+")]),
    opened.has(i) ? el("div", { class: "d5-acc-body" }, [md(it.text)]) : null,
  ])));
  draw();
  return host;
}

export function renderBlock(b) {
  switch (b.kind) {
    case "md": return md(b.text);
    case "heading": return el("h3", { class: "d5-h3" }, b.text);
    case "callout": return callout(b.tone, b.text, b.title);
    case "pair": return el("div", { class: "d5-pair" }, [b.left, b.right].map((side, i) => el("div", { class: `d5-pair-side ${i ? "ai" : "trad"}` }, [el("h4", {}, side.title), md(side.text)])));
    case "compare": return el("div", { class: "d5-compare", role: "table" }, [
      el("div", { class: "d5-compare-row head", role: "row" }, b.head.map((h) => el("div", { role: "columnheader" }, h))),
      ...b.rows.map((r) => el("div", { class: "d5-compare-row", role: "row" }, r.map((c) => el("div", { role: "cell" }, c)))),
    ]);
    case "tabs": return tabs(b.items);
    case "accordion": return accordion(b.items);
    case "cards": return el("div", { class: "d5-grid6" }, b.items.map((c) => el("div", { class: "d5-mini" }, [el("h4", {}, c.title), md(c.text), c.caution ? el("p", { class: "caveat" }, [el("b", {}, "Careful: "), c.caution]) : null])));
    default: return null;
  }
}

const teach = (step) => el("div", { class: "d5-stack" }, step.content.blocks.map(renderBlock));

function example(step) {
  const c = step.content;
  return el("div", { class: "d5-stack" }, [
    c.lead_md ? md(c.lead_md, "d5-lead") : null,
    el("div", { class: `d5-cases n${c.cases.length}` }, c.cases.map((k, i) => el("article", { class: `d5-case ${i ? "ai" : "trad"}` }, [
      el("header", {}, [k.tag ? el("span", { class: "d5-tag" }, k.tag) : null, el("h3", {}, k.title)]), md(k.body_md), k.verdict ? el("footer", {}, [el("span", { class: "d5-verdict" }, k.verdict)]) : null]))),
    c.takeaway_md ? callout("key", c.takeaway_md, "The important conclusion") : null,
  ]);
}

// An action that talks to the server and reloads the workspace; shows the server's own message on failure.
function busyButton(label, cls, action, { disabled = false } = {}) {
  const btn = el("button", { type: "button", class: `d5-btn ${cls}`, disabled: disabled || null }, label);
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const before = btn.textContent;
    btn.textContent = "Saving…";
    try { await action(); } catch (err) { btn.textContent = before; btn.disabled = false; btn.insertAdjacentElement("afterend", el("p", { class: "d5-err", role: "alert" }, errText(err))); }
  });
  return btn;
}

function reflect(step, ctx) {
  const saved = step.response && step.response.text;
  const counter = el("span", {}, `${words(saved)} words`);
  const ta = el("textarea", { class: "d5-textarea", rows: "5", placeholder: "Write your answer…", "aria-label": "Your answer", oninput: (e) => { counter.textContent = `${words(e.target.value)} words`; } });
  ta.value = saved || "";
  const min = step.content.min_chars || 8;
  return el("div", { class: "d5-stack" }, [
    md(step.content.prompt_md, "d5-lead"),
    el("div", { class: "d5-answerbox" }, [ta, el("div", { class: "d5-answer-foot" }, [el("span", { class: "d5-lock" }, "🔒 Private — only you see this"), counter])]),
    el("div", { class: "d5-lockbar" }, [busyButton(saved ? "Update my answer" : "Save my answer", "primary", async () => {
      if (txt(ta.value).length < min) throw new Error(`Write at least ${min} characters.`);
      await api.put(`/academy/level-1/items/${ctx.itemId}/steps/${step.key}/response`, { text: txt(ta.value) });
      await ctx.reload();
    }), el("span", { class: "d5-hint" }, "Saved privately. It is never graded.")]),
  ]);
}

function reflection(step, ctx) {
  const saved = step.response && step.response.text;
  const earlier = step.compare_to && step.compare_to.response && step.compare_to.response.text;
  const ta = el("textarea", { class: "d5-textarea", rows: "4", placeholder: "What would you say differently now?", "aria-label": "What would you change" });
  ta.value = saved || "";
  return el("div", { class: "d5-stack" }, [
    md(step.content.prompt_md, "d5-lead"),
    el("div", { class: "d5-then-now" }, [
      step.compare_to ? el("div", { class: "d5-then" }, [el("small", {}, "THEN — YOUR FIRST ANSWER"), el("p", {}, earlier ? `“${earlier}”` : "You skipped the first step, so there is nothing to compare yet.")]) : null,
      step.compare_to ? el("div", { class: "d5-arrow", "aria-hidden": "true" }, "→") : null,
      el("div", { class: "d5-now" }, [el("small", {}, "NOW — WHAT WOULD YOU CHANGE?"), ta]),
    ]),
    el("div", { class: "d5-lockbar" }, [busyButton(saved ? "Update my reflection" : "Save reflection", "primary", async () => {
      if (txt(ta.value).length < 8) throw new Error("Write at least 8 characters.");
      await api.put(`/academy/level-1/items/${ctx.itemId}/steps/${step.key}/response`, { text: txt(ta.value) });
      await ctx.reload();
    }), el("span", { class: "d5-hint" }, "Private, never graded")]),
  ]);
}

const CHOICES = [["true", "True"], ["false", "False"], ["depends", "Depends / needs context"]];

function think(step, ctx) {
  const host = el("div", { class: "d5-stack" });
  const content = step.content;
  const committed = Boolean(step.committed);
  let reveal = null;
  let error = "";
  const picks = {};
  const why = {};
  let text = step.response && step.response.text ? step.response.text : "";
  if (step.response && step.response.statements) {
    for (const [id, v] of Object.entries(step.response.statements)) { picks[id] = v.choice; why[id] = v.reasoning || ""; }
  }

  const loadReveal = async () => {
    try { reveal = await api.post(`/academy/level-1/items/${ctx.itemId}/steps/${step.key}/reveal`); } catch (err) { error = errText(err); }
    draw();
  };
  const commit = async (payload) => {
    error = "";
    try {
      await api.put(`/academy/level-1/items/${ctx.itemId}/steps/${step.key}/response`, payload);
      await ctx.reload({ keepStep: true });
    } catch (err) { error = errText(err); draw(); }
  };
  const claimReveal = (s) => {
    const k = reveal && reveal.claims && reveal.claims[s.id];
    if (!k) return el("div", { class: "d5-reveal loading" }, [el("small", {}, "FETCHING THE EXPLANATION…")]);
    return el("div", { class: `d5-reveal ${k.matched ? "match" : "differ"}` }, [
      el("small", {}, k.matched ? "YOUR CALL MATCHES" : `THE PROFESSOR'S CALL: ${k.answer === "depends" ? "DEPENDS / NEEDS CONTEXT" : k.answer.toUpperCase()}`), md(k.why_md)]);
  };
  const lockBar = (ok, hint, go) => el("div", { class: "d5-lockbar" }, [
    el("button", { type: "button", class: "d5-btn primary", disabled: !ok || null, onclick: go }, "Lock in my answer"),
    el("span", { class: "d5-hint" }, ok ? "Once locked, you'll see the explanation. You can't change it afterwards." : hint),
  ]);
  const draw = () => {
    const parts = [el("div", { class: "d5-scenario" }, [el("small", {}, "SCENARIO"), md(content.scenario_md)])];
    if (content.statements) {
      parts.push(el("div", { class: "d5-claims" }, content.statements.map((s, i) => el("div", { class: "d5-claim" }, [
        el("div", { class: "d5-claim-text" }, [el("span", { class: "d5-claim-n" }, String(i + 1)), s.text]),
        segmented(`Claim ${i + 1}`, CHOICES, picks[s.id], (v) => { picks[s.id] = v; draw(); }, committed),
        el("input", { class: "d5-input", type: "text", placeholder: "Why? One sentence…", value: why[s.id] || "", disabled: committed || null, "aria-label": `Reasoning for claim ${i + 1}`, oninput: (e) => { why[s.id] = e.target.value; } }),
        committed ? claimReveal(s) : null,
      ]))));
      if (!committed) {
        parts.push(lockBar(content.statements.every((s) => picks[s.id]), "Mark every claim to lock in", () => commit({
          statements: Object.fromEntries(content.statements.map((s) => [s.id, { choice: picks[s.id], ...(txt(why[s.id]) ? { reasoning: txt(why[s.id]) } : {}) }])) })));
      }
    } else {
      const ta = el("textarea", { class: "d5-textarea", rows: "4", placeholder: "Write your answer and your reasoning…", "aria-label": "Your answer", disabled: committed || null, oninput: (e) => { text = e.target.value; const b = host.querySelector(".d5-lockbar button"); if (b) b.disabled = txt(text).length < 8; } });
      ta.value = text;
      parts.push(el("div", { class: "d5-answerbox" }, [el("label", {}, content.question_md.replace(/[*_`#>]/g, "")), ta]));
      if (!committed) parts.push(lockBar(txt(text).length >= 8, "Write a short answer to lock in", () => commit({ text: txt(text) })));
      else if (!reveal) parts.push(el("div", { class: "d5-reveal loading" }, [el("small", {}, "ANSWER LOCKED — FETCHING THE EXPLANATION…")]));
      else parts.push(el("div", { class: "d5-reveal" }, [el("small", {}, "ANSWER LOCKED — NOW SEE THE EXPLANATION"), md(reveal.reveal_md)]));
    }
    if (error) parts.push(el("p", { class: "d5-err", role: "alert" }, error));
    fill(host, ...parts);
  };
  draw();
  if (committed) loadReveal();
  return host;
}

function check(step, ctx) {
  const picks = {};
  const questions = step.questions || [];
  const result = step.check_result;
  const passed = Boolean(result && result.ever_passed);
  const lastById = result ? Object.fromEntries((result.items || []).map((i) => [i.id, i.passed])) : {};
  const reviewById = step.review ? Object.fromEntries(step.review.map((r) => [r.id, r.explanation])) : {};
  const host = el("div", { class: "d5-stack" });
  let error = "";
  let feedback = null;
  let submitting = false;
  const submit = async () => {
    submitting = true; error = ""; draw();
    try {
      const out = await api.post(`/academy/level-1/items/${ctx.itemId}/knowledge-check`, { answers: checkPayload(questions, picks) });
      feedback = out.feedback;
      await ctx.reload({ keepStep: true, feedback: { passed: out.passed, feedback: out.feedback, results: out.results } });
    } catch (err) { error = errText(err); submitting = false; draw(); }
  };
  const retrying = ctx.checkFeedback && !passed;
  const draw = () => {
    const answered = questions.filter((q) => checkAnswered(q, picks)).length;
    const lastFeedback = ctx.checkFeedback || null;
    fill(host,
      md(step.content.intro_md, "d5-lead"),
      passed ? el("div", { class: "d5-score pass" }, [el("strong", {}, "Knowledge check passed"), el("span", {}, "Every item is right. The reasoning for each is shown below.")])
        : lastFeedback && lastFeedback.feedback ? el("div", { class: "d5-score" }, [el("strong", {}, `${lastFeedback.feedback.total - lastFeedback.feedback.incorrect} of ${lastFeedback.feedback.total} correct`), el("span", {}, lastFeedback.feedback.message)])
          : el("div", { class: "d5-progressline" }, [el("span", { style: `width:${questions.length ? (answered / questions.length) * 100 : 0}%` }), el("em", {}, `${answered} of ${questions.length} answered`)]),
      el("ol", { class: "d5-qlist" }, questions.map((q, i) => {
        const wrong = lastFeedback && lastFeedback.feedback && lastFeedback.feedback.incorrect_item_ids.includes(q.id);
        return el("li", { class: `d5-q${passed ? " right" : wrong ? " wrong" : ""}` }, [
          el("p", { class: "d5-q-text" }, [el("b", {}, `${i + 1}. `), q.prompt]),
          el("div", { class: "d5-q-row" }, checkParts(q).flatMap((part) => [
            part.name ? el("span", { class: "d5-q-lab" }, part.name === "system" ? "It is" : part.name === "claim" ? "The claim is" : part.name.replace(/_/g, " ")) : null,
            part.numeric
              ? el("input", { class: "d5-input", type: "text", inputmode: "decimal", "aria-label": part.name, placeholder: part.name, disabled: passed || submitting || null, value: (picks[q.id] || {})[part.name] || "",
                  oninput: (e) => { picks[q.id] = { ...(picks[q.id] || {}), [part.name]: e.target.value }; const b = host.querySelector(".d5-lockbar button"); if (b) b.disabled = !checkComplete(questions, picks) || submitting; } })
              : segmented(part.name || "Answer", part.options, part.name ? (picks[q.id] || {})[part.name] : picks[q.id], (v) => {
                if (part.name) picks[q.id] = { ...(picks[q.id] || {}), [part.name]: v }; else picks[q.id] = v;
                draw();
              }, passed || submitting),
          ])),
          passed && reviewById[q.id] ? el("div", { class: "d5-reveal small" }, [el("small", {}, "WHY"), el("p", {}, reviewById[q.id])]) : null,
          !passed && wrong ? el("p", { class: "d5-hint" }, "Look at this one again.") : null,
          passed || !(q.id in lastById) ? null : null,
        ]);
      })),
      passed ? null : el("div", { class: "d5-lockbar" }, [
        el("button", { type: "button", class: "d5-btn primary", disabled: !checkComplete(questions, picks) || submitting || null, onclick: submit }, submitting ? "Checking…" : retrying || (result && result.attempts) ? "Check again" : "Check my answers"),
        el("span", { class: "d5-hint" }, "Checked on the server. A miss is not penalised — read the feedback and try again."),
      ]),
      error ? el("p", { class: "d5-err", role: "alert" }, error) : null,
    );
  };
  draw();
  void feedback;
  return host;
}

function explainBack(step, ctx) {
  const saved = (step.response && step.response.points) || {};
  const areas = {};
  const defKey = step.binding && step.binding.definition_key;
  const host = el("div", { class: "d5-stack" }, [
    el("div", { class: "d5-scenario" }, [el("small", {}, "THE QUESTION"), md(step.content.prompt_md)]),
    el("p", { class: "d5-lead" }, "Plan your explanation in short pieces. This is a private outline; the graded written answer is submitted in the Assessment Center."),
    el("div", { class: "d5-points" }, step.content.points.map((p, i) => {
      const ta = el("textarea", { class: "d5-textarea slim", rows: "2", "aria-label": p.label });
      ta.value = saved[p.key] || "";
      areas[p.key] = ta;
      return el("div", { class: "d5-point" }, [el("span", { class: "d5-point-n" }, String(i + 1)), el("div", {}, [el("label", {}, p.label), p.hint ? el("small", {}, p.hint) : null, ta])]);
    })),
    el("div", { class: "d5-lockbar" }, [busyButton(step.response ? "Update my outline" : "Save my outline", "primary", async () => {
      const points = Object.fromEntries(Object.entries(areas).map(([k, t]) => [k, txt(t.value)]));
      if (Object.values(points).some((v) => v.length < 5)) throw new Error("Fill in every point (a few words each).");
      await api.put(`/academy/level-1/items/${ctx.itemId}/steps/${step.key}/response`, { points });
      await ctx.reload();
    }), el("span", { class: "d5-hint" }, "Private outline. It does not start the assessment.")]),
    defKey ? el("p", { class: "d5-note" }, ["When you are ready to demonstrate this, take the ", el("a", { href: assessmentDefinitionHref(defKey) }, "assessment in the Assessment Center"), " — the AI Professor pauses there."]) : null,
  ]);
  return host;
}

const RENDERERS = { teach, example, reflect, reflection, think, check, explain_back: explainBack, lab: (s, c) => renderPractice(s, c), practice: (s, c) => renderPractice(s, c) };

export function renderStepBody(step, ctx) {
  const fn = RENDERERS[step.type];
  return fn ? fn(step, ctx) : el("p", { class: "d5-err" }, `This step type (${step.type}) is not supported by this version.`);
}
