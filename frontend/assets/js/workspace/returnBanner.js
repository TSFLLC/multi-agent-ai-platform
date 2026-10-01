// AIL5D.6 — "Back to your lesson". When a learner leaves the Workspace for the Personal Lab (a run's page), the SERVER-built return
// path (from the practice handoff) is remembered for the tab; this shows one button that restores the exact Day, step and Practice
// Instance. Only an Academy Level 1 path is ever followed (safeReturnHash), so no stored value can send the learner elsewhere.

import { el } from "../dom.js";
import { safeReturnHash } from "./model.js";

const KEY = "ail5d6.return";

function read() {
  try { return sessionStorage.getItem(KEY); } catch { return null; }
}

export function installReturnBanner() {
  const paint = () => {
    document.getElementById("ail5d-return")?.remove();
    const stored = read();
    const hash = window.location.hash || "";
    if (!stored || hash.startsWith("#/academy/level-1")) {
      if (stored && hash.startsWith("#/academy/level-1")) { try { sessionStorage.removeItem(KEY); } catch { /* ignore */ } }
      return;
    }
    document.body.appendChild(el("button", { id: "ail5d-return", type: "button", onclick: () => {
      const target = safeReturnHash(stored);
      try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
      window.location.hash = target;
    } }, "← Back to your lesson"));
  };
  window.addEventListener("hashchange", paint);
  paint();
}
