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
  const google = developers.find((d) => d.key === "google");
  assert.equal(google.count, 2);
});
