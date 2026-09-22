// Hosted-mode auth token gate — MA7.7B.
//
// Locally (app.web.serve_console injects the real token into
// window.__MAP_LOCAL_TOKEN__ — see index.html), every function here is a
// no-op fast path: currentToken() returns the injected value immediately
// and ensureToken() never renders anything. Behavior is unchanged from
// before this module existed.
//
// In hosted mode (app.config.settings.hosted_mode), app.web.serve_console
// deliberately serves an EMPTY token placeholder (MA7.7A blocker #2: the
// real owner token must never be injected into this unauthenticated
// response). ensureToken() instead prompts the operator for the token
// client-side and holds it only in sessionStorage (cleared when the tab
// closes; never localStorage, never sent anywhere but this same origin's
// own Authorization header) for the rest of that tab's session.

const SESSION_KEY = "map_auth_token";

function getWindow() {
  return typeof window !== "undefined" ? window : null;
}

function getSessionStorage() {
  const win = getWindow();
  if (!win) return null;
  try {
    return win.sessionStorage;
  } catch {
    // Can throw in a locked-down/private browsing context.
    return null;
  }
}

// The one place this module reads the server-injected global — never
// re-read as "the truth" once ensureToken() has resolved (see
// currentToken()'s in-memory cache below), so a stale/empty placeholder
// left over from a prior render can't shadow a token the operator just
// entered.
export function injectedToken() {
  const win = getWindow();
  const value = win ? win.__MAP_LOCAL_TOKEN__ : null;
  return value && value.length > 0 ? value : null;
}

export function readSessionToken() {
  const storage = getSessionStorage();
  if (!storage) return null;
  try {
    const value = storage.getItem(SESSION_KEY);
    return value && value.length > 0 ? value : null;
  } catch {
    return null;
  }
}

export function writeSessionToken(token) {
  const storage = getSessionStorage();
  if (!storage) return;
  try {
    storage.setItem(SESSION_KEY, token);
  } catch {
    // Session storage can throw (private mode, blocked site data, quota).
    // The token still works for the rest of this page load via the
    // in-memory cache below; it just won't survive a reload.
  }
}

export function clearSessionToken() {
  const storage = getSessionStorage();
  if (!storage) return;
  try {
    storage.removeItem(SESSION_KEY);
  } catch {
    // Best-effort; see writeSessionToken.
  }
}

// The token every API call (frontend/assets/js/api.js) actually sends.
// Priority: the server-injected value (local dev), else whatever this
// module previously resolved and cached in sessionStorage (hosted mode —
// survives a reload within the same tab). No separate in-memory cache on
// top of that: sessionStorage read/write is already synchronous and
// cheap, and a second cache layer would only be one more place for the
// two to (however briefly) disagree. Never a network call, never async —
// api.js calls this synchronously on every request.
export function currentToken() {
  return injectedToken() || readSessionToken();
}

function setCurrentToken(token) {
  // Only sessionStorage-cache a token this module itself resolved
  // (hosted mode) — the locally-injected token is already available via
  // injectedToken() and deliberately never written to storage (matches
  // api.js's existing "never persisted to localStorage/sessionStorage"
  // invariant for the local-dev token).
  if (!injectedToken()) writeSessionToken(token);
}

async function defaultVerify(token) {
  try {
    const res = await fetch("/me", { headers: { Authorization: `Bearer ${token}` } });
    return res.ok;
  } catch {
    return false;
  }
}

function defaultRenderPrompt(onSubmit, errorMessage) {
  const doc = document;
  const overlay = doc.createElement("div");
  overlay.className = "token-gate-overlay";
  overlay.innerHTML = `
    <form class="token-gate-form">
      <h1>Multi-Agent AI Platform</h1>
      <p>Enter the hosted access token to continue.</p>
      <input type="password" name="token" autocomplete="off" autofocus />
      ${errorMessage ? `<p class="token-gate-error">${errorMessage}</p>` : ""}
      <button type="submit">Continue</button>
    </form>
  `;
  const form = overlay.querySelector("form");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = form.elements.token.value.trim();
    if (value) onSubmit(value, overlay);
  });
  doc.body.appendChild(overlay);
  return overlay;
}

// Resolves once a usable token is available: immediately, if one is
// already injected/cached/stored, or after the operator successfully
// enters one via a login overlay otherwise. `verify`/`renderPrompt` are
// injectable purely for testing (see frontend/tests/tokenGate.test.mjs) —
// callers always use the defaults.
export async function ensureToken({ verify = defaultVerify, renderPrompt = defaultRenderPrompt } = {}) {
  const existing = currentToken();
  if (existing && (await verify(existing))) {
    return existing;
  }
  if (existing) clearSessionToken();

  return new Promise((resolve) => {
    let overlay = null;
    const tryToken = async (candidate, currentOverlay) => {
      if (await verify(candidate)) {
        setCurrentToken(candidate);
        if (currentOverlay && currentOverlay.remove) currentOverlay.remove();
        resolve(candidate);
        return;
      }
      if (currentOverlay && currentOverlay.remove) currentOverlay.remove();
      overlay = renderPrompt(tryToken, "That token was not accepted. Try again.");
    };
    overlay = renderPrompt(tryToken, null);
  });
}
