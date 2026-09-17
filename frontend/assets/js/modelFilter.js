// Pure model-list filtering/selection helpers for the Ask Agents model
// selector (MA5-UI Post-UAT Enhancement 1) — no DOM, so these are
// unit-testable directly (see frontend/tests/modelFilter.test.mjs).
//
// Every field read here (pricing_classification, provider_id, ...) comes
// straight from the backend's registry metadata
// (app.schemas.providers.ModelCatalogEntryRead) — never inferred from a
// model's name or id. Filtering/selection state are kept strictly
// separate: filtering never mutates what's selected, and selection never
// depends on what the current filter happens to show.

export const PRICING_FILTER_ALL = "all";

export function modelMatchesQuery(model, query) {
  if (!query) return true;
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const haystack = `${model.canonical_model_id || ""} ${model.provider_model_id || ""}`.toLowerCase();
  return haystack.includes(q);
}

export function filterModels(models, { query = "", pricing = PRICING_FILTER_ALL, providerId = "" } = {}) {
  return models.filter((model) => {
    if (pricing !== PRICING_FILTER_ALL && (model.pricing_classification || "unknown") !== pricing) return false;
    if (providerId && model.provider_id !== providerId) return false;
    if (!modelMatchesQuery(model, query)) return false;
    return true;
  });
}

// Only real, distinct providers seen in the registry response — never a
// fabricated/fixed list. A caller should hide the provider filter
// entirely when this returns fewer than 2 entries (single-provider setup
// has nothing meaningful to filter by).
export function uniqueProviderIds(models) {
  return [...new Set(models.map((m) => m.provider_id).filter(Boolean))];
}

// Selected models, in the registry's own order — independent of the
// current search/pricing/provider filter, so a selection never
// disappears just because the operator narrowed the visible list.
export function selectedModelsList(models, selectedIds) {
  return models.filter((m) => selectedIds.has(m.id));
}

export function boundedResults(models, visibleCount) {
  const visible = models.slice(0, visibleCount);
  return { visible, remaining: Math.max(0, models.length - visibleCount) };
}

export function canRunComparison({ question, agentId, selectedCount }) {
  return Boolean(question && question.trim().length > 0 && agentId && selectedCount >= 2);
}
