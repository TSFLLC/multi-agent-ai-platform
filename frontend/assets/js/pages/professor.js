import { api } from "../api.js";
import { el, mount } from "../dom.js";

const intents = [
  ["WHAT_SHOULD_I_LEARN_NEXT", "What should I learn next?"],
  ["EXPLAIN_THIS", "Explain something"],
  ["UNDERSTAND_MY_EXPERIMENT", "Help me understand an experiment"],
  ["HELP_ME_REVIEW", "Help me review"],
  ["WHY_DOES_THIS_MATTER", "Why does this matter?"],
];

const labels = {
  learning_record: "Learning record",
  external_knowledge: "External fact",
  provider_claim: "Provider claim",
  platform_observation: "Platform observation",
  user_authored_conclusion: "Your conclusion",
  ai_explanation: "AI Professor explanation",
};

export function statusMessage(status) {
  return {
    preparing: "Preparing context…",
    asking: "Asking Professor…",
    complete: "Complete",
    unavailable: "Professor unavailable",
    budget_exceeded: "Budget limit",
    validation_error: "Validation error",
    authorization_error: "Authorization error",
  }[status] || String(status || "Unavailable").replaceAll("_", " ");
}

function evidenceList(response) {
  const refs = [...(response.evidence || []), ...(response.attachment_references || [])];
  return el("details", { class: "professor-evidence" }, [
    el("summary", {}, `Evidence & sources (${refs.length})`),
    refs.length
      ? el("ul", {}, refs.map((ref) => el("li", {}, [
        el("strong", {}, labels[ref.provenance_kind] || ref.provenance_kind),
        ` · ${ref.role}`,
        ref.claim_type ? ` · ${ref.claim_type}` : "",
        ref.conflict_group ? " · conflicting evidence group" : "",
      ])))
      : el("p", { class: "hint" }, "No additional evidence was selected for this answer."),
  ]);
}

function responseCard(response, onContinue) {
  if (!response.direct_answer) {
    return el("div", { class: "card professor-error" }, [
      el("div", { class: "eyebrow" }, statusMessage(response.status)),
      el("p", {}, response.error_message || "Professor could not provide an answer. Your learning records were not changed."),
      response.error_kind === "validation_error" ? el("p", { class: "hint" }, "The model response was rejected because it did not satisfy the grounded response contract.") : null,
    ]);
  }
  const actions = (response.suggested_next_actions || []).map((action) => el("li", {}, [action.reason]));
  return el("div", { class: "stack professor-result" }, [
    el("div", { class: "card professor-direct-answer" }, [
      el("div", { class: "eyebrow" }, "Direct answer"),
      el("p", { class: "professor-answer" }, response.direct_answer),
    ]),
    response.explanation ? el("div", { class: "card" }, [el("h2", {}, "Why / explanation"), el("p", {}, response.explanation)]) : null,
    el("div", { class: "card" }, [
      el("h2", {}, "Next steps"),
      actions.length ? el("ul", {}, actions) : el("p", { class: "hint" }, "No next action was suggested. You decide what to do next."),
    ]),
    evidenceList(response),
    el("button", { class: "button-link", type: "button", onclick: onContinue }, "Ask a follow-up"),
  ].filter(Boolean));
}

export async function renderProfessor(root) {
  const state = { intent: "ASK_PROFESSOR", question: "", response: null, busy: false, previous: null };
  const render = () => {
    const question = el("textarea", {
      class: "professor-question",
      placeholder: "Ask about what you’re learning, testing, reviewing, or seeing in AI…",
      oninput: (event) => { state.question = event.target.value; },
    });
    question.value = state.question;
    const status = el("p", { class: "hint", role: "status" }, state.busy ? statusMessage("asking") : "");
    const ask = async () => {
      if (state.busy || !state.question.trim()) return;
      state.busy = true;
      render();
      try {
        state.response = await api.post("/professor/interactions", { intent: state.intent, question: state.question });
        state.previous = state.response.interaction_id;
      } catch (error) {
        state.response = { status: error.status === 403 ? "authorization_error" : "unavailable", error_message: error.message };
      } finally {
        state.busy = false;
        render();
      }
    };
    const quick = intents.map(([intent, label]) => el("button", {
      type: "button",
      class: state.intent === intent ? "primary small" : "button-link",
      onclick: () => { state.intent = intent; state.question = label; render(); },
    }, label));
    const continueAsk = async () => {
      const follow = window.prompt("What would you like Professor to clarify?", "Can you explain that more simply?");
      if (!follow || !state.previous) return;
      state.busy = true;
      render();
      try {
        state.response = await api.post(`/professor/interactions/${encodeURIComponent(state.previous)}/continue`, {
          intent: state.intent,
          question: follow,
          previous_interaction_id: state.previous,
        });
        state.previous = state.response.interaction_id;
      } catch (error) {
        state.response = { status: "unavailable", error_message: error.message };
      } finally {
        state.busy = false;
        render();
      }
    };
    mount(root, el("div", { class: "stack professor-page" }, [
      el("div", { class: "page-header" }, [
        el("h1", {}, "AI Professor"),
        el("div", { class: "subtitle" }, "Ask about what you have learned, tested, reviewed, or what is changing in AI."),
      ]),
      el("div", { class: "card" }, [
        el("label", {}, "Your question"),
        question,
        el("div", { class: "row professor-actions" }, [el("button", { class: "primary", type: "button", disabled: state.busy, onclick: ask }, state.busy ? "Asking…" : "Ask Professor")]),
        status,
      ]),
      el("div", { class: "card" }, [el("h2", {}, "Quick actions"), el("div", { class: "row professor-quick-actions" }, quick)]),
      state.response ? responseCard(state.response, continueAsk) : el("div", { class: "card professor-empty" }, [el("p", {}, "Your answer will appear here. Professor uses only the authorized learning context needed for this question.")]),
    ].filter(Boolean)));
  };
  render();
}
