import { test } from "node:test";
import assert from "node:assert/strict";
import {
  formatCostForClassification,
  formatCost,
  formatPricePerMillion,
  formatPriceSummary,
  pricingLabel,
  formatTokens,
  formatLatency,
  candidateStatusLabel,
  phaseLabel,
  isTerminalPhase,
  truncate,
} from "../assets/js/format.js";

test("FREE classification always renders $0.0000 regardless of cost_amount", () => {
  assert.equal(formatCostForClassification("0.0000", "free"), "FREE — $0.0000");
  assert.equal(formatCostForClassification(null, "free"), "FREE — $0.0000");
});

test("UNKNOWN pricing is never displayed as FREE", () => {
  const result = formatCostForClassification("0", "unknown");
  assert.notEqual(result, "FREE — $0.0000");
  assert.match(result, /unknown/i);
});

test("PAID classification formats the actual cost to 4 decimals", () => {
  assert.equal(formatCostForClassification("1.23456", "paid"), "$1.2346");
});

test("formatCost handles null/undefined without throwing", () => {
  assert.equal(formatCost(null), "—");
  assert.equal(formatCost(undefined), "—");
  assert.equal(formatCost("2.5"), "$2.5000");
});

test("pricingLabel never infers FREE from anything but the given classification", () => {
  assert.equal(pricingLabel("free"), "FREE");
  assert.equal(pricingLabel("paid"), "PAID");
  assert.equal(pricingLabel("unknown"), "UNKNOWN");
  assert.equal(pricingLabel(undefined), "UNKNOWN");
});

test("formatTokens formats thousands separators and handles missing values", () => {
  assert.equal(formatTokens(1234), "1,234");
  assert.equal(formatTokens(null), "—");
});

test("formatLatency switches from ms to s at 1000ms", () => {
  assert.equal(formatLatency(500), "500 ms");
  assert.equal(formatLatency(1500), "1.50 s");
  assert.equal(formatLatency(0), "—");
});

test("candidateStatusLabel maps backend TaskRunStatus values to operator-readable labels", () => {
  assert.equal(candidateStatusLabel("not_launched"), "Not launched");
  assert.equal(candidateStatusLabel("queued"), "Queued");
  assert.equal(candidateStatusLabel("completed"), "Completed");
  assert.equal(candidateStatusLabel("failed"), "Failed");
});

test("phaseLabel and isTerminalPhase agree on which phases end polling", () => {
  assert.equal(isTerminalPhase("pending"), false);
  assert.equal(isTerminalPhase("running"), false);
  assert.equal(isTerminalPhase("ready_for_selection"), true);
  assert.equal(isTerminalPhase("completed"), true);
  assert.equal(isTerminalPhase("failed"), true);
  assert.equal(isTerminalPhase("cancelled"), true);
  assert.equal(phaseLabel("ready_for_selection"), "Ready for selection");
});

test("truncate keeps short strings intact and adds an ellipsis to long ones", () => {
  assert.equal(truncate("short", 20), "short");
  const long = "a".repeat(200);
  const truncated = truncate(long, 120);
  assert.equal(truncated.length, 120);
  assert.ok(truncated.endsWith("…"));
});

// -- Catalog-browsing price display (MA5-UI Post-UAT Enhancement 1B) --------

test("formatPricePerMillion formats a known rate to 2 decimals, or null if unknown", () => {
  assert.equal(formatPricePerMillion("1.5"), "$1.50");
  assert.equal(formatPricePerMillion(0.3), "$0.30");
  assert.equal(formatPricePerMillion(null), null);
  assert.equal(formatPricePerMillion(undefined), null);
});

test("formatPriceSummary shows FREE — $0 for a FREE model regardless of raw cost fields", () => {
  assert.equal(formatPriceSummary("free", "0", "0"), "FREE — $0");
  assert.equal(formatPriceSummary("free", null, null), "FREE — $0");
});

test("formatPriceSummary shows UNKNOWN for unknown pricing, never a fabricated number", () => {
  assert.equal(formatPriceSummary("unknown", null, null), "UNKNOWN");
  assert.equal(formatPriceSummary(undefined, null, null), "UNKNOWN");
});

test("formatPriceSummary shows actual input/output per-million rates for PAID models", () => {
  assert.equal(formatPriceSummary("paid", "3.00", "15.00"), "In: $3.00/1M · Out: $15.00/1M");
});

test("formatPriceSummary never displays UNKNOWN pricing as FREE", () => {
  const summary = formatPriceSummary("unknown", "0", "0");
  assert.notEqual(summary, "FREE — $0");
  assert.equal(summary, "UNKNOWN");
});
