// Tiny shared state — V1 is single-project/single-user (Section 2), so
// the only thing worth caching across pages is which project we're
// operating in, resolved once from GET /projects (never hard-coded, so
// this keeps working even if the backend's fixed local project id ever
// changes).

import { api } from "./api.js";

let projectPromise = null;

export function getProjectId() {
  if (!projectPromise) {
    projectPromise = api.get("/projects").then((projects) => {
      if (!projects || !projects.length) {
        throw new Error("No project is available yet — local bootstrap may not have run.");
      }
      return projects[0].id;
    });
  }
  return projectPromise;
}
