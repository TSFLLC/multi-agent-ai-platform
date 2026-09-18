// Shared Agent / AgentVersion directory lookups -- MA5-UI Post-UAT
// Enhancement 1C.
//
// Architecture note (investigated before writing any of this): "Role" is
// NOT a separate backend entity. It is AgentVersion itself --
// app.services.agent_registry_service.publish_agent_version never
// deprecates sibling versions (its own transition check only looks at
// the target version's current status), so one persistent Agent can
// already have multiple simultaneously-ACTIVE AgentVersion rows, each
// carrying its own independent, genuinely-executable role/name/
// prompt_version_id/model_policy. This module resolves that existing
// structure through the existing GET /agents + GET /agents/{id}/versions
// endpoints -- it invents no schema, no backend field, no new concept.
// Today most Agents only have one published version (so their Role
// dropdown will show exactly one real option), but the mechanism itself
// is real: publishing a second AgentVersion for the same Agent (already
// possible via the existing Agent Registry API) makes a second Role
// immediately selectable here, with zero further changes.

import { api } from "./api.js";

export async function loadAgentCatalog(projectId) {
  const agents = await api.get(`/agents?project_id=${encodeURIComponent(projectId)}`);
  return Promise.all(
    agents.map(async (agent) => {
      const versions = await api.get(`/agents/${agent.id}/versions`);
      return { agent, versions };
    })
  );
}

export function activeVersionsOf(entry) {
  if (!entry) return [];
  return entry.versions.filter((v) => v.status === "active").sort((a, b) => a.version - b.version);
}

// The human-readable "Role" label for one AgentVersion -- its own role
// field first (what every starter Agent sets), falling back to the
// version's name, and finally to a bare version number so this never
// renders blank for a version that somehow has neither.
export function roleLabelFor(version) {
  if (!version) return "—";
  return version.role || version.name || `v${version.version}`;
}

// agent_version_id -> { agent, version } -- built once per page load so
// a comparison candidate's agent_version_id can be resolved back to a
// human-readable Agent name + Role without a new backend field.
export function buildVersionIndex(catalog) {
  const index = new Map();
  for (const entry of catalog) {
    for (const version of entry.versions) {
      index.set(version.id, { agent: entry.agent, version });
    }
  }
  return index;
}
