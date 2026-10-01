// AIL5D.6 — the contextual AI Professor panel for the current step (AIL5D.4 server contract).
//
// The panel never chooses a mode or a help level and never sees an answer: the server decides both from the step and the
// learner, assembles the context itself, records delivered help, and pauses the Professor during an AIL.5C assessment.
// This file only asks and displays. Replies are model text and are inserted as text.

import { api } from "../api.js";
import { el } from "../dom.js";

const fill = (host, ...kids) => host.replaceChildren(...kids.filter(Boolean));
const errText = (err) => (err && err.message) || "The Professor could not answer. Try again.";
const LEVEL_LABEL = { clarify: "a clarifying answer", hint: "a hint", stronger_hint: "a stronger hint", explain: "an explanation" };
const MODE_LABEL = { teaching: "Teaching: ask me anything about this", guided: "Guided: I'll coach you without giving the answer away", independent: "Independent: you lead; I only give hints" };

// Conversation per step, kept in memory for the life of the page so switching steps does not lose it.
const logs = new Map();

export function professorPanel(step, ctx, { onHelp } = {}) {
  const host = el("aside", { class: "d5-prof", "aria-label": "AI Professor" });
  const key = `${ctx.itemId}:${step.key}`;
  const log = logs.get(key) || [];
  logs.set(key, log);
  let status = null;
  let busy = false;
  let error = "";
  let draft = "";

  const load = async () => {
    try { status = await api.get(`/academy/level-1/items/${ctx.itemId}/steps/${step.key}/professor`); } catch (err) { error = errText(err); }
    draw();
  };
  const ask = async (help, question) => {
    if (busy) return;
    busy = true; error = ""; draw();
    try {
      const reply = await api.post(`/academy/level-1/items/${ctx.itemId}/steps/${step.key}/professor`, { help, ...(question ? { question } : {}) });
      if (reply.status === "complete") {
        log.push({ q: question || (help === "hint" ? "Give me a hint" : "Help me with this step"), a: reply.direct_answer, more: reply.explanation, assistance: reply.assistance });
      } else {
        error = reply.error_message || "The Professor could not answer this time.";
      }
      status = await api.get(`/academy/level-1/items/${ctx.itemId}/steps/${step.key}/professor`);
      if (onHelp) onHelp();
    } catch (err) { error = errText(err); }
    busy = false; draft = ""; draw();
    const body = host.querySelector(".d5-prof-body");
    if (body) body.scrollTop = body.scrollHeight;
  };

  function draw() {
    const paused = status && !status.available;
    const input = el("input", { class: "d5-input", type: "text", maxlength: "2000", placeholder: paused ? "Paused during your assessment" : "Ask about this step…", "aria-label": "Ask the Professor", disabled: paused || busy || null, value: draft,
      oninput: (e) => { draft = e.target.value; }, onkeydown: (e) => { if (e.key === "Enter" && draft.trim()) ask("ask", draft.trim()); } });
    const nextLevel = status ? LEVEL_LABEL[status.next_hint_level] || "a hint" : "a hint";
    const canHint = status && status.mode !== "teaching" && !paused;
    fill(host,
      el("header", { class: "d5-prof-head" }, [el("span", { class: "av" }, "P"), el("div", {}, [el("b", {}, "AI Professor"), el("small", {}, status ? MODE_LABEL[status.mode] || "" : "Getting ready…")])]),
      paused ? el("div", { class: "d5-prof-paused", role: "status" }, [el("b", {}, "Paused"), el("p", {}, status.paused_reason || "The Professor is paused while you are in an assessment.")]) : null,
      el("div", { class: "d5-prof-body" }, [
        log.length ? null : el("p", { class: "d5-hint" }, "I can see this step, where you are in the Day, and your own work here. I can't see answers you haven't unlocked or any assessment material."),
        ...log.map((m) => el("div", { class: "d5-prof-turn" }, [
          el("p", { class: "d5-prof-q" }, m.q),
          el("div", { class: "d5-prof-a" }, [el("p", {}, m.a), m.more ? el("p", { class: "d5-prof-more" }, m.more) : null, m.assistance ? el("small", {}, `This was ${LEVEL_LABEL[m.assistance.level] || "help"}.`) : null]),
        ])),
        busy ? el("p", { class: "d5-hint", role: "status" }, "Thinking…") : null,
        error ? el("p", { class: "d5-err", role: "alert" }, error) : null,
      ]),
      el("footer", { class: "d5-prof-foot" }, [
        canHint ? el("div", { class: "d5-chips" }, [el("button", { type: "button", class: "d5-chip", disabled: busy || null, onclick: () => ask("hint") }, `Give me ${nextLevel}`)]) : null,
        status && status.prior_hints ? el("small", { class: "d5-hint" }, `${status.prior_hints} hint${status.prior_hints === 1 ? "" : "s"} used on this step`) : null,
        input,
        el("button", { type: "button", class: "d5-btn small", disabled: paused || busy || null, onclick: () => { if (draft.trim()) ask("ask", draft.trim()); } }, "Ask"),
      ]),
    );
  }

  draw();
  load();
  return host;
}
