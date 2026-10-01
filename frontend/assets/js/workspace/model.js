// AIL5D.6 — pure helpers for the Learning Workspace (no DOM, no network: runs under plain Node, see
// frontend/tests/workspaceModel.test.mjs). Everything here is a PRESENTATION decision derived from server data; the
// server (AcademyStepService / AcademyPracticeService / AIL.5C) still owns progress, completion, assistance and evidence.

export const STEP_META = {
  teach: { label: "Learn", icon: "▤", mode: "learning" },
  example: { label: "Example", icon: "◇", mode: "learning" },
  reflect: { label: "Reflect", icon: "✎", mode: "learning" },
  think: { label: "Think about it", icon: "?", mode: "learning" },
  check: { label: "Knowledge check", icon: "✓", mode: "learning" },
  explain_back: { label: "Explain back", icon: "✍", mode: "learning" },
  lab: { label: "Guided lab", icon: "⚗", mode: "practicing" },
  practice: { label: "Independent practice", icon: "↻", mode: "practicing" },
  reflection: { label: "Reflection", icon: "↺", mode: "learning" },
};

export const MODE_COPY = {
  learning: { label: "Learning", note: "You are learning. Nothing here is graded." },
  practicing: { label: "Practicing", note: "You are practicing. Safe to fail and repeat; it is recorded as practice, not as proof you know it." },
  demonstrating: { label: "Demonstrating", note: "You are demonstrating. This is graded separately in the Assessment Center, with the Professor paused." },
};

export const stepMeta = (type) => STEP_META[type] || { label: type, icon: "•", mode: "learning" };

// Learning / Practicing / Demonstrating, for the step the learner is on. Demonstrating is only ever the AIL.5C assessment.
export function workspaceMode(step, { inAssessment = false } = {}) {
  if (inAssessment) return "demonstrating";
  return stepMeta(step && step.type).mode;
}

// "#/academy/level-1/4?step=lab-run&practice=<id>" -> { day, step, practice }. Only plain tokens are accepted, so a hash
// can never smuggle a path, a URL or markup into the page (the server rebuilds every real return target itself).
const TOKEN = /^[A-Za-z0-9_-]{1,64}$/;
export function parseWorkspaceHash(hash) {
  const raw = String(hash || "").replace(/^#/, "");
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  const day = parts[0] === "academy" && parts[1] === "level-1" && /^\d{1,2}$/.test(parts[2] || "") ? Number(parts[2]) : null;
  const params = new URLSearchParams(query);
  const clean = (value) => (value && TOKEN.test(value) ? value : null);
  return { day, step: clean(params.get("step")), practice: clean(params.get("practice")) };
}

export function stepIndex(steps, key) {
  const i = steps.findIndex((s) => s.key === key);
  return i < 0 ? 0 : i;
}

// Where the workspace opens: an explicit, valid ?step=, else the server's current step, else the first.
export function initialStepKey(view, wanted) {
  const keys = view.steps.map((s) => s.key);
  if (wanted && keys.includes(wanted)) return wanted;
  if (view.current_step_key && keys.includes(view.current_step_key)) return view.current_step_key;
  return keys[0];
}

const COMPLETED = new Set(["completed"]);
export const isDone = (step) => COMPLETED.has(step.status);
// A lab that launches an existing platform engine (agent / workflow / build lab ...) rather than a Lab Kit practice. Its results live
// in that engine's own surface, so the Workspace cannot verify it: it is optional, and the learner moves on (the server records a skip).
export const isLauncherLab = (step) => step.type === "lab" && !(step.binding && step.binding.kit);
export const isSkipped = (step) => step.status === "skipped";

// May the learner press Continue? Completion is server-verified, so this only mirrors what the server will accept
// (it never decides it). Read-only steps always can; an interactive step needs its own recorded interaction.
export function canContinue(step, local = {}) {
  if (isDone(step) || isSkipped(step)) return true;
  switch (step.type) {
    case "teach":
    case "example":
      return true;
    case "reflect":
    case "reflection":
    case "explain_back":
      return Boolean(step.response);
    case "think":
      return Boolean(step.committed);
    case "check":
      return Boolean(step.check_result && step.check_result.ever_passed);
    case "lab":
      return isLauncherLab(step); // a kit lab is completed only by a completed Practice Instance (the server finishes the step itself)
    case "practice":
      return false;
    default:
      return Boolean(local.ready);
  }
}

export function continueHint(step) {
  if (isDone(step)) return "Step complete";
  switch (step.type) {
    case "reflect":
    case "reflection": return "Save your answer to continue";
    case "think": return "Lock in your answer to see the explanation";
    case "check": return "Every item must be right to continue — you may retry";
    case "explain_back": return "Save your outline to continue";
    case "lab": return isLauncherLab(step) ? "Optional — open the lab, or continue" : "Finish the guided lab to continue";
    case "practice": return step.required ? "Finish the practice to continue" : "Optional — try it, or continue";
    default: return "";
  }
}

export function progressText(view) {
  if (view.learning_complete) return "Learning complete";
  return `${view.required_completed} of ${view.required_total} required steps`;
}

// The Day's AIL.5C assessment, as the learner should read it. It never changes what "demonstrated" means.
export const ASSESSMENT_COPY = {
  not_started: ["Not started", "Graded separately. Finishing the lesson first is recommended."],
  in_progress: ["In progress", "You have a draft in the Assessment Center."],
  submitted: ["Submitted", "Waiting to be checked."],
  passed: ["Passed", "Recorded in the Assessment Center."],
  needs_work: ["Needs more work", "Review the feedback and try again."],
  in_review: ["In review", "A reviewer is looking at this."],
};
export function assessmentSummary(assessment) {
  if (!assessment) return null;
  const [label, note] = ASSESSMENT_COPY[assessment.status] || ASSESSMENT_COPY.not_started;
  return { label, note, status: assessment.status, key: assessment.definition_key };
}

export function learningStatusLine(view) {
  return {
    learning: view.learning_complete ? "Learning complete" : "Learning in progress",
    demonstrated: view.demonstrated ? "Concept demonstrated" : "Concept not yet demonstrated",
  };
}

export function hintsText(assistance) {
  if (!assistance || !assistance.tracked) return null;
  if (!assistance.help_count) return "No Professor help used";
  const hints = assistance.hints_used;
  return `${hints} hint${hints === 1 ? "" : "s"} used`;
}

// Guided Lab stages, derived from the server's own view of the instance (what is still required, what exists).
export const LAB_STAGES = [
  ["prediction", "Predict"], ["runs", "Run"], ["observation", "Observe"], ["change", "Change"], ["rerun", "Rerun"], ["comparison", "Compare"], ["reflection", "Reflect"],
];
export function labStages(practice) {
  const met = practice.met || {};
  const responses = practice.responses || {};
  const completedRuns = (practice.runs || []).filter((r) => r.state === "completed").length;
  const req = practice.scenario.requirements || {};
  const done = {
    prediction: Boolean(met.prediction),
    runs: completedRuns >= 1,
    observation: Boolean(responses.observation),
    change: Boolean(met.change) && completedRuns >= 2,
    rerun: completedRuns >= 2,
    comparison: Boolean(responses.comparison),
    reflection: Boolean(responses.reflection),
  };
  const stages = LAB_STAGES.filter(([k]) => {
    if (k === "prediction") return (practice.scenario.predictions || []).length > 0;
    if (k === "observation") return Boolean(req.require_observation);
    if (k === "change" || k === "rerun") return req.min_runs >= 2;
    if (k === "comparison") return Boolean(req.require_comparison);
    if (k === "reflection") return Boolean(req.require_reflection);
    return true;
  });
  const firstOpen = stages.findIndex(([k]) => !done[k]);
  const finished = practice.status === "completed";
  return stages.map(([key, label], i) => ({ key, label, done: finished || Boolean(done[key]), current: !finished && i === firstOpen }));
}

export function practiceActionHint(practice) {
  if (practice.status === "completed") return "Practice complete.";
  if (practice.status === "abandoned") return "This practice was abandoned.";
  const next = (practice.next_required || [])[0];
  const text = {
    prediction: "Make your prediction(s) before running anything.",
    runs: "Run it, then change one thing and run again.",
    change: "Change one variable and run again.",
    observation: "Record what you observed.",
    comparison: "Compare your runs.",
    reflection: "Write your reflection.",
  }[next];
  return text || "Keep going.";
}

export function limitsText(limits) {
  if (!limits) return "";
  const parts = [`${limits.runs_left} of ${limits.max_runs} runs left`];
  if (limits.budget_exhausted) parts.push("practice budget used up");
  return parts.join(" · ");
}

// A server-built return path ("#/academy/level-1/4?step=...&practice=...") is the only thing the app navigates to.
export function safeReturnHash(path) {
  const parsed = parseWorkspaceHash(path);
  return parsed.day && String(path).startsWith("#/academy/level-1/") ? path : "#/academy/level-1";
}

// A knowledge-check question has one list of options (a list), two named parts (choices is an object: Day 1), or numeric
// fields (no choices; the server names the fields and never the values).
export function checkParts(question) {
  const c = question.choices;
  if (Array.isArray(c)) return [{ name: null, options: c.map((o) => [o.key, o.text]) }];
  if (c && typeof c === "object") return Object.entries(c).map(([name, values]) => ({ name, options: values.map((v) => [v, String(v).replace(/_/g, " ")]) }));
  return (question.fields || []).map((name) => ({ name, numeric: true, options: [] }));
}

const isNumber = (v) => v !== "" && v !== null && v !== undefined && Number.isFinite(Number(v));

export function checkAnswered(question, picks) {
  const mine = picks[question.id];
  const parts = checkParts(question);
  if (parts.length === 0) return false;
  if (parts.length === 1 && parts[0].name === null) return typeof mine === "string" && mine !== "";
  return Boolean(mine) && parts.every((p) => (p.numeric ? isNumber(mine[p.name]) : typeof mine[p.name] === "string" && mine[p.name] !== ""));
}

// The payload the knowledge-check endpoint takes: numeric fields become numbers, everything else is sent as chosen.
export function checkPayload(questions, picks) {
  const out = {};
  for (const q of questions) {
    const mine = picks[q.id];
    const parts = checkParts(q);
    out[q.id] = parts.some((p) => p.numeric) ? Object.fromEntries(parts.map((p) => [p.name, Number(mine[p.name])])) : mine;
  }
  return out;
}

export function checkComplete(questions, picks) {
  return questions.length > 0 && questions.every((q) => checkAnswered(q, picks));
}

export function dayGroups(days) {
  const weeks = new Map();
  for (const d of days) {
    if (!weeks.has(d.week)) weeks.set(d.week, []);
    weeks.get(d.week).push(d);
  }
  return [...weeks.entries()].sort((a, b) => a[0] - b[0]).map(([week, rows]) => ({ week, days: rows }));
}

// A structured Day is "learned" when its required steps are done; a legacy Day only when it recorded evidence. Opening a Day is
// never learning it.
export const dayLearned = (d) => (d.structured ? Boolean(d.learning_complete) : Boolean(d.evidence_earned));

export function dayStatus(day, currentDay) {
  if (day.demonstrated) return "demonstrated";
  if (dayLearned(day)) return "learned";
  if (day.day === currentDay) return "current";
  return "upcoming";
}

export function nextDay(days) {
  return days.find((d) => !(dayLearned(d) || d.demonstrated)) || days[days.length - 1];
}

export function programProgress(days) {
  const total = days.length;
  const learned = days.filter((d) => dayLearned(d) || d.demonstrated).length;
  const demonstrated = days.filter((d) => d.demonstrated).length;
  return { total, learned, demonstrated, pct: total ? Math.round((learned / total) * 100) : 0 };
}
