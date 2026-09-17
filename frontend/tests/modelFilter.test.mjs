import { test } from "node:test";
import assert from "node:assert/strict";
import {
  PRICING_FILTER_ALL,
  filterModels,
  modelMatchesQuery,
  uniqueProviderIds,
  selectedModelsList,
  boundedResults,
  canRunComparison,
} from "../assets/js/modelFilter.js";

const MODELS = [
  { id: "pm-1", model_id: "m-1", provider_id: "prov-a", canonical_model_id: "openai/gpt-6-astra", pricing_classification: "paid" },
  { id: "pm-2", model_id: "m-2", provider_id: "prov-a", canonical_model_id: "meta/llama-4-free", pricing_classification: "free" },
  { id: "pm-3", model_id: "m-3", provider_id: "prov-b", canonical_model_id: "anthropic/claude-fable", pricing_classification: "unknown" },
  { id: "pm-4", model_id: "m-4", provider_id: "prov-b", canonical_model_id: "mystery/no-pricing-yet", pricing_classification: "unknown" },
];

test("search filters by canonical model name, case-insensitive substring", () => {
  const result = filterModels(MODELS, { query: "LLAMA" });
  assert.deepEqual(result.map((m) => m.id), ["pm-2"]);
});

test("search with no query returns everything (subject to other filters)", () => {
  const result = filterModels(MODELS, { query: "" });
  assert.equal(result.length, MODELS.length);
});

test("modelMatchesQuery also matches the provider's own model id, not just canonical_model_id", () => {
  const model = { canonical_model_id: "display-name", provider_model_id: "raw/provider-slug:free" };
  assert.equal(modelMatchesQuery(model, "provider-slug"), true);
});

test("pricing filter isolates exactly the requested classification", () => {
  assert.deepEqual(filterModels(MODELS, { pricing: "free" }).map((m) => m.id), ["pm-2"]);
  assert.deepEqual(filterModels(MODELS, { pricing: "paid" }).map((m) => m.id), ["pm-1"]);
  assert.deepEqual(
    filterModels(MODELS, { pricing: "unknown" })
      .map((m) => m.id)
      .sort(),
    ["pm-3", "pm-4"]
  );
});

test("pricing filter never treats UNKNOWN as FREE", () => {
  const free = filterModels(MODELS, { pricing: "free" });
  assert.ok(!free.some((m) => m.pricing_classification === "unknown"));
});

test("PRICING_FILTER_ALL returns every classification", () => {
  assert.equal(filterModels(MODELS, { pricing: PRICING_FILTER_ALL }).length, MODELS.length);
});

test("provider filter isolates exactly one real provider_id", () => {
  assert.deepEqual(
    filterModels(MODELS, { providerId: "prov-b" })
      .map((m) => m.id)
      .sort(),
    ["pm-3", "pm-4"]
  );
});

test("search, pricing, and provider filters combine (AND, not OR)", () => {
  const result = filterModels(MODELS, { query: "free", pricing: "free", providerId: "prov-a" });
  assert.deepEqual(result.map((m) => m.id), ["pm-2"]);
  const noMatch = filterModels(MODELS, { query: "free", pricing: "free", providerId: "prov-b" });
  assert.deepEqual(noMatch, []);
});

test("uniqueProviderIds reflects only real providers actually present, deduplicated", () => {
  assert.deepEqual(uniqueProviderIds(MODELS).sort(), ["prov-a", "prov-b"]);
  assert.deepEqual(uniqueProviderIds([MODELS[0]]), ["prov-a"]);
});

test("selectedModelsList stays independent of any filter and preserves registry order", () => {
  const selected = new Set(["pm-3", "pm-1"]);
  const result = selectedModelsList(MODELS, selected);
  assert.deepEqual(result.map((m) => m.id), ["pm-1", "pm-3"]); // registry order, not selection order
});

test("selectedModelsList returns selected entries even when they'd be excluded by any filter", () => {
  // pm-3 is 'unknown' pricing -- would be hidden by a 'free' filter -- but
  // selection must never be filter-dependent.
  const selected = new Set(["pm-3"]);
  const filteredView = filterModels(MODELS, { pricing: "free" });
  assert.equal(filteredView.some((m) => m.id === "pm-3"), false);
  assert.deepEqual(selectedModelsList(MODELS, selected).map((m) => m.id), ["pm-3"]);
});

test("boundedResults caps the visible slice and reports how many remain", () => {
  const { visible, remaining } = boundedResults(MODELS, 2);
  assert.equal(visible.length, 2);
  assert.equal(remaining, 2);
  const all = boundedResults(MODELS, 100);
  assert.equal(all.visible.length, 4);
  assert.equal(all.remaining, 0);
});

test("canRunComparison requires a question, an agent, and at least 2 selected models", () => {
  assert.equal(canRunComparison({ question: "Hi", agentId: "a1", selectedCount: 2 }), true);
  assert.equal(canRunComparison({ question: "Hi", agentId: "a1", selectedCount: 1 }), false);
  assert.equal(canRunComparison({ question: "", agentId: "a1", selectedCount: 2 }), false);
  assert.equal(canRunComparison({ question: "   ", agentId: "a1", selectedCount: 2 }), false);
  assert.equal(canRunComparison({ question: "Hi", agentId: null, selectedCount: 2 }), false);
});

test("canRunComparison imposes no artificial maximum", () => {
  assert.equal(canRunComparison({ question: "Hi", agentId: "a1", selectedCount: 50 }), true);
});
