import { test } from "node:test";
import assert from "node:assert/strict";
import { isCloseKey } from "../assets/js/modal.js";

// frontend/assets/js/modal.js otherwise manipulates document/window (append
// to <body>, focus management, keydown listeners) -- there is no DOM in
// plain Node and this project deliberately adds no jsdom/browser-test
// dependency (Section: no build step, no new frameworks), so the
// open/close/backdrop/focus-restore behavior itself is verified live in
// the browser (see the MA5-UI Post-UAT Enhancement 2 UAT report), per the
// task's own "modal open/close behavior where practical" scoping. What IS
// pure and worth covering here is the key-based close decision.

test("Escape (and the older 'Esc' key name) close the modal", () => {
  assert.equal(isCloseKey("Escape"), true);
  assert.equal(isCloseKey("Esc"), true);
});

test("other keys never close the modal", () => {
  assert.equal(isCloseKey("Enter"), false);
  assert.equal(isCloseKey("a"), false);
  assert.equal(isCloseKey(" "), false);
  assert.equal(isCloseKey(""), false);
  assert.equal(isCloseKey(undefined), false);
});
