import { test } from "node:test";
import assert from "node:assert/strict";
import { clampIndex, hasPrevious, hasNext, previousIndex, nextIndex } from "../assets/js/focusNav.js";

test("clampIndex keeps an in-range index unchanged", () => {
  assert.equal(clampIndex(1, 3), 1);
});

test("clampIndex clamps below zero to the first candidate (no wrap)", () => {
  assert.equal(clampIndex(-1, 3), 0);
});

test("clampIndex clamps past the end to the last candidate (no wrap)", () => {
  assert.equal(clampIndex(3, 3), 2);
  assert.equal(clampIndex(99, 3), 2);
});

test("clampIndex handles an empty list without throwing", () => {
  assert.equal(clampIndex(0, 0), 0);
  assert.equal(clampIndex(5, 0), 0);
});

test("hasPrevious/hasNext report the boundaries of a 3-candidate list", () => {
  assert.equal(hasPrevious(0), false);
  assert.equal(hasPrevious(1), true);
  assert.equal(hasNext(2, 3), false);
  assert.equal(hasNext(1, 3), true);
});

test("previousIndex/nextIndex are plain arithmetic -- clamping is the caller's job", () => {
  assert.equal(previousIndex(1), 0);
  assert.equal(nextIndex(1), 2);
});

test("walking Candidate 1 -> 2 -> 3 in a 3-candidate comparison never wraps past the end", () => {
  let index = 0;
  index = clampIndex(nextIndex(index), 3);
  assert.equal(index, 1);
  index = clampIndex(nextIndex(index), 3);
  assert.equal(index, 2);
  // one more "Next" at the last candidate stays put (Next should be
  // disabled here by hasNext, but clampIndex is the last line of defense).
  index = clampIndex(nextIndex(index), 3);
  assert.equal(index, 2);
});
