import { test } from "node:test";
import assert from "node:assert/strict";
import { extractDeveloperKey, developerLabel, uniqueDevelopers, OTHER_DEVELOPER_KEY } from "../assets/js/modelDeveloper.js";

test("extracts the developer key from the canonical_model_id namespace", () => {
  assert.equal(extractDeveloperKey({ canonical_model_id: "deepseek/deepseek-v3" }), "deepseek");
  assert.equal(extractDeveloperKey({ canonical_model_id: "google/gemini-2.5-flash" }), "google");
  assert.equal(extractDeveloperKey({ canonical_model_id: "anthropic/claude-fable" }), "anthropic");
  assert.equal(extractDeveloperKey({ canonical_model_id: "openai/gpt-6" }), "openai");
  assert.equal(extractDeveloperKey({ canonical_model_id: "nvidia/nemotron-3" }), "nvidia");
  assert.equal(extractDeveloperKey({ canonical_model_id: "nex-agi/nex-n2.5" }), "nex-agi");
  assert.equal(extractDeveloperKey({ canonical_model_id: "inclusionai/ling-3.0" }), "inclusionai");
});

test("falls back to provider_model_id when canonical_model_id is absent", () => {
  assert.equal(extractDeveloperKey({ provider_model_id: "qwen/qwen3.8" }), "qwen");
});

test("strips a leading non-alphanumeric namespace marker so variants group together", () => {
  // Observed live on the real OpenRouter catalog: "~openai/..." alongside plain "openai/...".
  assert.equal(extractDeveloperKey({ canonical_model_id: "~openai/gpt-astra-latest" }), "openai");
  assert.equal(extractDeveloperKey({ canonical_model_id: "~deepseek/deepseek-pro-latest" }), "deepseek");
});

test("handles an unusual/unknown namespace generically rather than crashing or hardcoding", () => {
  assert.equal(extractDeveloperKey({ canonical_model_id: "some-new-lab/model-x" }), "some-new-lab");
  assert.equal(developerLabel("some-new-lab"), "Some New Lab");
});

test("a model id with no namespace ('/' ) falls back to the generic 'other' bucket", () => {
  assert.equal(extractDeveloperKey({ canonical_model_id: "bare-model-name" }), OTHER_DEVELOPER_KEY);
  assert.equal(extractDeveloperKey({}), OTHER_DEVELOPER_KEY);
  assert.equal(developerLabel(OTHER_DEVELOPER_KEY), "Other");
});

test("developerLabel uses the known-alias table for the developers named in the request", () => {
  assert.equal(developerLabel("deepseek"), "DeepSeek");
  assert.equal(developerLabel("google"), "Google");
  assert.equal(developerLabel("anthropic"), "Anthropic");
  assert.equal(developerLabel("openai"), "OpenAI");
  assert.equal(developerLabel("nvidia"), "NVIDIA");
  assert.equal(developerLabel("qwen"), "Qwen");
  assert.equal(developerLabel("nex-agi"), "Nex AGI");
  assert.equal(developerLabel("inclusionai"), "InclusionAI");
});

test("uniqueDevelopers reflects only developers actually present, with real counts, sorted by label", () => {
  const models = [
    { canonical_model_id: "google/gemini-2.5" },
    { canonical_model_id: "deepseek/deepseek-v3" },
    { canonical_model_id: "google/gemma-4" },
    { canonical_model_id: "anthropic/claude-fable" },
  ];
  const developers = uniqueDevelopers(models);
  assert.deepEqual(
    developers.map((d) => d.label),
    ["Anthropic", "DeepSeek", "Google"] // alphabetical
  );
  const google = developers.find((d) => d.label === "Google");
  assert.equal(google.count, 2);
  assert.deepEqual([...google.keys], ["google"]);
});

// -- Sidebar label-collision fix (post-1C correction): two raw namespaces
// that alias to the same developer label must merge into ONE sidebar
// entry with the combined count, and selecting it must match models from
// every raw namespace it represents. Generic -- applies to any alias
// collision, not a Meta-specific special case. -----------------------------

test("meta + meta-llama normalize to one 'Meta' entry, not two", () => {
  const models = [
    { canonical_model_id: "meta/llama-4-scout" },
    { canonical_model_id: "meta-llama/llama-3.1-70b" },
    { canonical_model_id: "meta-llama/llama-3.1-8b" },
  ];
  const developers = uniqueDevelopers(models);
  const metaEntries = developers.filter((d) => d.label === "Meta");
  assert.equal(metaEntries.length, 1, "must be exactly one Meta row, not one per raw namespace");
});

test("the merged Meta entry's count is the sum across every raw namespace it represents", () => {
  const models = [
    { canonical_model_id: "meta/llama-4-scout" },
    { canonical_model_id: "meta-llama/llama-3.1-70b" },
    { canonical_model_id: "meta-llama/llama-3.1-8b" },
  ];
  const meta = uniqueDevelopers(models).find((d) => d.label === "Meta");
  assert.equal(meta.count, 3);
  assert.deepEqual([...meta.keys].sort(), ["meta", "meta-llama"]);
});

test("the merged entry's keys select models from every underlying raw namespace, via filterModels", async () => {
  const { filterModels } = await import("../assets/js/modelFilter.js");
  const models = [
    { id: "pm-1", canonical_model_id: "meta/llama-4-scout" },
    { id: "pm-2", canonical_model_id: "meta-llama/llama-3.1-70b" },
    { id: "pm-3", canonical_model_id: "anthropic/claude-fable" },
  ];
  const meta = uniqueDevelopers(models).find((d) => d.label === "Meta");
  const result = filterModels(models, { developerKeys: meta.keys });
  assert.deepEqual(
    result.map((m) => m.id).sort(),
    ["pm-1", "pm-2"]
  );
});

test("unrelated developers remain excluded when selecting the merged Meta entry", async () => {
  const { filterModels } = await import("../assets/js/modelFilter.js");
  const models = [
    { id: "pm-1", canonical_model_id: "meta/llama-4-scout" },
    { id: "pm-2", canonical_model_id: "meta-llama/llama-3.1-70b" },
    { id: "pm-3", canonical_model_id: "google/gemini-2.5" },
    { id: "pm-4", canonical_model_id: "deepseek/deepseek-v3" },
  ];
  const meta = uniqueDevelopers(models).find((d) => d.label === "Meta");
  const result = filterModels(models, { developerKeys: meta.keys });
  assert.deepEqual(result.map((m) => m.id).sort(), ["pm-1", "pm-2"]);
  assert.ok(!result.some((m) => m.id === "pm-3" || m.id === "pm-4"));
});

test("this generalizes to any alias collision, not just Meta (mistral + mistralai)", () => {
  const models = [
    { canonical_model_id: "mistral/mistral-large" },
    { canonical_model_id: "mistralai/mixtral-8x7b" },
  ];
  const developers = uniqueDevelopers(models);
  const mistralEntries = developers.filter((d) => d.label === "Mistral AI");
  assert.equal(mistralEntries.length, 1);
  assert.equal(mistralEntries[0].count, 2);
  assert.deepEqual([...mistralEntries[0].keys].sort(), ["mistral", "mistralai"]);
});

test("developers with no alias collision are unaffected -- one raw key, one entry", () => {
  const models = [{ canonical_model_id: "deepseek/deepseek-v3" }, { canonical_model_id: "deepseek/deepseek-r1" }];
  const developers = uniqueDevelopers(models);
  assert.equal(developers.length, 1);
  assert.equal(developers[0].label, "DeepSeek");
  assert.deepEqual([...developers[0].keys], ["deepseek"]);
});
