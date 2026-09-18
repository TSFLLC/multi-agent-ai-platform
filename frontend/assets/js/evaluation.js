// Evaluation & Analysis — MA6 Slice 4A. Pure, DOM/network-free domain
// logic for the Evaluation & Analysis section (frontend/assets/js/pages/
// evaluationConfig.js renders it) -- unit-tested directly, exactly like
// frontend/assets/js/candidates.js supports frontend/assets/js/pages/
// ask.js. No score/percentage/rank/winner concept exists anywhere in this
// module on purpose (MA6 non-negotiable invariant) -- it only ever builds
// the exact request app.schemas.comparisons.ComparisonEvaluationRequest
// expects and summarizes app.schemas.comparisons.ComparisonEvaluationResponse's
// bounded per-candidate outcomes ("created"/"reused"/"skipped"/"failed"),
// never a comparative verdict.

export const EVALUATION_METHOD_DETERMINISTIC = "deterministic";
export const EVALUATION_METHOD_AGENT_EVALUATOR = "agent_evaluator";

// Frontend presentation states for MA6.4A (Section: "do not invent
// backend states"). RUNNING/COMPLETED/PARTIAL are 4B's polling-derived
// states and deliberately not produced here yet.
export const EVAL_PHASE_NOT_CONFIGURED = "NOT_CONFIGURED";
export const EVAL_PHASE_READY = "READY";
export const EVAL_PHASE_STARTING = "STARTING";

export function createEvaluationConfig() {
  return {
    method: EVALUATION_METHOD_DETERMINISTIC,
    // evaluationDefinitionId tracks which Evaluation Definition is picked
    // (drives which versions the Definition Version select offers) --
    // evaluationDefinitionVersionId is the one field the request actually
    // needs, same two-level shape as evaluatorAgentId/evaluatorAgentVersionId.
    evaluationDefinitionId: null,
    evaluationDefinitionVersionId: null,
    evaluatorAgentId: null,
    evaluatorAgentVersionId: null,
    evaluatorModelId: null,
  };
}

export function isAgentEvaluatorMethod(method) {
  return method === EVALUATION_METHOD_AGENT_EVALUATOR;
}

// -- selection helpers (mutate `config` in place and return it, same
// direct-mutation convention frontend/assets/js/pages/ask.js already uses
// for its own candidate objects, e.g. `candidate.agentId = ...`) --------
//
// Each "select a parent" helper resets only the ONE dependent field it
// owns, and never touches any other field -- Agent selection resets which
// Role is offered/chosen, but never the Evaluator Model; Evaluation
// Definition selection resets which Version is offered/chosen, but never
// touches the evaluator fields at all. This is the same independence
// candidates.js's own agentId/agentVersionId/modelId split already
// guarantees for Ask Agents (Section: "Preserve Agent != Model").

// MA6.4A UX refinement: a dependent id (Definition Version / Evaluator
// Role) is only ever auto-resolved when there is exactly one ACTIVE
// option -- an unambiguous default a normal-operation click-through never
// has to see. When more than one ACTIVE option exists, this deliberately
// returns null rather than silently guessing the first one: the operator
// must open Advanced Settings and choose, so ambiguity is surfaced, never
// hidden behind a guessed default. `currentId` (if still present among
// `activeOptions`) is preserved across an unrelated re-render (e.g.
// switching Evaluation Method must never reset an already-valid Definition
// Version choice).
export function resolveUnambiguousVersionId(activeOptions, currentId) {
  if (currentId && activeOptions.some((v) => v.id === currentId)) return currentId;
  return activeOptions.length === 1 ? activeOptions[0].id : null;
}

// `activeVersions`: the caller's already-filtered list of ACTIVE
// EvaluationDefinitionVersionRead rows for the chosen definition (e.g.
// via agentDirectory.js's activeVersionsOf({ versions }), which is
// generic enough to reuse for Evaluation Definition Versions too -- both
// shapes carry a plain `status`/`version` pair).
export function selectEvaluationDefinition(config, definitionId, activeVersions) {
  config.evaluationDefinitionId = definitionId;
  config.evaluationDefinitionVersionId = resolveUnambiguousVersionId(activeVersions, null);
  return config;
}

// `activeVersions`: the caller's already-filtered list of ACTIVE
// AgentVersion rows for the chosen evaluator Agent (agentDirectory.js's
// activeVersionsOf(entry)).
export function selectEvaluatorAgent(config, agentId, activeVersions) {
  config.evaluatorAgentId = agentId;
  config.evaluatorAgentVersionId = resolveUnambiguousVersionId(activeVersions, null);
  return config;
}

// Whether every selection POST /comparisons/{id}/evaluations requires is
// present for the given config (Section: MA6.4A "Evaluation method" --
// deterministic only needs the Evaluation Definition Version; agent_evaluator
// additionally requires the evaluator Agent Version + Model). Never true
// merely because defaults happen to be present -- every field here is one
// the operator had to actually choose (createEvaluationConfig() starts
// every id at null).
export function isAnalyzeReady(config) {
  if (!config || !config.evaluationDefinitionVersionId) return false;
  if (isAgentEvaluatorMethod(config.method)) {
    return Boolean(config.evaluatorAgentVersionId && config.evaluatorModelId);
  }
  return true;
}

// The frontend-only presentation phase (Section 5 of the Step 0
// investigation) -- STARTING only while the POST is actually in flight,
// never merely because a picker changed.
export function evaluationSectionPhase(config, { submitting }) {
  if (submitting) return EVAL_PHASE_STARTING;
  return isAnalyzeReady(config) ? EVAL_PHASE_READY : EVAL_PHASE_NOT_CONFIGURED;
}

// A precise, safe hint for why Analyze Results is still disabled -- `null`
// once every required selection is present. Distinguishes "nothing chosen
// yet" (pick it in the primary controls) from "ambiguous, needs a manual
// choice" (resolveUnambiguousVersionId above left it null on purpose) so
// whichever one is blocking Analyze Results is always explained, never
// silently defaulted or silently hidden (Section: MA6.4A UX refinement
// "expose the requirement through Advanced Settings").
// Keyed off exactly the same fields isAnalyzeReady gates on (never a
// separate, looser/stricter notion of "configured") so the two can never
// disagree -- describeMissingRequirement(config) is null if and only if
// isAnalyzeReady(config) is true.
export function describeMissingRequirement(config) {
  if (!config.evaluationDefinitionVersionId) {
    return config.evaluationDefinitionId
      ? "Multiple Definition Versions are available — choose one in Advanced Settings."
      : "Choose an Evaluation above.";
  }
  if (isAgentEvaluatorMethod(config.method)) {
    if (!config.evaluatorAgentVersionId) {
      return config.evaluatorAgentId
        ? "Multiple Evaluator Roles are available — choose one in Advanced Settings."
        : "Choose an Evaluator above.";
    }
    if (!config.evaluatorModelId) return "Choose a Model above.";
  }
  return null;
}

// Builds the exact POST /comparisons/{comparison_id}/evaluations body from
// the operator's selected configuration -- mirrors
// app.schemas.comparisons.ComparisonEvaluationRequest field-for-field.
// evaluator_model_policy_override uses the same {mode:"manual",
// manual_provider_model_id} shape frontend/assets/js/pages/ask.js already
// sends for a candidate's own model override -- never a new request
// shape. Evaluator fields are omitted entirely for method=deterministic
// (the backend ignores them for that method, but the request should stay
// honest about what was actually configured).
export function buildAnalyzeRequest(config) {
  const request = {
    evaluation_definition_version_id: config.evaluationDefinitionVersionId,
    method: config.method,
  };
  if (isAgentEvaluatorMethod(config.method)) {
    request.evaluator_agent_version_id = config.evaluatorAgentVersionId;
    request.evaluator_model_policy_override = { mode: "manual", manual_provider_model_id: config.evaluatorModelId };
  }
  return request;
}

// Turns POST .../evaluations's per-candidate outcomes
// (app.schemas.comparisons.ComparisonCandidateEvaluationResult) into a
// bounded, safe-to-display summary. "started" = created OR reused (an
// EvaluationRun now exists, whether freshly made or an idempotent retry
// reusing a prior one) -- skipped/failed are counted and their
// backend-supplied `reason` is carried through unmodified, never
// reworded/fabricated. No aggregate score or ranking is derived here.
export function summarizeAnalyzeResults(results) {
  const list = Array.isArray(results) ? results : [];
  const counts = { created: 0, reused: 0, skipped: 0, failed: 0 };
  for (const r of list) {
    if (r && Object.prototype.hasOwnProperty.call(counts, r.status)) {
      counts[r.status] += 1;
    }
  }
  const startedCount = counts.created + counts.reused;
  const headline =
    startedCount > 0
      ? `Evaluation started for ${startedCount} candidate${startedCount === 1 ? "" : "s"}.`
      : "No candidate was eligible for evaluation.";
  return { counts, startedCount, headline, details: list };
}
