import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createCandidate,
  isCandidateComplete,
  validCandidates,
  duplicateLocalIds,
  runnableCandidates,
  canRunCandidateComparison,
  countCandidatesUsingModel,
} from "../assets/js/candidates.js";

test("a candidate missing agent, role, or model is incomplete", () => {
  assert.equal(isCandidateComplete(createCandidate({ localId: "c1" })), false);
  assert.equal(isCandidateComplete(createCandidate({ localId: "c1", agentId: "a1" })), false);
  assert.equal(isCandidateComplete(createCandidate({ localId: "c1", agentId: "a1", agentVersionId: "v1" })), false);
  assert.equal(
    isCandidateComplete(createCandidate({ localId: "c1", agentId: "a1", agentVersionId: "v1", modelId: "m1" })),
    true
  );
});

// -- Section 1/4: candidate identity is never model identity ----------------

test("the same model can be assigned to multiple candidates simultaneously", () => {
  const candidates = [
    createCandidate({ localId: "c1", agentId: "a1", agentVersionId: "v1", modelId: "MODEL_A" }),
    createCandidate({ localId: "c2", agentId: "a2", agentVersionId: "v2", modelId: "MODEL_A" }),
    createCandidate({ localId: "c3", agentId: "a3", agentVersionId: "v3", modelId: "MODEL_A" }),
  ];
  assert.equal(validCandidates(candidates).length, 3);
  assert.equal(duplicateLocalIds(candidates).size, 0, "different Agent Versions must never be flagged as duplicates");
  assert.equal(runnableCandidates(candidates).length, 3);
});

test("same model + different Agent is valid", () => {
  const candidates = [
    createCandidate({ localId: "c1", agentId: "agent-1", agentVersionId: "v1", modelId: "MODEL_A" }),
    createCandidate({ localId: "c2", agentId: "agent-2", agentVersionId: "v2", modelId: "MODEL_A" }),
  ];
  assert.equal(duplicateLocalIds(candidates).size, 0);
});

test("same model + same Agent but different Role (a different AgentVersion) is valid", () => {
  // Role == AgentVersion (see agentDirectory.js) -- two versions of the
  // SAME Agent are two different roles, even though agentId matches.
  const candidates = [
    createCandidate({ localId: "c1", agentId: "agent-1", agentVersionId: "v1-developer", modelId: "MODEL_A" }),
    createCandidate({ localId: "c2", agentId: "agent-1", agentVersionId: "v2-reviewer", modelId: "MODEL_A" }),
  ];
  assert.equal(duplicateLocalIds(candidates).size, 0);
});

test("different model + same Agent (same Role) is valid", () => {
  const candidates = [
    createCandidate({ localId: "c1", agentId: "agent-1", agentVersionId: "v1", modelId: "MODEL_A" }),
    createCandidate({ localId: "c2", agentId: "agent-1", agentVersionId: "v1", modelId: "MODEL_B" }),
  ];
  assert.equal(duplicateLocalIds(candidates).size, 0);
});

// -- Exact duplicate handling -------------------------------------------------

test("an exact duplicate (same Agent + same Role + same Model) is flagged and excluded from what would run", () => {
  const candidates = [
    createCandidate({ localId: "c1", agentId: "agent-1", agentVersionId: "v1", modelId: "MODEL_A" }),
    createCandidate({ localId: "c2", agentId: "agent-1", agentVersionId: "v1", modelId: "MODEL_A" }), // exact repeat
    createCandidate({ localId: "c3", agentId: "agent-2", agentVersionId: "v2", modelId: "MODEL_A" }),
  ];
  const dups = duplicateLocalIds(candidates);
  assert.deepEqual([...dups], ["c2"]); // the SECOND occurrence is flagged, not the first
  assert.deepEqual(
    runnableCandidates(candidates).map((c) => c.localId),
    ["c1", "c3"]
  );
});

test("an incomplete candidate is never flagged as a duplicate of another incomplete one", () => {
  const candidates = [
    createCandidate({ localId: "c1", agentId: "agent-1", agentVersionId: null, modelId: null }),
    createCandidate({ localId: "c2", agentId: "agent-1", agentVersionId: null, modelId: null }),
  ];
  assert.equal(duplicateLocalIds(candidates).size, 0);
});

// -- Minimum-2 validation, add/remove ----------------------------------------

test("canRunCandidateComparison requires a question and at least 2 runnable candidates", () => {
  const two = [
    createCandidate({ localId: "c1", agentId: "a1", agentVersionId: "v1", modelId: "MODEL_A" }),
    createCandidate({ localId: "c2", agentId: "a2", agentVersionId: "v2", modelId: "MODEL_A" }),
  ];
  assert.equal(canRunCandidateComparison({ question: "Hi", candidates: two }), true);
  assert.equal(canRunCandidateComparison({ question: "", candidates: two }), false);
  assert.equal(canRunCandidateComparison({ question: "   ", candidates: two }), false);

  const one = [createCandidate({ localId: "c1", agentId: "a1", agentVersionId: "v1", modelId: "MODEL_A" })];
  assert.equal(canRunCandidateComparison({ question: "Hi", candidates: one }), false);
});

test("an exact duplicate does not count toward the minimum of 2 -- it must be resolved first", () => {
  const candidates = [
    createCandidate({ localId: "c1", agentId: "a1", agentVersionId: "v1", modelId: "MODEL_A" }),
    createCandidate({ localId: "c2", agentId: "a1", agentVersionId: "v1", modelId: "MODEL_A" }), // exact dup of c1
  ];
  assert.equal(canRunCandidateComparison({ question: "Hi", candidates }), false);
});

test("canRunCandidateComparison imposes no artificial maximum", () => {
  const many = Array.from({ length: 12 }, (_, i) =>
    createCandidate({ localId: `c${i}`, agentId: `a${i}`, agentVersionId: `v${i}`, modelId: "MODEL_A" })
  );
  assert.equal(canRunCandidateComparison({ question: "Hi", candidates: many }), true);
});

// -- Catalog usage indicator --------------------------------------------------

test("countCandidatesUsingModel counts every candidate assigned to that model, across different Agents/Roles", () => {
  const candidates = [
    createCandidate({ localId: "c1", agentId: "a1", agentVersionId: "v1", modelId: "MODEL_A" }),
    createCandidate({ localId: "c2", agentId: "a2", agentVersionId: "v2", modelId: "MODEL_A" }),
    createCandidate({ localId: "c3", agentId: "a3", agentVersionId: "v3", modelId: "MODEL_B" }),
  ];
  assert.equal(countCandidatesUsingModel(candidates, "MODEL_A"), 2);
  assert.equal(countCandidatesUsingModel(candidates, "MODEL_B"), 1);
  assert.equal(countCandidatesUsingModel(candidates, "MODEL_C"), 0);
  assert.equal(countCandidatesUsingModel(candidates, null), 0);
});
