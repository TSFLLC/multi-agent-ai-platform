import { el } from "../dom.js";
import { formatDateTime } from "../format.js";
import { STAY_AHEAD_INTRO, allEmptyText, stayAheadModel } from "../stayAhead.js";

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

function sectionView(section) {
  return el("section", { class: "stack today-section stay-ahead-section" }, [
    el("div", { class: "row between" }, [el("h2", {}, section.title), el("span", { class: "hint" }, section.countText)]),
    section.cards.length
      ? el("div", { class: "stack" }, section.cards.map(stayAheadCard))
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
