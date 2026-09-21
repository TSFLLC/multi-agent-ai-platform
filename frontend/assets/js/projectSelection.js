// Explicit Project selection for the Workflow pages (MA7.6A).
//
// The rest of the console still resolves its project through
// state.js#getProjectId (which takes projects[0]) and is deliberately left
// alone. The Workflow pages must not silently assume that: a user with several
// projects has to be able to see -- and change -- which one they are working in.
//
// Pure functions plus an injectable storage, so they run under plain Node (see
// frontend/tests/projectSelection.test.mjs). The stored value is only ever a
// project id chosen by the user; it is validated against the projects the API
// says the user can access before it is used.

export const SELECTED_PROJECT_KEY = "map.workflows.selectedProjectId";

// The project to show, or null when the user must choose:
//   * a previously chosen project that is still accessible, else
//   * the only accessible project (visible in the selector, never hidden), else
//   * null -- with several projects nothing is assumed.
export function resolveSelectedProject(projects, storedId) {
  const list = Array.isArray(projects) ? projects : [];
  if (storedId) {
    const stored = list.find((project) => project.id === storedId);
    if (stored) return stored;
  }
  if (list.length === 1) return list[0];
  return null;
}

function defaultStorage() {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function readStoredProjectId(storage = defaultStorage()) {
  try {
    return (storage && storage.getItem(SELECTED_PROJECT_KEY)) || null;
  } catch {
    return null;
  }
}

export function writeStoredProjectId(projectId, storage = defaultStorage()) {
  try {
    if (!storage) return;
    if (projectId) storage.setItem(SELECTED_PROJECT_KEY, projectId);
    else storage.removeItem(SELECTED_PROJECT_KEY);
  } catch {
    // Storage is a convenience only (private mode, quota): selection still works for this page load.
  }
}
