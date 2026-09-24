// Pure view logic for the "Stay ahead" block on Today (AIL.4A). No DOM and no
// network, so it is unit-testable in plain Node. Every string mirrors a fact
// the backend recorded (reason codes, template sentences, evidence refs);
// nothing is scored, ranked, or invented client-side, and no link performs an
// action: following one only opens an existing Radar, Model or Lab page.

import { ladderLabel } from "./labLearning.js";

// Fixed presentation order. These are sections, not a ranking of importance.
export const STAY_AHEAD_SECTIONS = [
  {
    key: "worth_revisiting",
    title: "Worth revisiting",
    intro: "Things you have learned that now have recorded changes connected to them.",
    empty: "Nothing you have learned has a recorded change right now.",
    eyebrow: "Worth revisiting",
  },
  {
    key: "used_models_changed",
    title: "Models that changed since they were used",
    intro: "Models from your own Personal Lab experiments, or used in opted-in projects you can access, whose catalog record has changed since they were last used. Each card says which of the two it is.",
    empty: "None of the models used in your Lab or opted-in projects has a recorded change in this window.",
    eyebrow: "Model used",
  },
  {
    key: "watched_developments",
    title: "Watched developments with updates",
    intro: "Developments you chose to watch that have new recorded evidence.",
    empty: "None of the developments you are watching has new evidence.",
    eyebrow: "Watched development",
  },
  {
    key: "experiments_to_rerun",
    title: "Experiments worth rerunning",
    intro: "Finished Personal Lab experiments whose models have changed since they ran.",
    empty: "No finished experiment looks out of date.",
    eyebrow: "Experiment",
  },
  {
    key: "concepts_changed",
    title: "Concepts with relevant changes",
    intro: "Concepts you learned or chose to watch that have a new version or a new linked development.",
    empty: "No Concept you learned or watch has changed.",
    eyebrow: "Concept",
  },
];

export const STAY_AHEAD_INTRO =
  "Changes that matter to you, based only on what you learned, tested, used or watched. Nothing here runs an experiment, changes your learning record, or makes a decision for you.";

const REASON_LABELS = {
  PERSONAL_USAGE: "You used it in Personal Lab",
  OPTED_IN_PROJECT_USAGE: "Used in an opted-in project you can access",
  HAS_LEARNING_EVIDENCE: "You have learning evidence",
  WATCHING_CONCEPT: "You are watching this Concept",
  WATCHING_DEVELOPMENT: "You are watching this development",
  EXPERIMENT_COUNTED_TOWARD_LEARNING: "Counted toward your learning",
  MODEL_PRICE_CHANGED: "Price changed",
  MODEL_CONTEXT_CHANGED: "Context window changed",
  MODEL_CAPABILITY_CHANGED: "Capabilities changed",
  MODEL_STATUS_CHANGED: "Availability changed",
  WATCH_NEW_EVIDENCE: "New evidence since you started watching",
  WATCH_CONCEPT_LINK_CONFIRMED: "Concept link confirmed",
  WATCH_REVISIT_DATE_REACHED: "Your revisit date arrived",
  CONCEPT_NEW_MATERIAL_VERSION: "New material version",
  CONCEPT_NEW_LINKED_DEVELOPMENT: "New linked development",
};

const FAMILY_LABELS = {
  USED_MODEL_CHANGED: "Model you used changed",
  EXPERIMENT_MAY_BE_STALE: "Experiment may be out of date",
  WATCHED_DEVELOPMENT_CHANGED: "Watched development changed",
  CONCEPT_CHANGED: "Concept changed",
  WORTH_REVISITING: "Worth revisiting",
};

// Every link kind maps to a page that already exists; unknown kinds get no
// href rather than a guessed one.
export function hrefForLink(link) {
  if (!link || !link.id) return null;
  const id = encodeURIComponent(link.id);
  if (link.kind === "model") return `#/models/${id}/explore`;
  if (link.kind === "development") return `#/ail/radar/developments/${id}`;
  if (link.kind === "experiment") return `#/ail/lab/experiments/${id}`;
  return null;
}

export function reasonLabel(code) {
  return REASON_LABELS[code] || String(code || "Recorded reason").replaceAll("_", " ").toLowerCase();
}

export function familyLabel(family) {
  return FAMILY_LABELS[family] || String(family || "").replaceAll("_", " ").toLowerCase();
}

export function sectionCountText(section) {
  const total = Number(section?.total || 0);
  const shown = Number(section?.shown || 0);
  if (!total) return "";
  return shown < total ? `${shown} of ${total} shown` : `${total} shown`;
}

function linkModels(links) {
  return (links || [])
    .map((link) => ({ label: link.label, href: hrefForLink(link) }))
    .filter((link) => link.href);
}

function reasonModel(reason) {
  return {
    id: reason.id,
    family: reason.family,
    familyLabel: familyLabel(reason.family),
    title: reason.title,
    what: reason.what_changed,
    why: reason.why,
    reasons: (reason.reason_codes || []).map(reasonLabel),
    links: linkModels(reason.links),
    changedAt: reason.changed_at,
  };
}

// Personal and shared usage are different facts. Only the backend's
// usage_scope may say "you used"; project usage is never called personal.
export function usageEyebrow(signal, fallback) {
  const scope = signal?.subject?.usage_scope;
  if (scope === "shared_project") return "Model used in an opted-in project";
  if (scope === "personal" || scope === "personal_and_shared") return "Model you used";
  return fallback;
}

// One card = what changed, why you are seeing it, what you can open next.
export function cardModel(signal, eyebrow) {
  const base = reasonModel(signal);
  const state = signal.learner_state;
  return {
    ...base,
    eyebrow: usageEyebrow(signal, eyebrow || base.familyLabel),
    learnerState: state ? { label: ladderLabel(state.ladder), overlays: state.overlays || [] } : null,
    // Only WORTH_REVISITING carries absorbed signals; each keeps its own
    // reason labels and links, so nothing is summarised into a score.
    absorbed: (signal.reasons || []).map(reasonModel),
  };
}

export function sectionModel(config, section) {
  const items = (section?.items || []).map((item) => cardModel(item, config.eyebrow));
  return { ...config, total: Number(section?.total || 0), countText: sectionCountText(section), cards: items };
}

export function stayAheadModel(data) {
  const sections = STAY_AHEAD_SECTIONS.map((config) => sectionModel(config, data?.sections?.[config.key]));
  return {
    windowDays: Number(data?.window_days || 0),
    sections,
    allEmpty: sections.every((section) => section.total === 0),
  };
}

export function allEmptyText(windowDays) {
  return `Nothing you have learned, tested, used or watched has a recorded change in the last ${windowDays} days.`;
}
