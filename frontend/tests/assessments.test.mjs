import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CENTER_SECTIONS, assessmentModeBanner, attemptStatusText, buildDraft, centerModel, confidenceLabel, costLabel, declarationNote,
  effectLabel, emptyCenterText, freshReasonText, helpLabel, isTruthfulText, kindLabel, outcomeInfo, readinessModel,
  recordFileName, recordModel, recordStatusText, remediationLink, resultModel, reviewModel, reviewPayload, splitIds,
  timeLeftText, validateSubmit,
} from "../assets/js/assessments.js";

test("outcomes use beginner-friendly, honest wording", () => {
  assert.equal(outcomeInfo("needs_work").label, "Needs more work");
  assert.equal(outcomeInfo("provisional").label, "Not enough confidence to decide");
  assert.match(outcomeInfo("provisional").plain, /Nothing changed/);
  assert.match(outcomeInfo("human_review_required").plain, /person needs to review/);
  for (const key of ["passed", "needs_work", "provisional", "human_review_required", "unable_to_assess"]) {
    assert.ok(isTruthfulText(outcomeInfo(key).label + " " + outcomeInfo(key).plain), key);
  }
  assert.equal(outcomeInfo("nope").label, "In progress");
});

test("demonstration effects never overclaim", () => {
  assert.match(effectLabel("counts_toward_practiced_only"), /not yet as demonstration/);
  assert.match(effectLabel("formative_only"), /not recorded as evidence/);
  assert.equal(effectLabel("something else"), "Nothing was recorded");
  assert.equal(isTruthfulText("You are certified."), false);
  assert.equal(isTruthfulText("An expert badge."), false);
  assert.equal(isTruthfulText("Not a certificate or credential."), true);
});

test("labels for confidence, cost provenance and help levels", () => {
  assert.equal(confidenceLabel("low"), "Low confidence");
  assert.equal(costLabel("exact"), "exact cost");
  assert.equal(costLabel("unknown"), "cost unknown");
  assert.equal(costLabel(undefined), "cost unknown"); // never fabricated
  assert.equal(helpLabel("h5"), "after seeing the solution (H5)");
  assert.equal(helpLabel(null), "with no help recorded");
  assert.equal(kindLabel("capstone"), "Capstone assessment");
});

test("Assessment Center groups work into the required sections and shows an empty state", () => {
  assert.deepEqual(CENTER_SECTIONS.map((s) => s[0]), [
    "ready_for_assessment", "in_progress", "needs_work", "provisional_or_review", "demonstrated", "not_yet_ready", "history",
  ]);
  const empty = centerModel({ ready_for_assessment: [], history: [] });
  assert.equal(empty.empty, true);
  assert.match(emptyCenterText, /Nothing is ready yet/);
  const model = centerModel({ ready_for_assessment: [{ title: "x" }], history: [{ title: "y" }], needs_work: [] });
  assert.deepEqual(model.visible.map((s) => s.key), ["ready_for_assessment", "history"]);
  assert.equal(centerModel(null).empty, true);
});

test("Assessment Mode banner explains the rules without surveillance and only while in progress", () => {
  const attempt = { status: "draft", definition: { allowed_resources: ["Your lessons"] } };
  const banner = assessmentModeBanner(attempt);
  assert.equal(banner.title, "ASSESSMENT MODE");
  const text = banner.lines.join(" ");
  assert.match(text, /Mentor and Professor are paused/);
  assert.match(text, /Mentor history is safe/);
  assert.match(text, /Not allowed: AI assistants/);
  assert.match(text, /no camera, screen, keyboard or clipboard/i);
  assert.equal(assessmentModeBanner({ status: "finalized", definition: {} }), null);
  assert.equal(assessmentModeBanner(null), null);
  assert.match(attemptStatusText({ status: "awaiting_grading" }), /safe to retry/);
});

test("readiness model lists unmet items, the resume target and the fresh-challenge reason", () => {
  const model = readinessModel({
    ready: false, fresh_required: true, fresh_reason: "source_work_substantially_assisted",
    source_work: { levels: ["h5"] },
    checks: [{ key: "a", met: true }, { key: "no_active_attempt", met: false, resume_attempt_id: "att-1", label: "x" }],
  });
  assert.equal(model.ready, false);
  assert.equal(model.unmet.length, 1);
  assert.equal(model.resumeAttemptId, "att-1");
  assert.equal(model.fresh.required, true);
  assert.match(model.fresh.why, /source project alone cannot show demonstration/);
  assert.deepEqual(model.sourceWork, ["h5"]);
  assert.equal(readinessModel(null).ready, false);
  assert.equal(freshReasonText("nope"), "");
});

test("drafts are built from what the learner typed: selections as numbers, ids split, reflection kept apart", () => {
  const challenge = { items: [
    { entry_key: "q1", options: ["a", "b"], multiple: false },
    { entry_key: "p1", prompt_md: "Explain" },
    { entry_key: "v1", statement_md: "Run it" },
  ] };
  const fields = [
    { path: "run_ids", input: "ids" }, { path: "experiment_id", input: "id" }, { path: "fields.readme", input: "long_text" }, { path: "fields.claim", input: "text" },
  ];
  const draft = buildDraft(challenge, fields, {
    items: { q1: ["1"], p1: "in my words", v1: undefined },
    fields: { run_ids: "r1, r2  r3;r4", experiment_id: " e-9 ", "fields.readme": "## Problem", "fields.claim": "" },
    reflection: "hard part",
  });
  assert.deepEqual(draft, {
    responses: { q1: { selected: [1] }, p1: { text: "in my words" } },
    run_ids: ["r1", "r2", "r3", "r4"], experiment_id: "e-9",
    fields: { readme: "## Problem", reflection: "hard part" },
  });
  assert.deepEqual(splitIds(""), []);
  assert.deepEqual(buildDraft(null, null, { items: {}, fields: {}, reflection: "  " }), {});
});

test("a declaration is required to submit and using an AI assistant is treated honestly", () => {
  assert.equal(validateSubmit({ declaration: "" }).ok, false);
  assert.match(validateSubmit({}).message, /declaration/);
  assert.equal(validateSubmit({ declaration: "used_docs" }).ok, true);
  assert.match(declarationNote("used_ai_assistant"), /feedback only/);
  assert.match(declarationNote("no_external_help"), /self-reported/);
});

test("time left uses only the server-side expiry", () => {
  const now = Date.parse("2026-09-25T10:00:00Z");
  assert.match(timeLeftText({ expires_at: "2026-09-25T12:30:00Z" }, now), /About 2h 30m left/);
  assert.match(timeLeftText({ expires_at: "2026-09-25T12:30:00" }, now), /About 2h 30m left/); // naive = UTC
  assert.equal(timeLeftText({ expires_at: "2026-09-25T09:00:00Z" }, now), "This attempt has expired.");
  assert.equal(timeLeftText({}, now), "");
});

const VIEW = (overrides = {}) => ({
  status: "finalized",
  attempt: { project_attempt_id: "pa-1", definition: { definition_key: "kc-x" } },
  history: [{ kind: "deterministic" }, { kind: "final" }],
  reviews: [],
  result: {
    id: "res-1", outcome: "needs_work", demonstration_effect: "none", has_record: false,
    gaps: [{ criterion_key: "k", label: "Answers", source: "platform", finding: "not_met", detail: "" }],
    remediation: [{ kind: "learning_item", label: "Revisit" }, { kind: "retry", label: "Try again" }],
    report: {
      headline: "This needs more work.",
      platform_fact: { deterministic_checks: [], assistance: { source_levels: [] }, independence: {} },
      grader_judgment: { ran: false, criteria: [], runs: [] },
      learner_reflection: { reflection: null, earlier_explain_back: [] },
      professor_coaching: { label: "PROFESSOR COACHING", note: "Coaching is separate." },
      answers: { learner_state: { c1: { concept_name: "X", before: "exposed", after: "exposed", changed: false, overlays: [] } }, review_later: {} },
    },
    ...overrides,
  },
});

test("result model separates fact, judgment, reflection and coaching and links the Professor by result id", () => {
  const model = resultModel(VIEW());
  assert.equal(model.finalized, true);
  assert.equal(model.info.label, "Needs more work");
  assert.ok(model.fact && model.judgment && model.reflection && model.coaching);
  assert.equal(model.coachingHref, "#/professor?intent=HELP_ME_AFTER_ASSESSMENT&target_type=assessment_result&target_id=res-1");
  assert.equal(model.stateChanges[0].changed, false);
  assert.equal(model.hasRecord, false);
});

test("a pending attempt keeps its checks and offers a safe retry", () => {
  const pending = resultModel({ status: "awaiting_grading", pending: { message: "safe", deterministic_checks: [] } });
  assert.equal(pending.finalized, false);
  assert.equal(pending.canRetryGrading, true);
  assert.equal(resultModel({ status: "checking", pending: null }).canRetryGrading, false);
});

test("remediation links route to the right place and never dead-end", () => {
  assert.equal(remediationLink({ kind: "milestone" }, { projectAttemptId: "pa 1" }), "#/academy/projects/attempts/pa%201");
  assert.equal(remediationLink({ kind: "milestone" }, {}), "#/academy/projects");
  assert.equal(remediationLink({ kind: "definition", ref: "kc/x" }), "#/academy/assessments/definitions/kc%2Fx");
  assert.equal(remediationLink({ kind: "experiment" }), "#/ail/lab");
  assert.equal(remediationLink({ kind: "professor" }), "#/professor");
  assert.equal(remediationLink({ kind: "retry" }, { definitionKey: "kc-x" }), "#/academy/assessments/definitions/kc-x");
  assert.equal(remediationLink({ kind: "retry" }), "#/academy/assessments");
  assert.equal(remediationLink({ kind: "learning_item" }), "#/academy");
});

test("review request needs a reason and explicit consent; state of an open review is explained", () => {
  assert.equal(reviewPayload({ reason: "short", consent: true }).ok, false);
  assert.match(reviewPayload({ reason: "a good enough reason", consent: false }).message, /explicit consent/);
  assert.deepEqual(reviewPayload({ reason: "  a good enough reason ", consent: true }), { ok: true, body: { reason: "a good enough reason", consent: true } });
  const open = reviewModel([{ id: "r", status: "open", consented: false }]);
  assert.equal(open.needsConsent, true);
  assert.equal(open.canRequest, false);
  assert.match(open.summary, /waiting for your consent/);
  assert.equal(reviewModel([{ id: "r", status: "resolved", decision: "confirm" }]).canRequest, true);
});

test("Demonstration Record model states what was verified vs self-reported and is not a certificate", () => {
  const payload = {
    record_hash: "abcdef1234567890",
    status: { status: "changed_since", reasons: ["X changed"] },
    record: {
      title: "Demonstration Record", notice: "Not a certificate or credential.",
      assessment: { kind: "knowledge_check", definition_key: "kc-x", definition_version: 1 },
      concepts: [{ concept_name: "Structured Output", concept_version: 2 }], project: null,
      evidence: [{ evidence_type: "knowledge_check", assistance_level: "h0", execution_verification: null, grader: "deterministic" }],
      provenance: { verified_by_platform: ["Answers"], self_reported: ["Your declaration is self-reported."] },
      grader: { ran: false },
    },
  };
  const model = recordModel(payload);
  assert.equal(model.title, "Demonstration Record");
  assert.match(model.notice, /Not a certificate/);
  assert.equal(model.status, "The Concept changed since — consider reassessing");
  assert.match(model.concepts[0], /Structured Output \(Concept Version 2\)/);
  assert.match(model.evidence[0], /help level H0/);
  assert.deepEqual(model.verified, ["Answers"]);
  assert.equal(model.grader, "No AI judgment was used.");
  assert.equal(recordFileName(payload, "md"), "demonstration-record-kc-x-abcdef12.md");
  assert.equal(recordStatusText("superseded_by_review"), "Replaced by a later review decision (history is kept)");
  for (const text of [model.title, model.notice, model.status, ...model.verified, ...model.selfReported]) assert.ok(isTruthfulText(text));
});
