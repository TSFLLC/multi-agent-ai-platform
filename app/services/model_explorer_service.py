"""Model Explorer / Provider Explorer — AIL.1B.

Composes existing facts and evidence into one read-only view; nothing here
is a new source of truth:

* Registry FACTS — ``Provider``/``Model``/``ProviderModel``/
  ``ModelCapability`` (MA2, unchanged) plus the catalog-refresh half of
  ``ProviderModelSnapshot`` (AIL.1B's change-history addition — see
  ``app.services.model_registry_service``).
* PLATFORM OBSERVATIONS — ``model_calls`` / ``evaluation_runs`` /
  ``evaluation_criterion_results`` (MA6) / ``model_routing_decisions``,
  evaluated through ``app.routing_evidence.EvidenceProfile`` — the exact
  MA8.2 reliability/quality computation, reused rather than reimplemented,
  so the Explorer and the Router can never disagree about what "enough
  evidence" means. The aggregation query shape mirrors
  ``app.services.model_intelligence_service.ModelIntelligenceService``
  (module-level ``_since``/``_usage_columns``/``_usage_dict`` helpers are
  imported and reused directly), grouped by model instead of by
  (role, model) — a different GROUP BY over the same tables, not a second
  engine.

Scope note: "our evidence" is single-project, gated by the same
``require_project_access`` RBAC check MA8.3 uses (enforced at the API
layer in ``app.api.routers.model_explorer``) — cross-project aggregation
across an operator's opted-in projects is intentionally deferred. It
depends on ``projects.kind`` / ``projects.ail_evidence_opt_in``, which do
not exist in this repository yet; adding them here risked a migration
collision with whichever AIL.1A slice owns the shared ``projects`` table
(see the AIL.1B completion report's shared-contract blocker). Widening
this service to accept a list of opted-in project ids later is additive.

Later Radar/AIL.2 claim layers (provider claims, benchmarks, research,
community attention) are represented here only as an explicit, honestly
empty ``claims`` placeholder — never fabricated, never a stand-in schema.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.enums import EvaluationFinding, EvaluationRunStatus, ModelCallStatus, SnapshotSource
from app.domain.pricing import classify_pricing
from app.errors import NotFoundError
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.execution import ModelCall
from app.models.providers import (
    Model,
    ModelCapability,
    Provider,
    ProviderCatalogRefresh,
    ProviderModel,
    ProviderModelSnapshot,
)
from app.models.tasks import AgentRun, Task, TaskRun
from app.routing_evidence import PROVIDER_FAILURE_CATEGORIES, EvidenceProfile
from app.services.model_intelligence_service import ModelIntelligenceService, _since, _usage_columns, _usage_dict

MAX_HISTORY_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 25
MAX_WHATS_NEW_LIMIT = 100
DEFAULT_WHATS_NEW_LIMIT = 25


class ModelExplorerService:
    """Read-only. Never writes model_routing_decisions/router_policy_versions
    or any registry table — a plain composition over what already exists."""

    def __init__(self, db: Session):
        self.db = db

    # -- identity / facts -----------------------------------------------------

    @staticmethod
    def _model_identity(model: Model) -> Dict[str, Any]:
        return {
            "id": model.id,
            "canonical_model_id": model.canonical_model_id,
            "family": model.family,
            "context_window": model.context_window,
            "input_modalities": model.input_modalities,
            "output_modalities": model.output_modalities,
            "tool_calling_support": model.tool_calling_support.value if model.tool_calling_support else None,
            "structured_output_support": model.structured_output_support,
            "vision_capability": model.vision_capability,
            "reasoning_tier": model.reasoning_tier,
            "coding_capability_tier": model.coding_capability_tier,
            "status": model.status.value,
            "last_refreshed_at": model.last_refreshed_at,
        }

    def _offerings(self, model_id: str) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            select(ProviderModel, Provider).join(Provider, Provider.id == ProviderModel.provider_id).where(
                ProviderModel.model_id == model_id
            )
        ).all()
        return [
            {
                "provider_model_id": pm.id,
                "provider_id": provider.id,
                "provider_name": provider.name,
                "provider_type": provider.type.value,
                "provider_health_status": provider.health_status.value,
                "cost_input_per_mtok": pm.cost_input_per_mtok,
                "cost_output_per_mtok": pm.cost_output_per_mtok,
                "currency": pm.currency,
                "pricing_classification": classify_pricing(
                    pm.cost_input_per_mtok, pm.cost_output_per_mtok
                ).value,
                "availability_status": pm.availability_status.value,
                "last_refreshed_at": pm.last_refreshed_at,
            }
            for pm, provider in rows
        ]

    def _capabilities(self, model_id: str) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            select(ModelCapability).where(ModelCapability.model_id == model_id)
        ).scalars().all()
        return [{"key": r.capability_key, "value": r.value, "tier": r.tier} for r in rows]

    def list_models(self) -> List[Dict[str, Any]]:
        rows = self.db.execute(select(Model).order_by(Model.canonical_model_id)).scalars().all()
        return [self._model_identity(m) for m in rows]

    def get_model(self, model_id: str, *, project_id: Optional[str] = None) -> Dict[str, Any]:
        model = self.db.get(Model, model_id)
        if model is None:
            raise NotFoundError(f"Model {model_id} not found.")
        offerings = self._offerings(model_id)
        pm_ids = [o["provider_model_id"] for o in offerings]
        return {
            "identity": self._model_identity(model),
            "availability": offerings,
            "capabilities": self._capabilities(model_id),
            "our_evidence": self._our_evidence(project_id, pm_ids) if project_id else {
                "status": "no_project_selected"
            },
            # AIL.2/Radar claim layers (provider claims, benchmarks, research,
            # community attention) are not built yet — an honest empty seam,
            # never fabricated data.
            "claims": {"status": "not_available_yet", "layers": []},
        }

    def get_model_history(self, model_id: str, *, limit: int = DEFAULT_HISTORY_LIMIT) -> Dict[str, Any]:
        model = self.db.get(Model, model_id)
        if model is None:
            raise NotFoundError(f"Model {model_id} not found.")
        limit = max(1, min(limit, MAX_HISTORY_LIMIT))
        rows = self.db.execute(
            select(ProviderModelSnapshot, Provider.name)
            .join(Provider, Provider.id == ProviderModelSnapshot.provider_id)
            .where(
                ProviderModelSnapshot.model_id == model_id,
                ProviderModelSnapshot.source == SnapshotSource.CATALOG_REFRESH,
            )
            .order_by(ProviderModelSnapshot.snapshotted_at.desc(), ProviderModelSnapshot.id.desc())
            .limit(limit)
        ).all()
        return {
            "limit": limit,
            "items": [self._history_entry(snap, provider_name) for snap, provider_name in rows],
        }

    @staticmethod
    def _history_entry(snap: ProviderModelSnapshot, provider_name: str) -> Dict[str, Any]:
        return {
            "snapshotted_at": snap.snapshotted_at,
            "provider_id": snap.provider_id,
            "provider_name": provider_name,
            "change_kinds": snap.change_kinds,
            "pricing_input_per_mtok": snap.pricing_input_per_mtok,
            "pricing_output_per_mtok": snap.pricing_output_per_mtok,
            "currency": snap.currency,
            "pricing_classification": snap.pricing_classification.value,
            "context_window": snap.context_window,
            "capability_snapshot": snap.capability_snapshot,
        }

    def whats_new(
        self, *, provider_id: Optional[str] = None, limit: int = DEFAULT_WHATS_NEW_LIMIT
    ) -> Dict[str, Any]:
        limit = max(1, min(limit, MAX_WHATS_NEW_LIMIT))
        stmt = (
            select(ProviderModelSnapshot, Model.canonical_model_id, Provider.name)
            .join(Model, Model.id == ProviderModelSnapshot.model_id)
            .join(Provider, Provider.id == ProviderModelSnapshot.provider_id)
            .where(ProviderModelSnapshot.source == SnapshotSource.CATALOG_REFRESH)
        )
        if provider_id:
            stmt = stmt.where(ProviderModelSnapshot.provider_id == provider_id)
        rows = self.db.execute(
            stmt.order_by(ProviderModelSnapshot.snapshotted_at.desc(), ProviderModelSnapshot.id.desc()).limit(
                limit
            )
        ).all()
        return {
            "limit": limit,
            "items": [
                {
                    **self._history_entry(snap, provider_name),
                    "model_id": snap.model_id,
                    "canonical_model_id": canonical_model_id,
                }
                for snap, canonical_model_id, provider_name in rows
            ],
        }

    def compare(self, model_ids: List[str], *, project_id: Optional[str] = None) -> Dict[str, Any]:
        """A simple composed read over 2-4 models — no scoring, no verdict,
        just each model's own facts/evidence side by side."""
        if not (2 <= len(model_ids) <= 4):
            raise ValueError("compare accepts between 2 and 4 model_ids")
        return {"items": [self.get_model(model_id, project_id=project_id) for model_id in model_ids]}

    # -- our evidence (platform observations; single-project, RBAC-gated by caller) --

    def _our_evidence(self, project_id: str, provider_model_ids: List[str]) -> Dict[str, Any]:
        if not provider_model_ids:
            return {"status": "not_in_registry", "by_provider_model": {}}

        config = ModelIntelligenceService(self.db)._display_config()
        since = _since()
        pm_ids = sorted(set(provider_model_ids))

        usage_rows = self.db.execute(
            self._scoped(select(ModelCall.provider_model_id, *_usage_columns()), project_id)
            .where(
                ModelCall.provider_model_id.in_(pm_ids),
                ModelCall.started_at >= since,
                ModelCall.status.in_([ModelCallStatus.SUCCESS, ModelCallStatus.ERROR, ModelCallStatus.TIMEOUT]),
            )
            .group_by(ModelCall.provider_model_id)
        ).all()
        usage_by_pm = {row[0]: row[1:] for row in usage_rows}

        failure_rows = self.db.execute(
            self._scoped(select(ModelCall.provider_model_id, ModelCall.status, ModelCall.error), project_id).where(
                ModelCall.provider_model_id.in_(pm_ids),
                ModelCall.started_at >= since,
                ModelCall.status != ModelCallStatus.SUCCESS,
            )
        ).all()
        provider_failures: Dict[str, int] = {}
        for pm_id, status, error in failure_rows:
            category = (error or {}).get("category") if isinstance(error, dict) else None
            if status == ModelCallStatus.TIMEOUT or category in PROVIDER_FAILURE_CATEGORIES:
                provider_failures[pm_id] = provider_failures.get(pm_id, 0) + 1

        findings_rows = self.db.execute(
            self._scoped(
                select(
                    ModelCall.provider_model_id,
                    func.count(func.distinct(EvaluationRun.id)),
                    func.sum(case((EvaluationCriterionResult.finding == EvaluationFinding.MET, 1), else_=0)),
                    func.sum(case((EvaluationCriterionResult.finding == EvaluationFinding.PARTIAL, 1), else_=0)),
                    func.sum(case((EvaluationCriterionResult.finding == EvaluationFinding.NOT_MET, 1), else_=0)),
                    func.sum(
                        case(
                            (EvaluationCriterionResult.finding == EvaluationFinding.NOT_APPLICABLE, 1), else_=0
                        )
                    ),
                ).join(EvaluationRun, EvaluationRun.subject_agent_run_id == ModelCall.agent_run_id).join(
                    EvaluationCriterionResult, EvaluationCriterionResult.evaluation_run_id == EvaluationRun.id
                ),
                project_id,
            )
            .where(
                ModelCall.provider_model_id.in_(pm_ids),
                ModelCall.started_at >= since,
                ModelCall.status == ModelCallStatus.SUCCESS,
                EvaluationRun.status == EvaluationRunStatus.COMPLETED,
            )
            .group_by(ModelCall.provider_model_id)
        ).all()
        findings = {
            pm_id: {
                "evaluated_runs": int(runs or 0),
                "met": int(met or 0),
                "partial": int(partial or 0),
                "not_met": int(not_met or 0),
                "not_applicable": int(na or 0),
            }
            for pm_id, runs, met, partial, not_met, na in findings_rows
        }

        decision_rows = self.db.execute(
            self._scoped(
                select(ModelCall.provider_model_id, func.count(func.distinct(ModelCall.model_routing_decision_id))),
                project_id,
            ).where(
                ModelCall.provider_model_id.in_(pm_ids),
                ModelCall.model_routing_decision_id.isnot(None),
            ).group_by(ModelCall.provider_model_id)
        ).all()
        decision_counts = {pm_id: int(count) for pm_id, count in decision_rows}

        by_pm: Dict[str, Any] = {}
        for pm_id in pm_ids:
            router_selection_count = decision_counts.get(pm_id, 0)
            if pm_id not in usage_by_pm:
                by_pm[pm_id] = {
                    "status": "no_data",
                    "reason": "not_enough_history",
                    "router_selection_count": router_selection_count,
                }
                continue
            usage = _usage_dict(usage_by_pm[pm_id])
            found = findings.get(pm_id, {})
            profile = EvidenceProfile(
                provider_model_id=pm_id,
                completed=usage["completed_calls"],
                provider_failures=provider_failures.get(pm_id, 0),
                other_failures=usage["model_calls"] - usage["completed_calls"] - provider_failures.get(pm_id, 0),
                evaluated_runs=found.get("evaluated_runs", 0),
                met=found.get("met", 0),
                partial=found.get("partial", 0),
                not_met=found.get("not_met", 0),
                not_applicable=found.get("not_applicable", 0),
            )
            by_pm[pm_id] = {
                "status": "ok",
                "window_days": config.history_window_days,
                **usage,
                "reliability": profile.reliability(config),
                "quality": profile.quality(config),
                "evaluated_runs": profile.evaluated_runs,
                "findings": {
                    "met": profile.met,
                    "partial": profile.partial,
                    "not_met": profile.not_met,
                    "not_applicable": profile.not_applicable,
                },
                "router_selection_count": router_selection_count,
            }
        return {"status": "ok", "by_provider_model": by_pm}

    @staticmethod
    def _scoped(stmt, project_id: str):
        return (
            stmt.select_from(ModelCall)
            .join(AgentRun, AgentRun.id == ModelCall.agent_run_id)
            .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
            .join(Task, Task.id == TaskRun.task_id)
            .where(Task.project_id == project_id)
        )


class ProviderExplorerService:
    """Read-only, thin — no second Provider Registry. Everything here is a
    filtered/rolled-up view over Provider/ProviderModel/ProviderCatalogRefresh,
    already owned by MA2."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _identity(provider: Provider) -> Dict[str, Any]:
        return {
            "id": provider.id,
            "type": provider.type.value,
            "name": provider.name,
            "base_url": provider.base_url,
            "health_status": provider.health_status.value,
        }

    def list_providers(self) -> List[Dict[str, Any]]:
        rows = self.db.execute(select(Provider).order_by(Provider.name)).scalars().all()
        return [self._identity(p) for p in rows]

    def get_provider(self, provider_id: str) -> Dict[str, Any]:
        provider = self.db.get(Provider, provider_id)
        if provider is None:
            raise NotFoundError(f"Provider {provider_id} not found.")

        rows = self.db.execute(
            select(ProviderModel, Model).join(Model, Model.id == ProviderModel.model_id).where(
                ProviderModel.provider_id == provider_id
            )
        ).all()
        models = [
            {
                "provider_model_id": pm.id,
                "model_id": model.id,
                "canonical_model_id": model.canonical_model_id,
                "cost_input_per_mtok": pm.cost_input_per_mtok,
                "cost_output_per_mtok": pm.cost_output_per_mtok,
                "currency": pm.currency,
                "pricing_classification": classify_pricing(
                    pm.cost_input_per_mtok, pm.cost_output_per_mtok
                ).value,
                "availability_status": pm.availability_status.value,
                "model_status": model.status.value,
                "last_refreshed_at": pm.last_refreshed_at,
            }
            for pm, model in rows
        ]
        refresh_rows = self.db.execute(
            select(ProviderCatalogRefresh)
            .where(ProviderCatalogRefresh.provider_id == provider_id)
            .order_by(ProviderCatalogRefresh.started_at.desc())
            .limit(10)
        ).scalars().all()
        last_refreshed_values = [m["last_refreshed_at"] for m in models if m["last_refreshed_at"]]
        return {
            "identity": self._identity(provider),
            "models": models,
            "freshness": {
                "last_model_refresh_at": max(last_refreshed_values) if last_refreshed_values else None,
                "last_refresh_attempt": (
                    self._refresh_summary(refresh_rows[0]) if refresh_rows else None
                ),
            },
            "recent_refreshes": [self._refresh_summary(r) for r in refresh_rows],
        }

    @staticmethod
    def _refresh_summary(refresh: ProviderCatalogRefresh) -> Dict[str, Any]:
        return {
            "id": refresh.id,
            "started_at": refresh.started_at,
            "completed_at": refresh.completed_at,
            "status": refresh.status.value,
            "models_discovered": refresh.models_discovered,
            "models_added": refresh.models_added,
            "models_updated": refresh.models_updated,
            "models_unavailable": refresh.models_unavailable,
            "error": refresh.error,
        }
