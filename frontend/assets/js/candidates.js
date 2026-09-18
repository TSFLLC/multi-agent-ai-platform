// Candidate-based comparison configuration -- MA5-UI Post-UAT Enhancement
// 1C. Pure, DOM-free domain logic (unit-tested directly, see
// frontend/tests/candidates.test.mjs) for the new model:
//
//   multiple comparison candidates, each independently choosing
//   Agent + Role (= AgentVersion, see agentDirectory.js) + Model.
//
// Candidate identity is explicitly NOT model identity (Section 1): the
// same modelId may appear on any number of candidates. The only thing
// ever prevented is an EXACT duplicate configuration -- same
// agentVersionId AND same modelId both matching another candidate
// (Section 4). Same model + different Agent, same model + different
// Role (a different agentVersionId for the same Agent), and same Agent +
// different model are all always valid and never deduplicated.

export function createCandidate({ localId, agentId = null, agentVersionId = null, modelId = null }) {
  return { localId, agentId, agentVersionId, modelId };
}

export function isCandidateComplete(candidate) {
  return Boolean(candidate && candidate.agentId && candidate.agentVersionId && candidate.modelId);
}

export function validCandidates(candidates) {
  return candidates.filter(isCandidateComplete);
}

// localIds of every candidate that exactly repeats an EARLIER candidate's
// (agentVersionId, modelId) pair -- the first occurrence is never flagged,
// only later exact repeats. Only complete candidates can collide; an
// incomplete one (still missing a field) is never marked a duplicate.
export function duplicateLocalIds(candidates) {
  const seen = new Set();
  const duplicates = new Set();
  for (const candidate of candidates) {
    if (!isCandidateComplete(candidate)) continue;
    const key = `${candidate.agentVersionId}::${candidate.modelId}`;
    if (seen.has(key)) duplicates.add(candidate.localId);
    else seen.add(key);
  }
  return duplicates;
}

// The candidates that would actually be submitted to POST /comparisons --
// complete, and not an exact repeat of an earlier one.
export function runnableCandidates(candidates) {
  const duplicates = duplicateLocalIds(candidates);
  return validCandidates(candidates).filter((c) => !duplicates.has(c.localId));
}

export function canRunCandidateComparison({ question, candidates }) {
  return Boolean(question && question.trim().length > 0 && runnableCandidates(candidates).length >= 2);
}

export function countCandidatesUsingModel(candidates, modelId) {
  if (!modelId) return 0;
  return candidates.filter((c) => c.modelId === modelId).length;
}
