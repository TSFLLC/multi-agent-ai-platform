// Pure, DOM-free data-shaping for the Agent Detail view (MA7.7D) --
// mirrors the project's existing convention (see agentDirectory.js,
// workflowGraph.js): DOM construction stays in the page module
// (frontend/assets/js/pages/agents.js), tested live in the browser; the
// logic that decides WHAT to show is pulled out here so it can be
// covered by plain node:test (no jsdom/browser-test dependency, per
// modal.test.mjs's own note on that project decision).

export function selectActiveVersion(versions) {
  if (!Array.isArray(versions) || versions.length === 0) return null;
  const sorted = sortVersionsDescending(versions);
  return sorted.find((v) => v.status === "active") || sorted[0];
}

export function sortVersionsDescending(versions) {
  if (!Array.isArray(versions)) return [];
  return versions.slice().sort((a, b) => b.version - a.version);
}

export function resolvePromptContent(promptVersions, promptVersionId) {
  if (!promptVersionId || !Array.isArray(promptVersions)) return null;
  const match = promptVersions.find((p) => p.id === promptVersionId);
  return match ? match.content : null;
}

// Renders any unset/empty policy field as `null` -- the caller (the DOM
// layer) is responsible for turning `null` into the literal "Not
// configured" string called for by the spec; keeping that string out of
// this module keeps it a pure "is there a value" decision, not a display
// string source of truth.
export function formatList(value) {
  if (!Array.isArray(value) || value.length === 0) return null;
  return value.join(", ");
}

export function formatJson(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0) return null;
  return JSON.stringify(value);
}

export function formatToolGrants(grants) {
  if (!Array.isArray(grants) || grants.length === 0) return null;
  return grants.map((g) => `${g.tool_id} (${g.grant_type})`).join(", ");
}

// Never exposes anything beyond the fields the backend's AgentVersionRead
// contract already sends (Section 20.1/20.2: secrets never leave
// SecretService) -- this is a defensive allowlist, not a redaction step,
// so a future backend field never accidentally reaches the UI unreviewed.
const ALLOWED_VERSION_FIELDS = new Set([
  "id",
  "agent_id",
  "version",
  "name",
  "role",
  "description",
  "prompt_version_id",
  "capabilities",
  "model_policy",
  "default_model_strategy",
  "context_policy",
  "memory_policy",
  "budget_policy",
  "timeout_seconds",
  "retry_policy",
  "approval_requirements",
  "status",
  "created_at",
  "published_at",
  "tool_grants",
]);

export function containsOnlyAllowedVersionFields(version) {
  if (!version || typeof version !== "object") return true;
  return Object.keys(version).every((key) => ALLOWED_VERSION_FIELDS.has(key));
}
