// Hash-based router — deliberately not the History API: every route lives
// under "/" (app.web.serve_console is the only server-rendered page), so
// hash routing needs no server-side catch-all and can never collide with
// a real API path (Section 1: reuse infra, avoid backend redesign).

import { clear } from "./dom.js";

const routes = [];
let root = null;
let navLinks = null;
let currentCleanup = null;

export function registerRoute(pattern, render) {
  const paramNames = [];
  const regexSource = pattern
    .split("/")
    .map((segment) => {
      if (segment.startsWith(":")) {
        paramNames.push(segment.slice(1));
        return "([^/]+)";
      }
      return segment.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    })
    .join("/");
  routes.push({ regex: new RegExp(`^${regexSource}$`), paramNames, render });
}

export function startRouter(rootEl, navEl, defaultHash = "#/ask") {
  root = rootEl;
  navLinks = navEl;
  window.addEventListener("hashchange", dispatch);
  if (!window.location.hash) {
    window.location.hash = defaultHash;
  } else {
    dispatch();
  }
}

async function dispatch() {
  const hash = window.location.hash || "#/ask";
  const path = hash.slice(1); // drop leading '#'
  const [routePath] = path.split("?");

  if (typeof currentCleanup === "function") {
    try {
      currentCleanup();
    } catch {
      // A page's cleanup failing must never block navigation to the next one.
    }
    currentCleanup = null;
  }

  updateActiveNav(routePath);

  for (const route of routes) {
    const match = route.regex.exec(routePath);
    if (!match) continue;
    const params = {};
    route.paramNames.forEach((name, idx) => {
      params[name] = decodeURIComponent(match[idx + 1]);
    });
    clear(root);
    try {
      const cleanup = await route.render(root, params);
      if (typeof cleanup === "function") currentCleanup = cleanup;
    } catch (err) {
      renderRouteError(err);
    }
    return;
  }

  renderNotFound(routePath);
}

function updateActiveNav(routePath) {
  if (!navLinks) return;
  const topSegment = `/${routePath.split("/")[1] || ""}`;
  navLinks.querySelectorAll("a[data-route]").forEach((link) => {
    const isActive = topSegment === `/${link.dataset.route}`;
    link.classList.toggle("active", isActive);
  });
}

function renderNotFound(routePath) {
  clear(root);
  const div = document.createElement("div");
  div.className = "empty-state";
  div.innerHTML = `<div class="icon">🔍</div><h2>Not found</h2><p>No page matches "${escapeHtml(routePath)}".</p>`;
  root.appendChild(div);
}

function renderRouteError(err) {
  clear(root);
  const div = document.createElement("div");
  div.className = "error-banner";
  div.textContent = (err && err.message) || "Something went wrong loading this page.";
  root.appendChild(div);
  // eslint-disable-next-line no-console
  console.error(err);
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function navigate(hash) {
  window.location.hash = hash;
}
