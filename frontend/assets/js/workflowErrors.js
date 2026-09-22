// Error / validation-issue handling for the Workflow Studio (MA7.6A) -- pure.
//
// The server reports publish-time and dry-run validation as a list of textual
// issues (error.detail.issues, or `issues` on the validate response). They are
// TEXT: an issue may or may not concern one specific node. This module only ever
// says a node is "possibly related" when the issue names it in one of the
// validator's own message shapes -- it never claims an exact association.

// Normalizes anything thrown by api.js (an ApiError) -- or anything else -- into
// { message, issues, status, code, isValidation }. Never throws.
export function normalizeApiError(err) {
  const message = (err && err.message) || "Something went wrong.";
  const detail = err && err.detail;
  const issues =
    detail && Array.isArray(detail.issues) ? detail.issues.filter((issue) => typeof issue === "string" && issue) : [];
  return {
    message,
    issues,
    status: err && typeof err.status === "number" ? err.status : null,
    code: (err && err.code) || null,
    isValidation: Boolean(err && err.code === "workflow_validation_failed"),
  };
}

// The dry-run validate response -> { valid, issues }. A malformed body is
// treated as "not valid, no issues listed" rather than as valid.
export function normalizeValidationResult(body) {
  const issues = body && Array.isArray(body.issues) ? body.issues.filter((issue) => typeof issue === "string") : [];
  return { valid: Boolean(body && body.valid === true && issues.length === 0), issues };
}

function escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Node keys an issue POSSIBLY concerns. Only the validator's own phrasings
// count -- "<TYPE> node KEY ...", "Node KEY ...", "node: KEY", "Edge A -> B" --
// so a key that happens to be an ordinary word ("model", "test") is not matched
// by accident in a sentence that merely contains it.
export function nodeKeysMentioned(issue, nodeKeys) {
  const text = String(issue || "");
  const keys = (nodeKeys || []).filter(Boolean);
  const found = new Set();
  for (const key of keys) {
    // "AGENT node KEY ...", "Node KEY ...", "Unreachable node: KEY"
    const asNode = new RegExp(`\\bnode:?\\s+${escapeRegExp(key)}(?![A-Za-z0-9_])`, "i");
    if (asNode.test(text)) found.add(key);
  }
  // "Edge SOURCE -> TARGET has a condition ..." names two node keys.
  const edge = /\bEdge\s+(\S+)\s+->\s+(\S+)/i.exec(text);
  if (edge) {
    for (const token of [edge[1], edge[2]]) if (keys.includes(token)) found.add(token);
  }
  return [...found].sort();
}

// [{ text, nodeKeys }] -- nodeKeys are "possibly related", in sorted order.
export function annotateIssues(issues, nodeKeys) {
  return (issues || []).map((text) => ({ text, nodeKeys: nodeKeysMentioned(text, nodeKeys) }));
}

export function flaggedNodeKeys(annotated) {
  const keys = new Set();
  for (const entry of annotated || []) for (const key of entry.nodeKeys) keys.add(key);
  return keys;
}
