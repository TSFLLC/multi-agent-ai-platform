import { test } from "node:test";
import assert from "node:assert/strict";
import { academyEnrollmentHref, academyProgressText, groupAcademyItems } from "../assets/js/pages/academy.js";

test("Academy progress reports explicit evidence denominators", () => {
  assert.equal(
    academyProgressText({ completed_items: 3, required_items: 8, demonstrated_concepts: 2, required_concepts: 6 }),
    "3 of 8 required items complete; 2 of 6 required concepts demonstrated.",
  );
});

test("Program overview groups curriculum items by module deterministically", () => {
  assert.deepEqual(Object.keys(groupAcademyItems([
    { module_key: "week-2", day: 8 },
    { module_key: "week-1", day: 1 },
    { module_key: "week-2", day: 9 },
  ])), ["week-2", "week-1"]);
  assert.equal(groupAcademyItems([{ day: 1 }]).unassigned.length, 1);
});

test("Academy routes encode enrollment identifiers", () => {
  assert.equal(academyEnrollmentHref("enrollment/1", "progress"), "#/academy/enrollments/enrollment%2F1/progress");
});
