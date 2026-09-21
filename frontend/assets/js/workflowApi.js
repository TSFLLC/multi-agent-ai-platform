// Typed-ish fetch wrappers for the Workflow Studio (MA7.6A). Every call goes
// through api.js, so it carries the same local Bearer token and the same error
// contract ({ error: { code, message, detail } } -> ApiError). Nothing here is a
// frontend-only fake: each function is one existing (or MA7.6A-added) endpoint.
//
// Endpoint contracts worth knowing:
//   * POST .../edges takes from_node_id / to_node_id as QUERY parameters.
//   * PATCH .../nodes/{id} replaces the node's `config` (draft versions only).
//   * POST .../validate is a dry run of publish validation ({ valid, issues }).
//   * POST .../clone creates a new DRAFT version from any version, atomically.
//   * POST .../runs takes exactly one of { task_run_id } or { task_id }; with
//     task_id the run's parent TaskRun is created for us (no stray AgentRun).

import { api } from "./api.js";

const enc = encodeURIComponent;

export function edgeQuery(fromNodeId, toNodeId) {
  return `from_node_id=${enc(fromNodeId)}&to_node_id=${enc(toNodeId)}`;
}

export function newIdempotencyKey() {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  } catch {
    // fall through
  }
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

const versionPath = (workflowId, version) => `/workflows/${enc(workflowId)}/versions/${enc(version)}`;

export const workflowApi = {
  listProjects: () => api.get("/projects"),
  listWorkflows: (projectId) => api.get(`/workflows?project_id=${enc(projectId)}`),
  getWorkflow: (workflowId) => api.get(`/workflows/${enc(workflowId)}`),
  createWorkflow: (projectId, name) => api.post("/workflows", { project_id: projectId, name }),
  listVersions: (workflowId) => api.get(`/workflows/${enc(workflowId)}/versions`),
  getGraph: (workflowId, version) => api.get(`${versionPath(workflowId, version)}/graph`),

  addNode: (workflowId, version, payload) => api.post(`${versionPath(workflowId, version)}/nodes`, payload),
  patchNode: (workflowId, version, nodeId, config) =>
    api.raw(`${versionPath(workflowId, version)}/nodes/${enc(nodeId)}`, { method: "PATCH", body: { config } }),
  deleteNode: (workflowId, version, nodeId) =>
    api.raw(`${versionPath(workflowId, version)}/nodes/${enc(nodeId)}`, { method: "DELETE" }),
  // NO request body: on this route a JSON body is read as the edge's `condition`,
  // and api.post would send "{}" -- an (empty but non-null) condition, which the
  // validator rejects because conditional routing is not supported.
  addEdge: (workflowId, version, fromNodeId, toNodeId) =>
    api.raw(`${versionPath(workflowId, version)}/edges?${edgeQuery(fromNodeId, toNodeId)}`, { method: "POST" }),
  deleteEdge: (workflowId, version, edgeId) =>
    api.raw(`${versionPath(workflowId, version)}/edges/${enc(edgeId)}`, { method: "DELETE" }),

  validate: (workflowId, version) => api.post(`${versionPath(workflowId, version)}/validate`),
  publish: (workflowId, version) => api.post(`${versionPath(workflowId, version)}/publish`),
  cloneVersion: (workflowId, version) => api.post(`${versionPath(workflowId, version)}/clone`),

  // The assignment is a Task in "workflow" mode; starting the run then creates its
  // parent TaskRun (and nothing else) on the server.
  createTask: ({ projectId, title, description }) =>
    api.post("/tasks", {
      project_id: projectId,
      title,
      description: description || null,
      execution_mode: "workflow",
    }),
  startRun: (workflowId, version, taskId, idempotencyKey) =>
    api.post(
      `${versionPath(workflowId, version)}/runs`,
      { task_id: taskId },
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : {}
    ),
  getRun: (runId) => api.get(`/workflow-runs/${enc(runId)}`),

  getPromptVersions: (agentId) => api.get(`/agents/${enc(agentId)}/prompt-versions`),
};
