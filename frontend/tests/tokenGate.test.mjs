import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  clearSessionToken,
  currentToken,
  ensureToken,
  injectedToken,
  readSessionToken,
  writeSessionToken,
} from "../assets/js/tokenGate.js";

function fakeSessionStorage() {
  const values = new Map();
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

const realWindow = globalThis.window;

beforeEach(() => {
  globalThis.window = { __MAP_LOCAL_TOKEN__: null, sessionStorage: fakeSessionStorage() };
});

afterEach(() => {
  if (realWindow === undefined) delete globalThis.window;
  else globalThis.window = realWindow;
});

test("injectedToken reads window.__MAP_LOCAL_TOKEN__ when present (local dev)", () => {
  window.__MAP_LOCAL_TOKEN__ = "local-dev-token";
  assert.equal(injectedToken(), "local-dev-token");
});

test("injectedToken treats an empty string (hosted mode placeholder) as absent", () => {
  window.__MAP_LOCAL_TOKEN__ = "";
  assert.equal(injectedToken(), null);
});

test("session token round-trips through write/read/clear", () => {
  assert.equal(readSessionToken(), null);
  writeSessionToken("abc123");
  assert.equal(readSessionToken(), "abc123");
  clearSessionToken();
  assert.equal(readSessionToken(), null);
});

test("currentToken prefers the injected token over a stored session token", () => {
  window.__MAP_LOCAL_TOKEN__ = "injected";
  writeSessionToken("stored");
  assert.equal(currentToken(), "injected");
});

test("currentToken falls back to the stored session token when nothing is injected (hosted mode)", () => {
  window.__MAP_LOCAL_TOKEN__ = "";
  writeSessionToken("stored");
  assert.equal(currentToken(), "stored");
});

test("ensureToken resolves immediately without prompting when a token is already injected", async () => {
  window.__MAP_LOCAL_TOKEN__ = "local-dev-token";
  let verifyCalls = 0;
  let promptCalls = 0;
  const token = await ensureToken({
    verify: async (t) => {
      verifyCalls += 1;
      assert.equal(t, "local-dev-token");
      return true;
    },
    renderPrompt: () => {
      promptCalls += 1;
      return { remove: () => {} };
    },
  });
  assert.equal(token, "local-dev-token");
  assert.equal(verifyCalls, 1);
  assert.equal(promptCalls, 0);
});

test("ensureToken prompts when no token is available, and resolves once a valid one is submitted", async () => {
  window.__MAP_LOCAL_TOKEN__ = "";
  const removed = [];
  const overlay = { remove: () => removed.push(true) };
  let rendered = 0;

  const tokenPromise = ensureToken({
    verify: async (t) => t === "correct-token",
    renderPrompt: (onSubmit) => {
      rendered += 1;
      // Simulate the operator typing the token and submitting the form.
      queueMicrotask(() => onSubmit("correct-token", overlay));
      return overlay;
    },
  });

  const token = await tokenPromise;
  assert.equal(token, "correct-token");
  assert.equal(rendered, 1);
  assert.deepEqual(removed, [true]);
  // A hosted-mode-resolved token is cached in sessionStorage so a reload
  // within the same tab does not re-prompt.
  assert.equal(readSessionToken(), "correct-token");
});

test("ensureToken re-prompts with an error after an incorrect submission, then resolves on the next valid one", async () => {
  window.__MAP_LOCAL_TOKEN__ = "";
  let attempt = 0;
  const errors = [];

  const tokenPromise = ensureToken({
    verify: async (t) => t === "correct-token",
    renderPrompt: (onSubmit, errorMessage) => {
      attempt += 1;
      errors.push(errorMessage);
      const overlay = { remove: () => {} };
      if (attempt === 1) {
        queueMicrotask(() => onSubmit("wrong-token", overlay));
      } else {
        queueMicrotask(() => onSubmit("correct-token", overlay));
      }
      return overlay;
    },
  });

  const token = await tokenPromise;
  assert.equal(token, "correct-token");
  assert.equal(attempt, 2);
  assert.equal(errors[0], null);
  assert.match(errors[1], /not accepted/);
});

test("ensureToken clears and re-prompts when an existing stored token has gone stale", async () => {
  window.__MAP_LOCAL_TOKEN__ = "";
  writeSessionToken("stale-token");
  let promptCalls = 0;

  const token = await ensureToken({
    verify: async (t) => t === "fresh-token",
    renderPrompt: (onSubmit) => {
      promptCalls += 1;
      const overlay = { remove: () => {} };
      queueMicrotask(() => onSubmit("fresh-token", overlay));
      return overlay;
    },
  });

  assert.equal(token, "fresh-token");
  assert.equal(promptCalls, 1);
});
