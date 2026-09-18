// Model Developer / Family derivation -- MA5-UI Post-UAT Enhancement 1B.
//
// This is a purely presentational grouping over data the registry
// already exposes: the namespace prefix of canonical_model_id /
// provider_model_id (e.g. "deepseek/deepseek-v3" -> key "deepseek").
// It never creates, implies, or requires a new backend Provider record
// -- OpenRouter remains the one configured Provider throughout; "Model
// Developer" here is a UI-only concept derived from an existing
// identifier, never persisted, never altering model identity.
//
// A small alias table maps a few common namespace keys to the nicer
// label the operator asked for by name (DeepSeek, Google, Nex AGI, ...),
// but any namespace NOT in that table still gets a real, human-readable
// label via a generic, deterministic transform -- never a hardcoded
// per-vendor branch, and never silently dropped into an unlabeled bucket.

const DEVELOPER_LABELS = {
  deepseek: "DeepSeek",
  google: "Google",
  anthropic: "Anthropic",
  openai: "OpenAI",
  nvidia: "NVIDIA",
  qwen: "Qwen",
  "nex-agi": "Nex AGI",
  inclusionai: "InclusionAI",
  meta: "Meta",
  "meta-llama": "Meta",
  mistralai: "Mistral AI",
  mistral: "Mistral AI",
  cohere: "Cohere",
  "z-ai": "Z.AI",
  "x-ai": "xAI",
  liquid: "Liquid AI",
  microsoft: "Microsoft",
  perplexity: "Perplexity",
  ibm: "IBM",
};

export const OTHER_DEVELOPER_KEY = "other";

// Some OpenRouter namespaces carry a leading non-alphanumeric marker
// (observed live: "~openai/gpt-..." alongside plain "openai/gpt-...") --
// stripped generically (not per-vendor) so both group under one developer.
function normalizeNamespace(namespace) {
  return namespace.replace(/^[^a-z0-9]+/i, "").trim().toLowerCase();
}

export function extractDeveloperKey(model) {
  const raw = String((model && (model.canonical_model_id || model.provider_model_id)) || "");
  const slashIndex = raw.indexOf("/");
  if (slashIndex <= 0) return OTHER_DEVELOPER_KEY;
  const namespace = normalizeNamespace(raw.slice(0, slashIndex));
  return namespace || OTHER_DEVELOPER_KEY;
}

export function developerLabel(key) {
  if (!key || key === OTHER_DEVELOPER_KEY) return "Other";
  if (DEVELOPER_LABELS[key]) return DEVELOPER_LABELS[key];
  // Generic, deterministic fallback for any namespace not in the small
  // alias table above: Title Case each hyphen/underscore-separated
  // segment. A real acronym (e.g. "ibm") is handled by adding it to the
  // alias table above, never by a fragile length-based guess here.
  return key
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

// Developers actually present in the given catalog, grouped by their
// NORMALIZED display identity (developerLabel), not by raw namespace key
// -- MA5-UI Post-UAT Enhancement 1C correction. Two raw namespaces that
// alias to the same label (e.g. "meta" and "meta-llama" both -> "Meta")
// must appear as one sidebar entry with a combined count; `keys` carries
// every raw namespace that entry represents, so a caller can select all
// of them together (see ask.js's buildDeveloperSidebar) without ever
// touching persisted provider/model identity. This generalizes to any
// alias collision, not just Meta -- the grouping key is always the
// label, never a hardcoded per-developer special case.
export function uniqueDevelopers(models) {
  const rawCounts = new Map(); // rawKey -> count
  for (const model of models) {
    const key = extractDeveloperKey(model);
    rawCounts.set(key, (rawCounts.get(key) || 0) + 1);
  }

  const byLabel = new Map(); // label -> { label, count, keys: Set<rawKey> }
  for (const [key, count] of rawCounts) {
    const label = developerLabel(key);
    if (!byLabel.has(label)) byLabel.set(label, { label, count: 0, keys: new Set() });
    const entry = byLabel.get(label);
    entry.count += count;
    entry.keys.add(key);
  }

  return [...byLabel.values()].sort((a, b) => a.label.localeCompare(b.label));
}
