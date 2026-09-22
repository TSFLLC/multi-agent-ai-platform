import { test } from "node:test";
import assert from "node:assert/strict";
import {
  selectActiveVersion,
  sortVersionsDescending,
  resolvePromptContent,
  formatList,
  formatJson,
  formatToolGrants,
  containsOnlyAllowedVersionFields,
} from "../assets/js/agentDetailView.js";

test("selectActiveVersion prefers the ACTIVE version regardless of order", () => {
  const versions = [
    { id: "v1", version: 1, status: "active" },
    { id: "v2", version: 2, status: "active" },
    { id: "v3", version: 3, status: "draft" },
  ];
  assert.equal(selectActiveVersion(versions).id, "v2");
});

test("selectActiveVersion falls back to the highest version when none is active", () => {
  const versions = [
    { id: "v1", version: 1, status: "draft" },
    { id: "v2", version: 2, status: "retired" },
  ];
  assert.equal(selectActiveVersion(versions).id, "v2");
});

test("selectActiveVersion handles empty/missing input without throwing", () => {
  assert.equal(selectActiveVersion([]), null);
  assert.equal(selectActiveVersion(null), null);
  assert.equal(selectActiveVersion(undefined), null);
});

test("sortVersionsDescending orders newest-first and does not mutate the input", () => {
  const versions = [{ version: 1 }, { version: 3 }, { version: 2 }];
  const sorted = sortVersionsDescending(versions);
  assert.deepEqual(sorted.map((v) => v.version), [3, 2, 1]);
  assert.deepEqual(versions.map((v) => v.version), [1, 3, 2]);
});

test("resolvePromptContent finds the matching prompt version's content", () => {
  const prompts = [
    { id: "p1", content: "v1 prompt" },
    { id: "p2", content: "v2 prompt" },
  ];
  assert.equal(resolvePromptContent(prompts, "p2"), "v2 prompt");
});

test("resolvePromptContent returns null when unresolvable -- never fabricates text", () => {
  assert.equal(resolvePromptContent([{ id: "p1", content: "x" }], "missing"), null);
  assert.equal(resolvePromptContent([], "p1"), null);
  assert.equal(resolvePromptContent(null, "p1"), null);
  assert.equal(resolvePromptContent([{ id: "p1", content: "x" }], null), null);
});

test("formatList returns null (never an empty string or empty bracket) for unset/empty capabilities", () => {
  assert.equal(formatList(null), null);
  assert.equal(formatList(undefined), null);
  assert.equal(formatList([]), null);
  assert.equal(formatList(["read", "write"]), "read, write");
});

test("formatJson returns null for unset policy fields, including an empty object", () => {
  assert.equal(formatJson(null), null);
  assert.equal(formatJson(undefined), null);
  assert.equal(formatJson({}), null);
  assert.equal(formatJson({ mode: "manual" }), '{"mode":"manual"}');
});

test("formatToolGrants returns null when no tool grants are configured", () => {
  assert.equal(formatToolGrants(null), null);
  assert.equal(formatToolGrants([]), null);
  assert.equal(
    formatToolGrants([{ tool_id: "t1", grant_type: "allow" }]),
    "t1 (allow)"
  );
});

test("containsOnlyAllowedVersionFields rejects any field outside the known-safe AgentVersionRead shape", () => {
  assert.equal(
    containsOnlyAllowedVersionFields({ id: "v1", version: 1, status: "active" }),
    true
  );
  assert.equal(
    containsOnlyAllowedVersionFields({ id: "v1", api_key: "sk-should-never-appear" }),
    false
  );
  assert.equal(
    containsOnlyAllowedVersionFields({ id: "v1", secret_store_ref: "secret:1" }),
    false
  );
});
