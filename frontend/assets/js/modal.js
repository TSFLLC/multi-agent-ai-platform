// Minimal overlay/modal utility (MA5-UI Post-UAT Enhancement 2) -- one
// overlay instance appended to <body> on demand, closable via the X
// button, Escape, or a backdrop click. Purely a reading affordance for
// "Expand Answer" (Section 3): it never reads or mutates comparison/
// selection state, never triggers a data reload -- the caller's own page
// state is completely untouched by opening or closing it.

let activeOverlay = null;
let activeKeyHandler = null;
let previousBodyOverflow = null;
let previousFocused = null;

export function isCloseKey(key) {
  return key === "Escape" || key === "Esc";
}

export function isModalOpen() {
  return activeOverlay !== null;
}

export function closeModal() {
  if (!activeOverlay) return;
  activeOverlay.remove();
  activeOverlay = null;

  if (activeKeyHandler) {
    document.removeEventListener("keydown", activeKeyHandler);
    activeKeyHandler = null;
  }

  document.body.style.overflow = previousBodyOverflow || "";
  previousBodyOverflow = null;

  if (previousFocused && typeof previousFocused.focus === "function") {
    previousFocused.focus();
  }
  previousFocused = null;
}

export function openModal(contentNode, { label = "Dialog", size = null } = {}) {
  closeModal(); // never stack two overlays

  previousFocused = document.activeElement;
  previousBodyOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden"; // background must not scroll while the modal is open

  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.setAttribute("role", "dialog");
  backdrop.setAttribute("aria-modal", "true");
  backdrop.setAttribute("aria-label", label);

  const panel = document.createElement("div");
  // `size` is an additive, optional variant (e.g. "large" for the MA5-UI
  // Post-UAT Candidate Focus View) -- omitting it keeps every existing
  // caller's normal-sized modal unchanged.
  panel.className = size ? `modal-panel modal-panel-${size}` : "modal-panel";
  panel.tabIndex = -1;
  panel.appendChild(contentNode);
  backdrop.appendChild(panel);

  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop) closeModal();
  });

  activeKeyHandler = (e) => {
    if (isCloseKey(e.key)) closeModal();
  };
  document.addEventListener("keydown", activeKeyHandler);

  document.body.appendChild(backdrop);
  activeOverlay = backdrop;
  panel.focus();
  return backdrop;
}
