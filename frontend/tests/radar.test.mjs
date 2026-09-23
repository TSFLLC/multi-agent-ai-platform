import { test } from "node:test";
import assert from "node:assert/strict";
import { claimTypeLabel, groupToday, reasonLabel, sortRecent } from "../assets/js/pages/radar.js";

function detail(id, date, reasons = [], triage = null, type = "capability") {
  return {
    development: { id, title: id, development_type: type, announced_at: date, first_seen_at: date },
    reason_codes: reasons,
    current_triage: triage,
    verification_level: reasons.includes("NEEDS_VERIFICATION") ? "CLAIMED" : "DOCUMENTED",
  };
}

test("Radar labels retain claim-type boundaries and deterministic reason wording", () => {
  assert.equal(reasonLabel("RELATED_TO_INTEREST"), "Related to your declared interest");
  assert.equal(claimTypeLabel("PROVIDER_CLAIM"), "Provider claim");
  assert.equal(claimTypeLabel("COMMUNITY_SIGNAL"), "Community signal");
});

test("Today grouping is deterministic, bounded, and does not backfill empty sections", () => {
  const items = Array.from({ length: 5 }, (_, index) => detail(`model-${index}`, `2026-09-${String(22 - index).padStart(2, "0")}`, ["NEW_MODEL"] , null, "model_release"));
  const groups = groupToday(items);
  assert.equal(groups.newModels.length, 3);
  assert.equal(groups.important.length, 3);
  assert.equal(groups.learning.length, 0);
  assert.equal(groups.platform.length, 0);
});

test("ignored items do not appear in important changes but remain reviewable", () => {
  const ignored = detail("ignored", "2026-09-22", ["NEW_MODEL"], { decision: "IGNORE" }, "model_release");
  const groups = groupToday([ignored]);
  assert.equal(groups.important.length, 0);
  assert.equal(groups.newModels.length, 1);
});

test("recent ordering uses announced date then first-seen date", () => {
  const sorted = sortRecent([
    detail("old", "2026-09-20"),
    detail("new", "2026-09-22"),
  ]);
  assert.deepEqual(sorted.map((item) => item.development.id), ["new", "old"]);
});
