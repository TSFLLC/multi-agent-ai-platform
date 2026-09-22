import { test } from "node:test";
import assert from "node:assert/strict";
import {
  EMPTY_TEXT,
  NOT_ENOUGH_HISTORY,
  choiceComparison,
  costBreakdown,
  costShort,
  evidenceStatusLabel,
  exclusionLabel,
  exclusionText,
  issueKindLabel,
  modeView,
  outcomeLabel,
  policyLabel,
  policyView,
  qualityView,
  reliabilityView,
  roleLabel,
  sectionState,
  strategyLabel,
  toggleErrorMessage,
  toggleEvidenceRouting,
  tokenLine,
  yesNo,
} from "../assets/js/modelIntelligence.js";

const INSTALLED_OFF = {
  installed: true,
  enabled: false,
  active_versions: 0,
  version: 1,
  strategy: "ma8.2-evidence-v1",
  history_window_days: 30,
  min_observations: 5,
  min_evaluated_runs: 3,
};
const INSTALLED_ON = { ...INSTALLED_OFF, enabled: true, active_versions: 1 };

// -- evidence policy OFF / ACTIVE ----------------------------------------------------------------------

test("policy OFF shows the settings and offers an explicit enable action", () => {
  const view = policyView(INSTALLED_OFF);
  assert.equal(view.state, "off");
  assert.equal(view.label, "OFF");
  assert.equal(view.actionLabel, "Enable evidence routing");
  assert.match(view.summary, /History is still recorded/);
  assert.deepEqual(view.facts, [
    ["Strategy", "Evidence routing v1 (policy v1)"],
    ["History window", "30 days"],
    ["Reliability minimum", "5 observed calls"],
    ["Quality minimum", "3 evaluated runs"],
  ]);
  assert.match(view.confirmText, /MANUAL choices are never changed/);
});

test("policy ACTIVE offers disable and promises history is kept", () => {
  const view = policyView(INSTALLED_ON);
  assert.equal(view.state, "active");
  assert.equal(view.label, "ACTIVE");
  assert.equal(view.actionLabel, "Disable evidence routing");
  assert.match(view.confirmText, /history and evidence are kept/);
});

test("a database without the policy installed offers no action", () => {
  const view = policyView({ installed: false, enabled: false, active_versions: 0 });
  assert.equal(view.state, "not_installed");
  assert.equal(view.actionLabel, null);
  assert.deepEqual(view.facts, []);
});

// -- enable / disable interaction ---------------------------------------------------------------------------

function fakeApi(response, { fail = null } = {}) {
  const calls = [];
  return {
    calls,
    post: async (path, body) => {
      calls.push({ path, body });
      if (fail) throw fail;
      return response;
    },
  };
}

test("the first click only asks for confirmation and never calls the API", async () => {
  const api = fakeApi(INSTALLED_ON);
  const result = await toggleEvidenceRouting({ api, current: INSTALLED_OFF, confirmed: false });
  assert.deepEqual(result, { ok: false, needsConfirm: true });
  assert.equal(api.calls.length, 0);
});

test("confirming enable posts enabled=true and returns the backend's status", async () => {
  const api = fakeApi(INSTALLED_ON);
  const result = await toggleEvidenceRouting({ api, current: INSTALLED_OFF, confirmed: true });
  assert.deepEqual(api.calls, [{ path: "/model-intelligence/evidence-policy", body: { enabled: true } }]);
  assert.equal(result.ok, true);
  assert.equal(result.status.enabled, true);
});

test("confirming disable posts enabled=false", async () => {
  const api = fakeApi(INSTALLED_OFF);
  const result = await toggleEvidenceRouting({ api, current: INSTALLED_ON, confirmed: true });
  assert.deepEqual(api.calls[0].body, { enabled: false });
  assert.equal(result.status.enabled, false);
});

test("a forbidden toggle explains who may change it; nothing is assumed changed", async () => {
  const err = Object.assign(new Error("Forbidden"), { status: 403 });
  const result = await toggleEvidenceRouting({ api: fakeApi(null, { fail: err }), current: INSTALLED_OFF, confirmed: true });
  assert.equal(result.ok, false);
  assert.equal(result.error, "Only an organization owner or admin can change evidence routing.");
  assert.equal(toggleErrorMessage({ status: 500, message: "boom" }), "boom");
});

test("toggling an uninstalled policy is refused locally", async () => {
  const api = fakeApi(null);
  const result = await toggleEvidenceRouting({ api, current: { installed: false }, confirmed: true });
  assert.equal(result.ok, false);
  assert.equal(api.calls.length, 0);
});

// -- loading / error / empty -----------------------------------------------------------------------------

test("section states: loading, error, empty and ready", () => {
  assert.equal(sectionState({ loading: true }).kind, "loading");
  assert.deepEqual(sectionState({ error: new Error("API down") }), { kind: "error", text: "API down" });
  assert.equal(sectionState({ items: [] }).kind, "empty");
  assert.equal(sectionState({ items: [{}] }).kind, "ready");
  assert.match(EMPTY_TEXT.roles, /No model calls/);
  assert.match(EMPTY_TEXT.decisions, /No routing decisions/);
});

// -- evidence: reliability vs quality, insufficient ---------------------------------------------------------

test("insufficient evidence says so instead of showing a rating", () => {
  const entry = { model_calls: 2, completed_calls: 2, provider_failures: 0, reliability: "insufficient", evaluated_runs: 1, quality: "insufficient", findings: { met: 1 } };
  assert.equal(reliabilityView(entry).standing, NOT_ENOUGH_HISTORY);
  assert.equal(qualityView(entry).standing, NOT_ENOUGH_HISTORY);
  assert.equal(qualityView({ evaluated_runs: 0 }).text, "No evaluated runs");
});

test("reliability and quality are shown as factual counts, never a score", () => {
  const entry = {
    model_calls: 12,
    completed_calls: 10,
    provider_failures: 2,
    reliability: "ok",
    evaluated_runs: 7,
    quality: "positive",
    findings: { met: 18, partial: 3, not_met: 1, not_applicable: 2 },
  };
  assert.deepEqual(reliabilityView(entry), { text: "Calls 12 · Completed 10 · Provider failures 2", standing: "No reliability concern" });
  const quality = qualityView(entry);
  assert.equal(quality.text, "Evaluated runs 7 · MET 18 · PARTIAL 3 · NOT MET 1 · N/A 2 (not counted)");
  assert.equal(quality.standing, "Mostly MET");
  assert.doesNotMatch(quality.text + reliabilityView(entry).text, /\d+\s*\/\s*100|score|%/i);
});

test("a provider failure is labelled a reliability issue, not a quality judgement", () => {
  assert.equal(issueKindLabel("reliability"), "Reliability issue — not a quality judgement");
  assert.equal(issueKindLabel("configuration"), "Configuration issue (credentials)");
  assert.equal(issueKindLabel("other"), "Failed call");
});

// -- cost / tokens ---------------------------------------------------------------------------------------

test("exact, estimated and unknown cost stay distinct and unknown is never $0", () => {
  const lines = costBreakdown({ exact_usd: "0.02", exact_calls: 1, estimated_usd: "0.03", estimated_calls: 2, unknown_calls: 1 });
  assert.deepEqual(
    lines.map((l) => l.kind),
    ["exact", "estimated", "unknown"]
  );
  assert.equal(lines[0].text, "$0.02 exact (1 call)");
  assert.equal(lines[1].text, "$0.03 estimated (2 calls)");
  assert.equal(lines[2].text, "Unknown cost for 1 call");
  const unknownOnly = costShort({ exact_calls: 0, estimated_calls: 0, unknown_calls: 3 });
  assert.equal(unknownOnly, "Unknown cost for 3 calls");
  assert.doesNotMatch(unknownOnly, /\$0/);
  assert.equal(costShort({}), "No cost recorded");
});

test("tokens show total, input and output", () => {
  assert.equal(tokenLine({ tokens_total: 1500, tokens_in: 1000, tokens_out: 500 }), "1,500 total · 1,000 in · 500 out");
});

// -- decisions: manual vs auto, detail ---------------------------------------------------------------------

test("MANUAL is presented as the operator's choice, AUTO with its policy", () => {
  assert.deepEqual(modeView({ is_manual: true, selection_mode: "manual" }), { mode: "MANUAL", title: "MANUAL", note: "Operator selected this model." });
  const auto = modeView({ is_manual: false, selection_mode: "auto", requested_policy: "prefer_free" });
  assert.equal(auto.title, "AUTO · PREFER FREE");
  assert.equal(policyLabel("free_only"), "FREE ONLY");
  assert.equal(policyLabel("any"), "ANY");
  assert.equal(evidenceStatusLabel("applied", { isManual: true }), "Not applicable — operator choice");
});

test("an MA8.1-only decision has no deterministic-vs-evidence comparison", () => {
  const detail = { is_manual: false, selected_canonical_model_id: "a/free", evidence: { status: "disabled", profiles: [] } };
  assert.equal(choiceComparison(detail), null);
  assert.equal(evidenceStatusLabel("disabled"), "Evidence routing off — deterministic rules used");
  assert.equal(strategyLabel("ma8.1-deterministic-v1"), "Deterministic rules v1");
  assert.equal(choiceComparison({ is_manual: true, evidence: null }), null);
});

test("an evidence decision shows the deterministic and evidence choices and whether they differ", () => {
  const changed = choiceComparison({
    is_manual: false,
    selected_canonical_model_id: "bbb/good",
    evidence: {
      status: "applied",
      deterministic_pick_provider_model_id: "pm-a",
      deterministic_pick_canonical_model_id: "aaa/cheap",
      evidence_changed_selection: true,
    },
  });
  assert.deepEqual(changed, { deterministic: "aaa/cheap", selected: "bbb/good", changed: "YES", evidenceUsed: true });

  const same = choiceComparison({
    is_manual: false,
    selected_canonical_model_id: "aaa/cheap",
    evidence: {
      status: "insufficient",
      deterministic_pick_provider_model_id: "pm-a",
      deterministic_pick_canonical_model_id: "aaa/cheap",
      evidence_changed_selection: false,
    },
  });
  assert.equal(same.changed, "NO");
  assert.equal(same.evidenceUsed, false);
  assert.equal(yesNo(null), "—");
});

test("exclusions use the persisted reason codes with plain-language labels", () => {
  assert.equal(exclusionLabel("NOT_FREE"), "Not free (policy requires FREE)");
  assert.equal(exclusionLabel("PROVIDER_MODEL_UNAVAILABLE"), "Model unavailable at this provider");
  assert.equal(exclusionText({ exclusion_reasons: ["MODEL_INACTIVE", "PROVIDER_UNAVAILABLE"] }), "Model not active (MODEL_INACTIVE), Provider down (PROVIDER_UNAVAILABLE)");
  assert.equal(exclusionLabel("SOMETHING_NEW"), "SOMETHING_NEW");
});

test("outcome and role labels read naturally", () => {
  assert.equal(outcomeLabel({ outcome: "selected" }), "Model selected");
  assert.equal(outcomeLabel({ outcome: "failed", failure_code: "NO_ELIGIBLE_MODEL" }), "No model — No eligible model");
  assert.equal(roleLabel("software_engineer"), "Software Engineer");
  assert.equal(roleLabel("final_reviewer"), "Final Reviewer");
  assert.equal(roleLabel("custom-role name"), "Custom Role Name");
});
