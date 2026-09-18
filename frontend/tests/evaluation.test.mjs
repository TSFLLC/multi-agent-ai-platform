import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { activeVersionsOf } from "../assets/js/agentDirectory.js";
import {
  EVALUATION_METHOD_AGENT_EVALUATOR,
  EVALUATION_METHOD_DETERMINISTIC,
  EVAL_PHASE_NOT_CONFIGURED,
  EVAL_PHASE_READY,
  EVAL_PHASE_STARTING,
  buildAnalyzeRequest,
  createEvaluationConfig,
  describeMissingRequirement,
  evaluationSectionPhase,
  isAgentEvaluatorMethod,
  isAnalyzeReady,
  resolveUnambiguousVersionId,
  selectEvaluationDefinition,
  selectEvaluatorAgent,
  summarizeAnalyzeResults,
} from "../assets/js/evaluation.js";

// buildEvaluationSection/loadEvaluationDefinitionCatalog (frontend/assets/js/
// pages/evaluationConfig.js) do real DOM building and real fetches -- this
// project deliberately adds no jsdom dependency (see frontend/tests/
// modal.test.mjs's own note), so that module's DOM wiring is exercised
// live per the MA6.4A UAT instructions, exactly like ask.js/comparisonDetail.js
// are today. This file covers the pure request/gating/summary logic in
// frontend/assets/js/evaluation.js, plus source-level regression guards for
// the two hard invariants (no /select-winner call, no score/rank/winner
// surface) that a pure-logic test can still verify precisely.

// -- config defaults / gating -----------------------------------------------

test("a fresh config defaults to deterministic with every selection unset", () => {
  const config = createEvaluationConfig();
  assert.equal(config.method, EVALUATION_METHOD_DETERMINISTIC);
  assert.equal(config.evaluationDefinitionVersionId, null);
  assert.equal(config.evaluatorAgentVersionId, null);
  assert.equal(config.evaluatorModelId, null);
});

test("isAgentEvaluatorMethod distinguishes the two backend methods exactly", () => {
  assert.equal(isAgentEvaluatorMethod(EVALUATION_METHOD_AGENT_EVALUATOR), true);
  assert.equal(isAgentEvaluatorMethod(EVALUATION_METHOD_DETERMINISTIC), false);
  assert.equal(isAgentEvaluatorMethod("something_else"), false);
});

test("deterministic is ready as soon as an Evaluation Definition Version is chosen", () => {
  const config = createEvaluationConfig();
  assert.equal(isAnalyzeReady(config), false);
  config.evaluationDefinitionVersionId = "v1";
  assert.equal(isAnalyzeReady(config), true);
});

test("agent_evaluator cannot Analyze until Definition Version + evaluator Agent Version + evaluator Model all exist", () => {
  const config = createEvaluationConfig();
  config.method = EVALUATION_METHOD_AGENT_EVALUATOR;
  config.evaluationDefinitionVersionId = "v1";
  assert.equal(isAnalyzeReady(config), false, "missing evaluator Agent Version + Model");

  config.evaluatorAgentVersionId = "av1";
  assert.equal(isAnalyzeReady(config), false, "still missing evaluator Model");

  config.evaluatorModelId = "m1";
  assert.equal(isAnalyzeReady(config), true, "every required selection now present");
});

test("agent_evaluator without a Definition Version is never ready even with evaluator fields set", () => {
  const config = createEvaluationConfig();
  config.method = EVALUATION_METHOD_AGENT_EVALUATOR;
  config.evaluatorAgentVersionId = "av1";
  config.evaluatorModelId = "m1";
  assert.equal(isAnalyzeReady(config), false);
});

// -- presentation phase (Section 5 of the Step 0 investigation) -------------

test("phase is NOT_CONFIGURED while required selections are missing", () => {
  const config = createEvaluationConfig();
  assert.equal(evaluationSectionPhase(config, { submitting: false }), EVAL_PHASE_NOT_CONFIGURED);
});

test("phase is READY once configured and not submitting", () => {
  const config = createEvaluationConfig();
  config.evaluationDefinitionVersionId = "v1";
  assert.equal(evaluationSectionPhase(config, { submitting: false }), EVAL_PHASE_READY);
});

test("phase is STARTING only while the request is actually in flight", () => {
  const config = createEvaluationConfig();
  config.evaluationDefinitionVersionId = "v1";
  assert.equal(evaluationSectionPhase(config, { submitting: true }), EVAL_PHASE_STARTING);
});

test("STARTING never appears merely because the config isn't ready yet -- submitting false always yields NOT_CONFIGURED/READY", () => {
  const config = createEvaluationConfig();
  assert.notEqual(evaluationSectionPhase(config, { submitting: false }), EVAL_PHASE_STARTING);
});

// -- Agent selection controls Role versions (independence from Model) -------

test("selecting an Evaluation Definition auto-resolves its Version only when exactly one ACTIVE version exists (unambiguous default), never touching evaluator fields", () => {
  const config = createEvaluationConfig();
  config.evaluatorModelId = "m-untouched";
  const versions = [
    { id: "dv-draft", version: 1, status: "draft" },
    { id: "dv-active-1", version: 2, status: "active" },
  ];
  selectEvaluationDefinition(config, "def-1", activeVersionsOf({ versions }));
  assert.equal(config.evaluationDefinitionId, "def-1");
  assert.equal(config.evaluationDefinitionVersionId, "dv-active-1", "the one unambiguous ACTIVE version, not the draft");
  assert.equal(config.evaluatorModelId, "m-untouched", "evaluator fields are a separate concern");
});

test("selecting an Evaluation Definition with MULTIPLE ACTIVE versions leaves the Version unset -- never silently guesses (MA6.4A: expose via Advanced Settings)", () => {
  const config = createEvaluationConfig();
  const versions = [
    { id: "dv-active-1", version: 1, status: "active" },
    { id: "dv-active-2", version: 2, status: "active" },
  ];
  selectEvaluationDefinition(config, "def-1", activeVersionsOf({ versions }));
  assert.equal(config.evaluationDefinitionId, "def-1");
  assert.equal(config.evaluationDefinitionVersionId, null, "ambiguous -- the operator must choose in Advanced Settings");
});

test("a definition with no ACTIVE version resets its Version selection to null (never a draft/retired id)", () => {
  const config = createEvaluationConfig();
  const versions = [{ id: "dv-draft", version: 1, status: "draft" }];
  selectEvaluationDefinition(config, "def-2", activeVersionsOf({ versions }));
  assert.equal(config.evaluationDefinitionVersionId, null);
});

test("selecting an evaluator Agent auto-resolves its Role (AgentVersion) only when exactly one ACTIVE version exists, never touching the Model", () => {
  const config = createEvaluationConfig();
  config.evaluatorModelId = "m-keep";
  const versions = [
    { id: "av-draft", version: 1, status: "draft", role: "reviewer" },
    { id: "av-active", version: 2, status: "active", role: "senior-reviewer" },
  ];
  selectEvaluatorAgent(config, "agent-1", activeVersionsOf({ versions }));
  assert.equal(config.evaluatorAgentId, "agent-1");
  assert.equal(config.evaluatorAgentVersionId, "av-active");
  assert.equal(config.evaluatorModelId, "m-keep", "Agent selection must never reset the Model -- Agent != Model");
});

test("selecting an evaluator Agent with MULTIPLE ACTIVE Roles leaves the Role unset -- never silently guesses", () => {
  const config = createEvaluationConfig();
  const versions = [
    { id: "av-active-1", version: 1, status: "active", role: "reviewer" },
    { id: "av-active-2", version: 2, status: "active", role: "senior-reviewer" },
  ];
  selectEvaluatorAgent(config, "agent-1", activeVersionsOf({ versions }));
  assert.equal(config.evaluatorAgentVersionId, null, "ambiguous -- the operator must choose in Advanced Settings");
});

// -- resolveUnambiguousVersionId (the shared default-resolution rule) -------

test("resolveUnambiguousVersionId auto-resolves only when exactly one option exists", () => {
  assert.equal(resolveUnambiguousVersionId([{ id: "only" }], null), "only");
  assert.equal(resolveUnambiguousVersionId([{ id: "a" }, { id: "b" }], null), null, "ambiguous -- never guessed");
  assert.equal(resolveUnambiguousVersionId([], null), null, "no options at all");
});

test("resolveUnambiguousVersionId preserves an already-valid current selection across an unrelated re-render", () => {
  const options = [{ id: "a" }, { id: "b" }];
  assert.equal(resolveUnambiguousVersionId(options, "b"), "b", "still present among the options -- keep it");
});

test("resolveUnambiguousVersionId drops a current selection that is no longer among the options", () => {
  const options = [{ id: "a" }, { id: "b" }];
  assert.equal(resolveUnambiguousVersionId(options, "stale-id"), null, "not ambiguous by accident -- genuinely gone");
});

// -- describeMissingRequirement (safe, precise hint for a disabled button) --

test("describeMissingRequirement guides to the primary controls when nothing is chosen yet", () => {
  assert.equal(describeMissingRequirement(createEvaluationConfig()), "Choose an Evaluation above.");
});

test("describeMissingRequirement distinguishes an unresolved ambiguous Definition Version from nothing chosen", () => {
  const config = createEvaluationConfig();
  config.evaluationDefinitionId = "def-1"; // chosen, but multiple ACTIVE versions left it unresolved
  assert.equal(
    describeMissingRequirement(config),
    "Multiple Definition Versions are available — choose one in Advanced Settings."
  );
});

test("describeMissingRequirement walks through every agent_evaluator requirement in order", () => {
  const config = createEvaluationConfig();
  config.method = EVALUATION_METHOD_AGENT_EVALUATOR;
  config.evaluationDefinitionId = "def-1";
  config.evaluationDefinitionVersionId = "def-v1";
  assert.equal(describeMissingRequirement(config), "Choose an Evaluator above.");

  config.evaluatorAgentId = "agent-1";
  assert.equal(
    describeMissingRequirement(config),
    "Multiple Evaluator Roles are available — choose one in Advanced Settings."
  );

  config.evaluatorAgentVersionId = "av-1";
  assert.equal(describeMissingRequirement(config), "Choose a Model above.");

  config.evaluatorModelId = "m-1";
  assert.equal(describeMissingRequirement(config), null, "fully configured -- nothing left to describe");
});

test("describeMissingRequirement is null exactly when isAnalyzeReady is true", () => {
  const deterministicReady = createEvaluationConfig();
  deterministicReady.evaluationDefinitionVersionId = "v1";
  assert.equal(describeMissingRequirement(deterministicReady), null);
  assert.equal(isAnalyzeReady(deterministicReady), true);
});

test("the same evaluator Model id survives switching between two different evaluator Agent/Role combinations", () => {
  const config = createEvaluationConfig();
  config.evaluatorModelId = "shared-model";

  selectEvaluatorAgent(config, "agent-a", activeVersionsOf({ versions: [{ id: "va", version: 1, status: "active" }] }));
  assert.equal(config.evaluatorModelId, "shared-model");

  selectEvaluatorAgent(config, "agent-b", activeVersionsOf({ versions: [{ id: "vb", version: 1, status: "active" }] }));
  assert.equal(config.evaluatorModelId, "shared-model", "same model remains usable across different evaluator Agents/Roles");
});

// -- multiple ACTIVE versions (definitions AND agent versions) --------------

test("activeVersionsOf (reused for Evaluation Definition Versions) supports multiple simultaneously-ACTIVE versions", () => {
  const versions = [
    { id: "v1", version: 1, status: "active" },
    { id: "v2", version: 2, status: "active" },
    { id: "v3", version: 3, status: "deprecated" },
  ];
  const active = activeVersionsOf({ versions });
  assert.deepEqual(active.map((v) => v.id), ["v1", "v2"], "both ACTIVE versions offered, sorted; deprecated excluded");
});

test("a definition with zero ACTIVE versions yields an empty active list (drives the empty-definition-state / exclusion from the picker)", () => {
  const versions = [
    { id: "v1", version: 1, status: "draft" },
    { id: "v2", version: 2, status: "retired" },
  ];
  assert.deepEqual(activeVersionsOf({ versions }), []);
});

// -- request building --------------------------------------------------------

test("buildAnalyzeRequest for deterministic sends only the Evaluation Definition Version + method, no evaluator fields", () => {
  const config = createEvaluationConfig();
  config.evaluationDefinitionVersionId = "def-v1";
  config.evaluatorAgentVersionId = "should-be-ignored";
  config.evaluatorModelId = "should-be-ignored-too";

  const request = buildAnalyzeRequest(config);
  assert.deepEqual(request, {
    evaluation_definition_version_id: "def-v1",
    method: EVALUATION_METHOD_DETERMINISTIC,
  });
  assert.equal("evaluator_agent_version_id" in request, false);
  assert.equal("evaluator_model_policy_override" in request, false);
});

test("buildAnalyzeRequest for agent_evaluator includes the evaluator Agent Version and a manual model policy override", () => {
  const config = createEvaluationConfig();
  config.method = EVALUATION_METHOD_AGENT_EVALUATOR;
  config.evaluationDefinitionVersionId = "def-v1";
  config.evaluatorAgentVersionId = "eval-av-1";
  config.evaluatorModelId = "provider-model-1";

  const request = buildAnalyzeRequest(config);
  assert.deepEqual(request, {
    evaluation_definition_version_id: "def-v1",
    method: EVALUATION_METHOD_AGENT_EVALUATOR,
    evaluator_agent_version_id: "eval-av-1",
    evaluator_model_policy_override: { mode: "manual", manual_provider_model_id: "provider-model-1" },
  });
});

// -- POST outcome summarizing (created/reused/skipped/failed) ---------------

test("summarizeAnalyzeResults counts every outcome status and produces a bounded headline", () => {
  const summary = summarizeAnalyzeResults([
    { comparison_candidate_id: "c1", status: "created", evaluation_run_id: "r1" },
    { comparison_candidate_id: "c2", status: "created", evaluation_run_id: "r2" },
    { comparison_candidate_id: "c3", status: "reused", evaluation_run_id: "r3" },
    { comparison_candidate_id: "c4", status: "skipped", reason: "no valid output artifact" },
    { comparison_candidate_id: "c5", status: "failed", reason: "evaluation definition version not published" },
  ]);
  assert.deepEqual(summary.counts, { created: 2, reused: 1, skipped: 1, failed: 1 });
  assert.equal(summary.startedCount, 3, "created + reused, not skipped/failed");
  assert.equal(summary.headline, "Evaluation started for 3 candidates.");
  assert.equal(summary.details.length, 5, "the raw per-candidate outcomes are preserved, never collapsed");
});

test("summarizeAnalyzeResults handles a singular count in its headline", () => {
  const summary = summarizeAnalyzeResults([{ comparison_candidate_id: "c1", status: "created" }]);
  assert.equal(summary.headline, "Evaluation started for 1 candidate.");
});

test("summarizeAnalyzeResults never fabricates a started count when nothing was eligible", () => {
  const summary = summarizeAnalyzeResults([
    { comparison_candidate_id: "c1", status: "skipped", reason: "not launched" },
    { comparison_candidate_id: "c2", status: "skipped", reason: "no valid output artifact" },
  ]);
  assert.equal(summary.startedCount, 0);
  assert.equal(summary.headline, "No candidate was eligible for evaluation.");
  assert.equal(summary.counts.skipped, 2);
});

test("summarizeAnalyzeResults carries the backend-supplied safe reason through unmodified, never rewording it", () => {
  const summary = summarizeAnalyzeResults([
    { comparison_candidate_id: "c1", status: "failed", reason: "Evaluator Agent Version is not published (status='draft')." },
  ]);
  assert.equal(summary.details[0].reason, "Evaluator Agent Version is not published (status='draft').");
});

test("summarizeAnalyzeResults tolerates a missing/malformed results list without throwing", () => {
  assert.deepEqual(summarizeAnalyzeResults(undefined).counts, { created: 0, reused: 0, skipped: 0, failed: 0 });
  assert.deepEqual(summarizeAnalyzeResults(null).counts, { created: 0, reused: 0, skipped: 0, failed: 0 });
});

// -- Section 13 regression guard: no score/rank/winner surface in the
// summarized DATA that actually reaches the UI (never merely absent by
// coincidence -- explicitly asserted against every field name). ------------

test("summarizeAnalyzeResults's output shape carries no score/percentage/rank/winner/recommendation field", () => {
  const summary = summarizeAnalyzeResults([{ comparison_candidate_id: "c1", status: "created" }]);
  const forbidden = ["score", "percent", "rank", "winner", "recommend", "grade", "best"];
  const keysAndHeadline = JSON.stringify(summary).toLowerCase();
  for (const term of forbidden) {
    assert.equal(keysAndHeadline.includes(term), false, `summarizeAnalyzeResults output must not contain "${term}"`);
  }
});

// -- Section 12/13 source-level regression guards ----------------------------
// Read the actual shipped module source (not a copy) to prove two hard
// invariants that are otherwise DOM-only concerns this pure-logic test file
// can still check precisely: the evaluation flow never references the
// canonical-selection endpoint, and no comparative-ranking UI label exists
// anywhere in either module's rendered/user-facing strings.

function readSource(relativePath) {
  const url = new URL(relativePath, import.meta.url);
  return readFileSync(fileURLToPath(url), "utf8");
}

function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

test("the evaluation flow never calls POST .../select-winner from its actual code (comments may explain the invariant by name)", () => {
  const evaluationCode = stripComments(readSource("../assets/js/evaluation.js"));
  const sectionCode = stripComments(readSource("../assets/js/pages/evaluationConfig.js"));
  assert.equal(evaluationCode.includes("select-winner"), false);
  assert.equal(sectionCode.includes("select-winner"), false);
});

test("neither evaluation module renders a score, percentage, rank, or winner-badge UI label", () => {
  // Scoped to actual UI-facing string literals this module builds (button/
  // label/option text), not doc-comments that legitimately explain the
  // invariant itself (which reference these same words to say they're
  // absent) -- comments are stripped first so those don't false-positive.
  const withoutComments = stripComments(readSource("../assets/js/pages/evaluationConfig.js"));
  const forbidden = ["score", "percentage", "ranking", "winner", "recommend", "best answer", "%"];
  for (const term of forbidden) {
    assert.equal(
      withoutComments.toLowerCase().includes(term),
      false,
      `evaluationConfig.js's rendered UI must not contain "${term}"`
    );
  }
});
