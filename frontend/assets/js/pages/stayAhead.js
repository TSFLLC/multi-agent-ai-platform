import { api } from "../api.js";
import { clear, el } from "../dom.js";
import { formatDateTime } from "../format.js";
import { navigate } from "../router.js";
import {
  REVIEW_INTRO,
  STAY_AHEAD_INTRO,
  allEmptyText,
  reviewOutcome,
  stayAheadModel,
  validateReviewSelection,
} from "../stayAhead.js";

function chips(labels) {
  return labels.length
    ? el("div", { class: "chip-row radar-reasons" }, labels.map((label) => el("span", { class: "badge badge-neutral" }, label)))
    : null;
}

// Links only: an <a> that opens an existing page. There is deliberately no
// button here, so nothing on this block can start work or change a record.
function linkRow(links) {
  return links.length
    ? el("div", { class: "row stay-ahead-links" }, links.map((link) => el("a", { class: "button-link", href: link.href }, link.label)))
    : null;
}

function absorbedBlock(reason) {
  return el("div", { class: "stay-ahead-reason" }, [
    el("div", { class: "eyebrow" }, reason.familyLabel),
    el("strong", {}, reason.title),
    el("p", {}, reason.what),
    el("p", { class: "hint" }, reason.why),
    chips(reason.reasons),
    linkRow(reason.links),
  ]);
}

export function stayAheadCard(card) {
  return el("article", { class: "card radar-card compact stay-ahead-card" }, [
    el("div", { class: "row between" }, [
      el("div", { class: "eyebrow" }, card.eyebrow),
      card.learnerState ? el("span", { class: "badge badge-neutral" }, `Your record: ${card.learnerState.label}`) : null,
    ]),
    el("h3", {}, card.title),
    el("p", { class: "stay-ahead-what" }, [el("strong", {}, "What changed: "), card.what]),
    el("p", { class: "stay-ahead-why" }, [el("strong", {}, "Why you are seeing this: "), card.why]),
    chips(card.reasons),
    card.absorbed.length ? el("div", { class: "stack stay-ahead-absorbed" }, card.absorbed.map(absorbedBlock)) : null,
    linkRow(card.links),
    el("p", { class: "hint" }, `Latest recorded change ${formatDateTime(card.changedAt)}`),
  ]);
}

// -- AIL.4B review card ------------------------------------------------------------------

const defaultReviewApi = {
  start: (conceptId) => api.post("/learning-reviews", { concept_id: conceptId }),
  complete: (attemptId, selected) => api.post(`/learning-reviews/${encodeURIComponent(attemptId)}/complete`, { selected }),
};

function errorText(error, fallback) {
  return (error && error.message) || fallback;
}

// The whole review runs inside the card, and only after the user clicks. Each
// step is one explicit request: start (POST), then submit the answer (POST).
function reviewPanel(area, card, reviewApi) {
  const show = (...nodes) => {
    clear(area);
    nodes.filter(Boolean).forEach((node) => area.appendChild(node));
  };

  const showResult = (result) => {
    const outcome = reviewOutcome(result);
    show(
      el("div", { class: `stay-ahead-review-result ${outcome.passed ? "passed" : "not-passed"}` }, [
        el("p", {}, outcome.message),
        el("p", { class: "hint" }, outcome.note),
        card.conceptId ? el("a", {
          class: "button-link",
          href: `#/professor?intent=HELP_ME_REVIEW&target_type=concept&target_id=${encodeURIComponent(card.conceptId)}`,
        }, "Ask Professor about this review") : null,
      ]),
    );
  };

  const showQuestion = (started) => {
    const item = started.item;
    if (!item) {
      show(el("p", { class: "hint" }, "This review has no question to show right now."));
      return;
    }
    const selected = new Set();
    const message = el("p", { class: "hint stay-ahead-review-message" }, "");
    const submit = el("button", { class: "primary small", disabled: true }, "Submit answer");
    const inputs = item.options.map((option, index) => {
      const input = el("input", {
        type: item.multiple ? "checkbox" : "radio",
        name: `review-${started.attempt.id}`,
        value: String(index),
        onchange: () => {
          if (!item.multiple) selected.clear();
          if (input.checked) selected.add(index);
          else selected.delete(index);
          submit.disabled = validateReviewSelection(item, selected) !== null;
        },
      });
      return el("label", { class: "stay-ahead-review-option" }, [input, ` ${option}`]);
    });
    submit.onclick = async () => {
      const problem = validateReviewSelection(item, selected);
      if (problem) {
        message.textContent = problem;
        return;
      }
      submit.disabled = true;
      try {
        showResult(await reviewApi.complete(started.attempt.id, [...selected].sort((a, b) => a - b)));
      } catch (error) {
        message.textContent = errorText(error, "Your answer could not be submitted.");
        submit.disabled = false;
      }
    };
    show(
      el("div", { class: "stack stay-ahead-review-question" }, [
        el("strong", {}, item.title),
        item.body_md ? el("p", {}, item.body_md) : null,
        item.multiple ? el("p", { class: "hint" }, "Choose every answer that applies.") : null,
        el("div", { class: "stack" }, inputs),
        submit,
        message,
      ]),
    );
  };

  const begin = async () => {
    show(el("p", { class: "hint" }, "Starting your review…"));
    try {
      showQuestion(await reviewApi.start(card.conceptId));
    } catch (error) {
      show(el("p", { class: "error-banner" }, errorText(error, "The review could not be started.")), startButton());
    }
  };

  const startButton = () =>
    el("button", { class: "primary small", onclick: begin }, card.action.label);

  return { begin, startButton };
}

export function reviewCard(card, { reviewApi = defaultReviewApi } = {}) {
  const area = el("div", { class: "stay-ahead-review-action" }, []);
  const panel = reviewPanel(area, card, reviewApi);
  if (card.action.kind === "unavailable") {
    area.appendChild(el("p", { class: "hint stay-ahead-review-unavailable" }, card.action.message));
  } else {
    area.appendChild(panel.startButton());
  }
  return el("article", { class: `card radar-card compact stay-ahead-card stay-ahead-review ${card.kind.toLowerCase()}` }, [
    el("div", { class: "row between" }, [
      el("div", { class: "eyebrow" }, card.kindLabel),
      el("div", { class: "row" }, [
        el("span", { class: "badge badge-neutral" }, `Your record: ${card.learnerState.label}`),
        ...card.learnerState.overlays.map((label) => el("span", { class: "badge badge-neutral" }, label)),
      ]),
    ]),
    el("h3", {}, card.title),
    el("p", { class: "stay-ahead-what" }, [el("strong", {}, "What changed: "), card.what]),
    el("p", { class: "stay-ahead-why" }, [el("strong", {}, "Why you are seeing this: "), card.why]),
    chips(card.reasons),
    card.baseline ? el("p", { class: "hint stay-ahead-baseline" }, card.baseline) : null,
    card.interval ? el("p", { class: "hint stay-ahead-interval" }, card.interval) : null,
    card.attempt ? el("p", { class: "hint stay-ahead-attempt" }, card.attempt) : null,
    area,
  ]);
}

function sectionView(section) {
  const render = section.cardType === "review" ? reviewCard : stayAheadCard;
  return el("section", { class: "stack today-section stay-ahead-section" }, [
    el("div", { class: "row between" }, [el("h2", {}, section.title), el("span", { class: "hint" }, section.countText)]),
    section.cardType === "review" && section.cards.length ? el("p", { class: "hint" }, REVIEW_INTRO) : null,
    section.quotaText ? el("p", { class: "hint stay-ahead-quota" }, section.quotaText) : null,
    section.cards.length
      ? el("div", { class: "stack" }, section.cards.map((card) => render(card)))
      : el("p", { class: "hint stay-ahead-empty" }, section.empty),
  ]);
}

export function stayAheadBlock(data) {
  const model = stayAheadModel(data);
  return el("section", { class: "stack stay-ahead" }, [
    el("div", {}, [el("h2", {}, "Stay ahead"), el("p", { class: "hint" }, STAY_AHEAD_INTRO)]),
    model.allEmpty
      ? el("div", { class: "empty-state stay-ahead-empty" }, allEmptyText(model.windowDays))
      : el("div", { class: "stack" }, model.sections.map(sectionView)),
  ]);
}

// A failure here must never take Today down: the Radar sections still render.
export function stayAheadUnavailable(error) {
  const detail = error && error.message ? `: ${error.message}` : ".";
  return el("section", { class: "stack stay-ahead" }, [
    el("h2", {}, "Stay ahead"),
    el("div", { class: "error-banner" }, `Stay ahead could not be loaded${detail} The rest of Today is unaffected.`),
  ]);
}
