// Discoverability links between the Academy pages and the existing AIL.5C Assessment Center.
//
// Nothing here starts, grades or records an assessment: every link resolves to a route the
// Assessment Center already owns (#/academy/assessments/...). The pages that show these links
// only *point at* the existing definition; readiness, attempts, the Grader and evidence all stay
// in the assessment API.

import { el } from "./dom.js";

export const ACADEMY_HOME_HREF = "#/academy";
export const LEVEL1_HREF = "#/academy/level-1";
export const ASSESSMENT_CENTER_HREF = "#/academy/assessments";

export const assessmentDefinitionHref = (key) => `${ASSESSMENT_CENTER_HREF}/definitions/${encodeURIComponent(key)}`;

// The assessment a Level 1 Day is bound to, or null when the Day has none.
// The bound key lives on the Day's spec (spec.assessment_definition_key, mirrored in
// spec.explain_back.assessment_definition_key). Capstone Days keep their own flow, which needs a
// project attempt and already opens the Assessment Center (Day 29), so they get no second action here.
export function level1AssessmentAction(item) {
  const spec = (item && item.spec) || {};
  const key = spec.assessment_definition_key || (spec.explain_back && spec.explain_back.assessment_definition_key) || null;
  if (!key || item.capstone_stage) return null;
  return {
    key,
    heading: "Demonstrate what you learned",
    label: "Take the assessment",
    hint: "An authored assessment for this Day. It is graded separately, and nothing is recorded until you submit it.",
    href: assessmentDefinitionHref(key),
  };
}

// One row of links shown at the top of the Academy pages. It never depends on an enrollment.
export function academyEntryLinks({ current } = {}) {
  const links = [
    ["home", "Academy Home", ACADEMY_HOME_HREF],
    ["level1", "30-day Level 1 program", LEVEL1_HREF],
    ["assessments", "Assessment Center", ASSESSMENT_CENTER_HREF],
  ].filter(([id]) => id !== current);
  return el("nav", { class: "row academy-entry-links", "aria-label": "Academy shortcuts" }, links.map(([, label, href]) => el("a", { class: "button-link", href }, label)));
}
