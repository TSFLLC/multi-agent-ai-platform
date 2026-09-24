// Pure view logic for the Personal Lab results page (AIL.3C). No DOM and no
// network here, so it is unit-testable in plain Node. Every string mirrors a
// backend fact (qualification status, evaluation status, run rows); nothing is
// scored, ranked, or invented client-side.

export const CONCLUSION_OPTIONS = [
  { value: "no_meaningful_difference", label: "No meaningful difference" },
  { value: "tradeoff", label: "It's a tradeoff" },
  { value: "inconclusive", label: "Inconclusive" },
  { value: "more_testing_needed", label: "I need more testing" },
  { value: "custom", label: "In my own words" },
];

export const CONCLUSION_TEXT_MAX = 4000;
export const CONCLUSION_PLACEHOLDER = "Choose…";
export const NO_CONCLUSION_TEXT = "No conclusion saved yet.";
// Human Conclusion and Learning Evidence are separate records: a conclusion can
// be written or changed at any time, including after counting toward learning.
export const CONCLUSION_INTRO = "Your conclusion is your own reading of the results, saved on its own. It is separate from your learning evidence, so you can write or change it at any time, even after counting this experiment. It never has to name a winner and it does not change the evaluation.";

// Ordered hierarchy of the results page. There is deliberately no Professor
// entry: no canonical Professor exists, so nothing here may pretend one does.
export const RESULT_SECTIONS = ["tested", "happened", "evaluation", "differed", "conclusion", "count", "next"];

const CONCLUDABLE = new Set(["COMPLETED", "PARTIAL", "FAILED", "EVALUATION_FAILED"]);

const LADDER_LABELS = {
  not_started: "Not started",
  exposed: "Exposed",
  understood: "Understood",
  practiced: "Practiced",
  demonstrated: "Demonstrated",
};

export function conclusionLabel(type) {
  return CONCLUSION_OPTIONS.find((option) => option.value === type)?.label || "Conclusion";
}

export function ladderLabel(ladder) {
  return LADDER_LABELS[ladder] || String(ladder || "").replaceAll("_", " ");
}

// Mirrors the backend lifecycle rule: a conclusion needs results to read.
export function canConclude(experiment) {
  return CONCLUDABLE.has(String(experiment?.overall_status || ""));
}

export function validateConclusionDraft({ type, text }) {
  if (!CONCLUSION_OPTIONS.some((option) => option.value === type)) return "Choose how you would describe the result.";
  const trimmed = String(text || "").trim();
  if (type === "custom" && !trimmed) return "Describe your conclusion in your own words.";
  if (trimmed.length > CONCLUSION_TEXT_MAX) return `Keep your conclusion under ${CONCLUSION_TEXT_MAX} characters.`;
  return null;
}

// Every conclusion type is a valid reading; none of them names a winner.
export function conclusionSummary(conclusion) {
  if (!conclusion || !conclusion.type) return null;
  return {
    label: conclusionLabel(conclusion.type),
    text: conclusion.text || "",
    note: "No winner was chosen. That is a valid conclusion.",
    concludedAt: conclusion.concluded_at || null,
  };
}

const QUALIFICATION = {
  READY: {
    tone: "ready",
    title: "Ready to count toward learning",
    hint: "This records hands-on lab evidence for your chosen Concept. It does not matter which candidate scored higher: you are recording that you ran and reviewed a real experiment.",
  },
  ALREADY_COUNTED: {
    tone: "done",
    title: "Already counted toward learning",
    hint: "This experiment is part of your learning evidence for its Concept.",
  },
  MISSING_CONCEPT: {
    tone: "blocked",
    title: "Choose a Concept first",
    hint: "Pick the Concept this experiment taught you about.",
  },
  CONCEPT_REBIND_REQUIRED: {
    tone: "blocked",
    title: "Confirm your Concept",
    hint: "Choose the Concept again so the right version is recorded.",
  },
  EXPERIMENT_INCOMPLETE: {
    tone: "waiting",
    title: "The experiment has not finished yet",
    hint: "Come back when it has finished running.",
  },
  EVALUATION_NOT_CONFIGURED: {
    tone: "blocked",
    title: "This experiment was not evaluated",
    hint: "Only evaluated experiments can count, because the evaluation is the verified record of what happened.",
  },
  EVALUATION_PENDING: {
    tone: "waiting",
    title: "Evaluation is still pending",
    hint: "This will become available when the evaluation finishes.",
  },
  EVALUATION_FAILED: {
    tone: "blocked",
    title: "Evaluation failed",
    hint: "A failed evaluation is not a verified record, so this experiment cannot count yet.",
  },
  EVALUATION_INCOMPLETE: {
    tone: "waiting",
    title: "Some required evaluations are incomplete",
    hint: "Every run needs a finished evaluation before this can count.",
  },
  NO_MEANINGFUL_EVALUATION: {
    tone: "blocked",
    title: "Nothing to learn from yet",
    hint: "The evaluation did not produce results that apply to this experiment.",
  },
};

export function qualificationView(qualification) {
  if (!qualification) {
    return { status: "UNKNOWN", tone: "blocked", title: "Not available", message: "", hint: "", canCount: false, counted: false };
  }
  const known = QUALIFICATION[qualification.status];
  return {
    status: qualification.status,
    tone: known?.tone || "blocked",
    title: known?.title || "Not ready yet",
    message: qualification.message || "",
    hint: known?.hint || "",
    canCount: qualification.ready === true && qualification.status === "READY",
    counted: qualification.status === "ALREADY_COUNTED",
  };
}

export function countedMessage(result) {
  const ladder = ladderLabel(result?.learner_state?.ladder);
  return `Counted toward learning. Your level for this Concept is now "${ladder}". That level is worked out from all of your evidence, not set by this experiment.`;
}

// Evaluation status only. No criteria counts or denominators appear because the
// results payload carries none; they would be invented here.
export function evaluationView(experiment) {
  const status = experiment?.evaluation_status;
  if (!experiment?.evaluation_required || status === "NOT_REQUIRED" || !status) {
    return { status: "NOT_REQUIRED", headline: "This experiment was not evaluated.", detail: "" };
  }
  const headline = {
    PENDING: "Evaluation pending",
    RUNNING: "Evaluation running",
    FAILED: "Evaluation failed",
    COMPLETED: "Evaluation complete",
  }[status] || `Evaluation ${String(status).toLowerCase()}`;
  const detail = status === "COMPLETED"
    ? "The findings are recorded as evidence. They describe how each candidate performed and do not choose a winner."
    : "Findings appear only after the evaluation completes.";
  return { status, headline, detail };
}

// Candidates are labelled model-N / agent-N / repetition-N by the execution
// service, where N is the 1-based position of that model / agent version.
export function candidateName(label, experiment) {
  const text = String(label || "");
  const model = /^model-(\d+)$/.exec(text);
  if (model) return experiment?.models?.[Number(model[1]) - 1]?.name || `Model ${model[1]}`;
  const agent = /^agent-(\d+)$/.exec(text);
  if (agent) return `Agent version ${agent[1]}`;
  const repetition = /^repetition-(\d+)$/.exec(text);
  if (repetition) return `Repetition ${repetition[1]}`;
  return text || "Candidate";
}

export const FINDING_LABELS = [
  ["met", "Met"],
  ["partial", "Partly met"],
  ["not_met", "Not met"],
  ["not_applicable", "Not applicable"],
];

// A projection of the canonical MA6 finding counts the results API returns.
// Counts and denominators are shown exactly as given: no score, no ranking,
// no "better" or "best", and nothing is inferred.
export function findingsView(data) {
  const findings = data?.evaluation_findings;
  const experiment = data?.experiment;
  const note = "These are evaluation findings for each candidate. They are not a ranking, and no winner is chosen.";
  if (!findings?.evaluation_required) {
    return { available: false, candidates: [], message: "This experiment was not evaluated, so there are no findings to compare.", note };
  }
  const candidates = (findings.candidates || []).map((candidate) => ({
    name: candidateName(candidate.label, experiment),
    label: candidate.label,
    evaluated: `${candidate.runs_evaluated} of ${candidate.runs} runs evaluated`,
    hasFindings: candidate.total > 0,
    cells: FINDING_LABELS.map(([key, label]) => ({ key, label, count: candidate[key] || 0, of: candidate.total })),
  }));
  if (!candidates.some((candidate) => candidate.hasFindings)) {
    return { available: false, candidates, message: "Findings appear once the evaluation has completed.", note };
  }
  return { available: true, candidates, message: "", note };
}

export function conceptLabel(experiment) {
  return experiment?.concept?.name || (experiment?.concept_id ? "A Concept (name unavailable)" : "");
}

export function nextSteps(experiment, qualification) {
  const steps = [{ label: "Back to Personal AI Lab", href: "#/ail/lab" }];
  if (experiment?.development_id) {
    steps.push({
      label: "Back to the Radar development this came from",
      href: `#/ail/radar/developments/${encodeURIComponent(experiment.development_id)}`,
    });
  }
  if (qualification?.status === "ALREADY_COUNTED") {
    steps.push({ label: "See what is new for you on Today", href: "#/ail/today" });
  }
  return steps;
}
