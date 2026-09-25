// AIL.5C Assessment + Demonstration — pure view models.
//
// No DOM and no network here: everything the pages need to *decide* (labels,
// grouping, draft building, submit validation, remediation links) lives in
// this module so it can be unit-tested. Language is deliberate: "needs more
// work" not "failed", "not enough confidence to decide", and never
// "certified", "mastered", "expert" or "accredited".

const OUTCOMES = {
  passed: { label: "Passed", tone: "good", plain: "You passed this assessment." },
  needs_work: { label: "Needs more work", tone: "warn", plain: "This needs more work. Here is exactly what to strengthen." },
  provisional: { label: "Not enough confidence to decide", tone: "neutral", plain: "There was not enough confidence to decide. Nothing changed. You can ask for a review or try again." },
  human_review_required: { label: "A person needs to review this", tone: "neutral", plain: "Two judgments disagreed, so a person needs to review this. Nothing changed." },
  unable_to_assess: { label: "Could not be assessed", tone: "neutral", plain: "We could not assess this right now. Nothing changed. You can try again." },
};

const EFFECTS = {
  counts_toward_demonstrated: "Recorded as evidence toward Demonstrated",
  counts_toward_practiced_only: "Counts as practice, not yet as demonstration",
  formative_only: "Feedback only — not recorded as evidence",
  none: "Nothing was recorded",
};

const FINDINGS = { met: "Met", partial: "Partly met", not_met: "Not met", not_applicable: "Not applicable" };
const CONFIDENCE = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" };
const COST = { exact: "exact cost", estimated: "estimated cost", unknown: "cost unknown" };
const KIND = {
  knowledge_check: "Knowledge check", explain_back: "Explain-back", modification: "Modification challenge",
  reproduction: "Reproduction challenge", debugging: "Debugging challenge", experiment_interpretation: "Experiment interpretation",
  project: "Project assessment", capstone: "Capstone assessment",
};
const DECISIONS = {
  confirm: "Confirm the result", override_pass: "Change to passed", override_needs_work: "Change to needs more work", new_assessment: "Issue a new assessment",
};
const HELP = {
  h0: "independently (H0)", h1: "with a conceptual clue (H1)", h2: "with a targeted pointer (H2)",
  h3: "with partial structure (H3)", h4: "with a guided walkthrough (H4)", h5: "after seeing the solution (H5)",
};
const BLOCKED = /\b(certified|certification|accredited|mastered|mastery|expert|professional-level)\b/i;

export const outcomeInfo = (outcome) => OUTCOMES[outcome] || { label: "In progress", tone: "neutral", plain: "Your attempt is in progress." };
export const effectLabel = (effect) => EFFECTS[effect] || EFFECTS.none;
export const findingLabel = (finding) => FINDINGS[finding] || "Not judged";
export const confidenceLabel = (confidence) => CONFIDENCE[confidence] || "";
export const costLabel = (status) => COST[status] || COST.unknown;
export const kindLabel = (kind) => KIND[kind] || "Assessment";
export const decisionLabel = (decision) => DECISIONS[decision] || decision;
export const helpLabel = (level) => HELP[String(level || "").toLowerCase()] || "with no help recorded";
export const isTruthfulText = (text) => !BLOCKED.test(String(text || "").replace("Not a certificate or credential.", ""));

export const CENTER_SECTIONS = [
  ["ready_for_assessment", "Ready for assessment", "Everything needed is in place. Start when you are ready."],
  ["in_progress", "In progress", "Pick up where you left off."],
  ["needs_work", "Needs more work", "Specific gaps and what to do next."],
  ["provisional_or_review", "Provisional or waiting for review", "Nothing changed for these. You can request a review or try again."],
  ["demonstrated", "Demonstration records", "What you have shown, with the evidence behind it."],
  ["not_yet_ready", "Not ready yet", "What is still missing for each one."],
  ["history", "All attempts", "Every attempt is kept, including ones that needed more work."],
];

export const emptyCenterText = "Nothing is ready yet. Finish a project or lesson to unlock an assessment.";

export function centerModel(center) {
  const sections = CENTER_SECTIONS.map(([key, title, hint]) => ({ key, title, hint, items: (center && center[key]) || [] }));
  const visible = sections.filter((s) => s.items.length);
  return { sections, visible, empty: !visible.length };
}

export function attemptStatusText(attempt) {
  return {
    draft: "In progress — Assessment Mode is on",
    submitted: "Submitted — being checked",
    checking: "Checking your work",
    awaiting_grading: "Waiting for the Grader — safe to retry",
    grading: "The Grader is working",
    finalized: "Finished",
    abandoned: "You left this assessment",
  }[attempt && attempt.status] || "In progress";
}

export function assessmentModeBanner(attempt) {
  if (!attempt || attempt.status !== "draft") return null;
  const resources = (attempt.definition?.allowed_resources || []).join("; ") || "your lessons and your own work";
  return {
    title: "ASSESSMENT MODE",
    lines: [
      "The Mentor and Professor are paused for this Concept while this is open. Your Mentor history is safe.",
      `Allowed: ${resources}. Not allowed: AI assistants.`,
      "Nothing is monitored: no camera, screen, keyboard or clipboard tracking. Only the clock on the server.",
    ],
  };
}

export function readinessModel(readiness) {
  const checks = (readiness && readiness.checks) || [];
  return {
    ready: Boolean(readiness && readiness.ready),
    unmet: checks.filter((c) => !c.met),
    met: checks.filter((c) => c.met),
    resumeAttemptId: (checks.find((c) => c.resume_attempt_id) || {}).resume_attempt_id || null,
    fresh: readiness && readiness.fresh_required
      ? { required: true, why: freshReasonText(readiness.fresh_reason) }
      : { required: false, why: "" },
    sourceWork: (readiness && readiness.source_work && readiness.source_work.levels) || [],
  };
}

export function freshReasonText(reason) {
  return {
    policy_always: "This assessment always uses a fresh challenge so it shows what you can do on your own.",
    source_work_partially_assisted: "Your project work had partial help, so a fresh challenge will show what you can do on your own.",
    source_work_substantially_assisted: "Your project work had substantial help, so a fresh independent challenge is needed. The source project alone cannot show demonstration.",
    study_mode_used: "You used Study Mode, so a fresh challenge is needed.",
  }[reason] || "";
}

export function sourceWorkText(levels) {
  return levels && levels.length ? `Your project work: ${levels.map(helpLabel).join(", ")}.` : "";
}

// -- draft building ------------------------------------------------------------------------------------------------

const setPath = (target, path, value) => {
  const parts = path.split(".");
  let node = target;
  parts.slice(0, -1).forEach((part) => { node[part] = node[part] && typeof node[part] === "object" ? node[part] : {}; node = node[part]; });
  node[parts[parts.length - 1]] = value;
};

export const splitIds = (text) => String(text || "").split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);

/** Builds the draft object the API expects from what the learner typed. Pure. */
export function buildDraft(challenge, fieldDefs, values) {
  const draft = {};
  for (const item of (challenge && challenge.items) || []) {
    const entry = (values.items || {})[item.entry_key];
    if (entry === undefined || entry === null) continue;
    if (Array.isArray(item.options)) setPath(draft, `responses.${item.entry_key}.selected`, entry.map(Number));
    else if (String(entry).trim()) setPath(draft, `responses.${item.entry_key}.text`, String(entry));
  }
  for (const def of fieldDefs || []) {
    const raw = (values.fields || {})[def.path];
    if (raw === undefined || raw === null || raw === "") continue;
    setPath(draft, def.path, def.input === "ids" ? splitIds(raw) : def.input === "id" ? String(raw).trim() : raw);
  }
  if (values.reflection && values.reflection.trim()) setPath(draft, "fields.reflection", values.reflection);
  return draft;
}

export const DECLARATIONS = [
  ["no_external_help", "I used no outside help"],
  ["used_docs", "I used documentation or my notes"],
  ["used_ai_assistant", "I used an AI assistant"],
  ["other", "Something else"],
];

export function declarationNote(declaration) {
  return declaration === "used_ai_assistant"
    ? "That is fine to say. This attempt will be feedback only and will not be recorded as evidence."
    : "Your declaration is self-reported and is recorded as such.";
}

export function validateSubmit({ declaration }) {
  return declaration ? { ok: true, message: "" } : { ok: false, message: "Choose a declaration about the help you used before you submit." };
}

export function timeLeftText(attempt, now = Date.now()) {
  if (!attempt || !attempt.expires_at) return "";
  const ms = Date.parse(attempt.expires_at.endsWith("Z") || /[+-]\d\d:?\d\d$/.test(attempt.expires_at) ? attempt.expires_at : `${attempt.expires_at}Z`) - now;
  if (Number.isNaN(ms)) return "";
  if (ms <= 0) return "This attempt has expired.";
  const hours = Math.floor(ms / 3.6e6);
  const minutes = Math.floor((ms % 3.6e6) / 6e4);
  return `About ${hours}h ${minutes}m left. The clock is server-side only.`;
}

// -- results -----------------------------------------------------------------------------------------------------------------

export function resultModel(view) {
  const result = view && view.result;
  if (!result) {
    return {
      finalized: false,
      pending: view && view.pending ? view.pending : null,
      canRetryGrading: Boolean(view && view.status === "awaiting_grading"),
    };
  }
  const report = result.report || {};
  const answers = report.answers || {};
  const info = outcomeInfo(result.outcome);
  return {
    finalized: true,
    id: result.id,
    outcome: result.outcome,
    info,
    effect: effectLabel(result.demonstration_effect),
    headline: report.headline || info.plain,
    fact: report.platform_fact || null,
    judgment: report.grader_judgment || null,
    reflection: report.learner_reflection || null,
    coaching: report.professor_coaching || null,
    answers,
    gaps: result.gaps || [],
    remediation: result.remediation || [],
    hasRecord: Boolean(result.has_record),
    stateChanges: Object.values(answers.learner_state || {}),
    review: reviewModel(view.reviews || []),
    history: view.history || [],
    coachingHref: `#/professor?intent=HELP_ME_AFTER_ASSESSMENT&target_type=assessment_result&target_id=${encodeURIComponent(result.id)}`,
  };
}

export function remediationLink(step, ctx = {}) {
  switch (step.kind) {
    case "milestone": return ctx.projectAttemptId ? `#/academy/projects/attempts/${encodeURIComponent(ctx.projectAttemptId)}` : "#/academy/projects";
    case "definition": return `#/academy/assessments/definitions/${encodeURIComponent(step.ref)}`;
    case "experiment": return "#/ail/lab";
    case "professor": return "#/professor";
    case "retry": return ctx.definitionKey ? `#/academy/assessments/definitions/${encodeURIComponent(ctx.definitionKey)}` : "#/academy/assessments";
    case "learning_item":
    default: return "#/academy";
  }
}

export function reviewModel(reviews) {
  const open = reviews.find((r) => r.status === "open") || null;
  return {
    open,
    latest: reviews[reviews.length - 1] || null,
    canRequest: !open,
    needsConsent: Boolean(open && !open.consented),
    canWithdraw: Boolean(open),
    summary: open
      ? (open.consented ? "A reviewer will look at this attempt only." : "A review is waiting for your consent before anyone can see your attempt.")
      : "",
  };
}

export function reviewPayload({ reason, consent }) {
  const text = String(reason || "").trim();
  if (text.length < 10) return { ok: false, message: "Tell us why in at least 10 characters." };
  if (!consent) return { ok: false, message: "A review needs your explicit consent to share this attempt with a reviewer." };
  return { ok: true, body: { reason: text, consent: true } };
}

export const REVIEW_SEES = [
  "Only this attempt: your responses, the challenge you were given, and the checks and judgments.",
  "Your declaration about the help you used.",
  "Nothing else: not your other attempts, your Mentor chats, or your profile.",
];

// -- Demonstration Record ------------------------------------------------------------------------------------------------------

export function recordStatusText(status) {
  return {
    valid: "Current",
    superseded_by_review: "Replaced by a later review decision (history is kept)",
    changed_since: "The Concept changed since — consider reassessing",
    review_due: "A review is due",
  }[status] || "Current";
}

export function recordModel(payload) {
  const record = payload.record;
  return {
    title: record.title,
    notice: record.notice,
    status: recordStatusText(payload.status && payload.status.status),
    statusReasons: (payload.status && payload.status.reasons) || [],
    concepts: record.concepts.map((c) => `${c.concept_name} (Concept Version ${c.concept_version})`),
    assessment: `${kindLabel(record.assessment.kind)} · ${record.assessment.definition_key} v${record.assessment.definition_version}`,
    project: record.project ? `${record.project.title} (template v${record.project.version})` : "",
    evidence: record.evidence.map((e) => `${e.evidence_type.replaceAll("_", " ")} — help level ${e.assistance_level ? e.assistance_level.toUpperCase() : "none recorded"}, verification ${(e.execution_verification || "not applicable").replaceAll("_", " ")}, judged by ${e.grader.replaceAll("_", " ")}`),
    verified: record.provenance.verified_by_platform,
    selfReported: record.provenance.self_reported,
    grader: record.grader.ran ? `Grader Agent Version ${record.grader.agent_version_id}; cross-check ${record.grader.crosscheck_ran ? "ran" : "did not run"}` : "No AI judgment was used.",
    hash: payload.record_hash,
  };
}

export const recordFileName = (payload, format) => `demonstration-record-${payload.record.assessment.definition_key}-${String(payload.record_hash || "").slice(0, 8)}.${format}`;
