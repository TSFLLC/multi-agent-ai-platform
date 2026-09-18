import { test } from "node:test";
import assert from "node:assert/strict";
import { activeVersionsOf, roleLabelFor, buildVersionIndex } from "../assets/js/agentDirectory.js";

// loadAgentCatalog() itself does real fetches (via api.js) and is
// exercised live in the MA5-UI Post-UAT Enhancement 1C UAT instead --
// these tests cover the pure, DOM/network-free logic this module adds.

test("activeVersionsOf keeps only ACTIVE versions, sorted by version number", () => {
  const entry = {
    agent: { id: "a1", name: "Agent One" },
    versions: [
      { id: "v3", version: 3, status: "draft" },
      { id: "v1", version: 1, status: "active" },
      { id: "v2", version: 2, status: "active" },
      { id: "v4", version: 4, status: "retired" },
    ],
  };
  const active = activeVersionsOf(entry);
  assert.deepEqual(active.map((v) => v.id), ["v1", "v2"]);
});

test("activeVersionsOf handles a missing/undefined entry without throwing", () => {
  assert.deepEqual(activeVersionsOf(undefined), []);
  assert.deepEqual(activeVersionsOf(null), []);
});

test("roleLabelFor prefers the version's own role, then name, then a bare version number", () => {
  assert.equal(roleLabelFor({ role: "developer", name: "Dev Agent", version: 1 }), "developer");
  assert.equal(roleLabelFor({ role: null, name: "Dev Agent", version: 1 }), "Dev Agent");
  assert.equal(roleLabelFor({ role: null, name: null, version: 2 }), "v2");
  assert.equal(roleLabelFor(null), "—");
});

test("buildVersionIndex resolves every AgentVersion id back to its parent Agent + itself", () => {
  const catalog = [
    {
      agent: { id: "a1", name: "Agent One" },
      versions: [{ id: "v1", version: 1, role: "developer" }],
    },
    {
      agent: { id: "a2", name: "Agent Two" },
      versions: [
        { id: "v2", version: 1, role: "researcher" },
        { id: "v3", version: 2, role: "planner" },
      ],
    },
  ];
  const index = buildVersionIndex(catalog);
  assert.equal(index.get("v1").agent.name, "Agent One");
  assert.equal(index.get("v2").agent.name, "Agent Two");
  assert.equal(index.get("v3").version.role, "planner");
  assert.equal(index.has("nonexistent"), false);
});

test("buildVersionIndex lets the same Agent expose multiple independently-resolvable Roles", () => {
  // This is the structural proof behind the Enhancement 1C architecture
  // finding: one Agent, two ACTIVE AgentVersions, each its own real Role.
  const catalog = [
    {
      agent: { id: "a1", name: "Agent One" },
      versions: [
        { id: "v1", version: 1, role: "developer", status: "active" },
        { id: "v2", version: 2, role: "reviewer", status: "active" },
      ],
    },
  ];
  const index = buildVersionIndex(catalog);
  assert.equal(index.get("v1").version.role, "developer");
  assert.equal(index.get("v2").version.role, "reviewer");
  assert.equal(index.get("v1").agent.id, index.get("v2").agent.id, "same Agent identity for both roles");
});
