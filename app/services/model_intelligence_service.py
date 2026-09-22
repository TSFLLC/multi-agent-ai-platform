"""Model Intelligence — MA8.3 read model + evidence-routing control.

Operator visibility over data the platform already records — nothing here is
a new source of truth and nothing here routes:

* ``model_calls`` (per provider invocation: outcome, error category, tokens,
  cost + its exact/estimated flag, latency),
* ``evaluation_runs`` / ``evaluation_criterion_results`` (MA6 findings),
* ``model_routing_decisions`` (MA8.1/MA8.2 decision-time records),
* ``router_policy_versions`` (the MA8.2 evidence policy).

Every read is scoped to one project (``tasks.project_id``) and bounded: a
fixed recent window (the MA8.2 v1 history window) plus row limits, with
aggregation done in SQL. Evidence standings reuse
``app.routing_evidence.EvidenceProfile`` so the dashboard and the router can
never disagree about what "enough evidence" means.

Payloads carry identifiers, names, counts, categories and the persisted
routing explanation only — never prompts, artifact content, evaluation
rationales, raw provider error messages, credentials or secret references.

Evidence routing on/off is ``router_policy_versions.status`` — platform-wide,
like the provider/model registry. Enabling activates the installed
``ma8.2-evidence-v1`` version (never creating one); disabling leaves no
version active. Neither deletes any history.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.db.enums import (
    EvaluationFinding,
    EvaluationRunStatus,
    ModelCallStatus,
    ModelSelectionMode,
)
from app.errors import ConflictError, NotFoundError
from app.models.agents import Agent, AgentVersion
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.execution import ModelCall, ModelRoutingDecision, RouterPolicyVersion
from app.models.providers import Model, Provider, ProviderModel
from app.models.tasks import AgentRun, Task, TaskRun
from app.routing_evidence import (
    DEFAULT_V1_CONFIG,
    EVIDENCE_STRATEGY_V1,
    PROVIDER_FAILURE_CATEGORIES,
    EvidenceConfig,
    EvidenceConfigError,
    EvidenceProfile,
)
from app.services.audit_service import AuditService

WINDOW_DAYS = DEFAULT_V1_CONFIG["history_window_days"]
MAX_ROLE_MODEL_ROWS = 200
MAX_FAILURE_ROWS = 5000
MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 25

POLICY_ACTIVE = "active"
POLICY_INACTIVE = "inactive"

# Fixed, human-readable labels for ModelCall error categories. The raw
# provider message is never exposed.
ISSUE_LABELS = {
    "provider_timeout": "Provider timed out",
    "provider_connection_error": "Could not connect to the provider",
    "provider_invalid_response": "Provider returned an invalid or empty response",
    "provider_authentication_error": "Provider rejected the credentials",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _since() -> datetime:
    return _utcnow() - timedelta(days=WINDOW_DAYS)


def _category(status, error) -> Optional[str]:
    if isinstance(error, dict) and isinstance(error.get("category"), str):
        return error["category"]
    return "provider_timeout" if status == ModelCallStatus.TIMEOUT else None


def issue_kind(category: Optional[str]) -> str:
    """``reliability`` for provider/infrastructure failures that MA8.2
    counts; ``configuration`` for credential problems; ``other`` otherwise.
    None of these is quality evidence."""
    if category in PROVIDER_FAILURE_CATEGORIES:
        return "reliability"
    if category == "provider_authentication_error":
        return "configuration"
    return "other"


def _scoped_calls(project_id: str, since: datetime):
    """Base join for a project's model calls in the window."""
    return (
        select()
        .select_from(ModelCall)
        .join(AgentRun, AgentRun.id == ModelCall.agent_run_id)
        .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
        .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            Task.project_id == project_id,
            ModelCall.started_at >= since,
            ModelCall.status.in_([ModelCallStatus.SUCCESS, ModelCallStatus.ERROR, ModelCallStatus.TIMEOUT]),
        )
    )


def _usage_columns():
    success = ModelCall.status == ModelCallStatus.SUCCESS
    return (
        func.count(ModelCall.id),
        func.sum(case((success, 1), else_=0)),
        func.coalesce(func.sum(ModelCall.tokens_in), 0),
        func.coalesce(func.sum(ModelCall.tokens_out), 0),
        func.sum(case((ModelCall.cost_is_estimated.is_(False), ModelCall.cost_amount), else_=None)),
        func.sum(case((ModelCall.cost_is_estimated.is_(True), ModelCall.cost_amount), else_=None)),
        func.sum(case((success & ModelCall.cost_amount.is_(None), 1), else_=0)),
        func.sum(
            case((ModelCall.cost_is_estimated.is_(False) & ModelCall.cost_amount.isnot(None), 1), else_=0)
        ),
        func.sum(
            case((ModelCall.cost_is_estimated.is_(True) & ModelCall.cost_amount.isnot(None), 1), else_=0)
        ),
    )


def _usage_dict(row) -> Dict[str, Any]:
    calls, completed, t_in, t_out, exact, estimated, unknown, exact_n, estimated_n = row
    return {
        "model_calls": int(calls or 0),
        "completed_calls": int(completed or 0),
        "tokens_in": int(t_in or 0),
        "tokens_out": int(t_out or 0),
        "tokens_total": int(t_in or 0) + int(t_out or 0),
        "cost": {
            # Sums only over calls whose cost is known; ``unknown_calls`` are
            # successful calls with no recorded cost — never counted as $0.
            "exact_usd": str(exact) if exact is not None else None,
            "exact_calls": int(exact_n or 0),
            "estimated_usd": str(estimated) if estimated is not None else None,
            "estimated_calls": int(estimated_n or 0),
            "unknown_calls": int(unknown or 0),
        },
    }


class ModelIntelligenceService:
    def __init__(self, db: Session):
        self.db = db

    # -- summary ---------------------------------------------------------------

    def summary(self, project_id: str) -> Dict[str, Any]:
        since = _since()
        usage = self.db.execute(_scoped_calls(project_id, since).add_columns(*_usage_columns())).one()
        failures = self._failure_counts(project_id, since)
        agent_runs = self.db.execute(
            select(func.count(AgentRun.id))
            .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
            .join(Task, Task.id == TaskRun.task_id)
            .where(Task.project_id == project_id, AgentRun.created_at >= since)
        ).scalar_one()
        evaluated_runs = self.db.execute(
            select(func.count(EvaluationRun.id))
            .join(AgentRun, AgentRun.id == EvaluationRun.subject_agent_run_id)
            .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
            .join(Task, Task.id == TaskRun.task_id)
            .where(
                Task.project_id == project_id,
                EvaluationRun.status == EvaluationRunStatus.COMPLETED,
                EvaluationRun.created_at >= since,
            )
        ).scalar_one()
        return {
            "window_days": WINDOW_DAYS,
            "since": since,
            "agent_runs": int(agent_runs or 0),
            **_usage_dict(usage),
            "failed_calls": sum(failures.values()),
            "reliability_issues": sum(v for k, v in failures.items() if issue_kind(k) == "reliability"),
            "failures_by_category": failures,
            "evaluated_runs": int(evaluated_runs or 0),
            "evidence_policy": self.policy_status(),
        }

    def _failures(self, project_id: str, since: datetime):
        """(role, provider_model_id, category) for the window's failed calls,
        newest first and capped. The category is read from ``error_json`` in
        Python: SQLite builds without JSON1 (e.g. the local Windows runtime)
        cannot evaluate JSON_EXTRACT in SQL."""
        rows = self.db.execute(
            _scoped_calls(project_id, since)
            .add_columns(AgentVersion.role, ModelCall.provider_model_id, ModelCall.status, ModelCall.error)
            .where(ModelCall.status != ModelCallStatus.SUCCESS)
            .order_by(ModelCall.started_at.desc(), ModelCall.id)
            .limit(MAX_FAILURE_ROWS)
        ).all()
        return [
            (role, pm_id, _category(status, error) or "unknown_error") for role, pm_id, status, error in rows
        ]

    def _failure_counts(self, project_id: str, since: datetime) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for _role, _pm_id, category in self._failures(project_id, since):
            counts[category] = counts.get(category, 0) + 1
        return dict(sorted(counts.items()))

    # -- role x model ------------------------------------------------------------

    def role_models(self, project_id: str) -> Dict[str, Any]:
        since = _since()
        config = self._display_config()
        usage_rows = self.db.execute(
            _scoped_calls(project_id, since)
            .add_columns(AgentVersion.role, ModelCall.provider_model_id, func.max(ModelCall.started_at))
            .add_columns(*_usage_columns())
            .add_columns(
                func.avg(
                    case((ModelCall.status == ModelCallStatus.SUCCESS, ModelCall.latency_ms), else_=None)
                )
            )
            .group_by(AgentVersion.role, ModelCall.provider_model_id)
            .order_by(AgentVersion.role, func.count(ModelCall.id).desc(), ModelCall.provider_model_id)
            .limit(MAX_ROLE_MODEL_ROWS)
        ).all()

        keys = [(row[0], row[1]) for row in usage_rows]
        provider_failure_counts: Dict[Any, int] = {}
        for role, pm_id, category in self._failures(project_id, since):
            if category in PROVIDER_FAILURE_CATEGORIES:
                provider_failure_counts[(role, pm_id)] = provider_failure_counts.get((role, pm_id), 0) + 1
        findings = self._findings(project_id, since, keys)
        names = self._model_names({pm_id for _role, pm_id in keys})

        entries = []
        for row in usage_rows:
            role, pm_id, last_at = row[0], row[1], row[2]
            usage = _usage_dict(row[3:12])
            provider_failures, avg_latency = provider_failure_counts.get((role, pm_id), 0), row[12]
            found = findings.get((role, pm_id), {})
            profile = EvidenceProfile(
                provider_model_id=pm_id,
                completed=usage["completed_calls"],
                provider_failures=provider_failures,
                other_failures=usage["model_calls"] - usage["completed_calls"] - provider_failures,
                evaluated_runs=found.get("evaluated_runs", 0),
                met=found.get("met", 0),
                partial=found.get("partial", 0),
                not_met=found.get("not_met", 0),
                not_applicable=found.get("not_applicable", 0),
            )
            entries.append(
                {
                    "agent_role": role,
                    **names.get(pm_id, {"provider_model_id": pm_id}),
                    **usage,
                    "provider_failures": provider_failures,
                    "other_failures": profile.other_failures,
                    "average_latency_ms": int(avg_latency) if avg_latency is not None else None,
                    "last_used_at": last_at,
                    "evaluated_runs": profile.evaluated_runs,
                    "findings": {
                        "met": profile.met,
                        "partial": profile.partial,
                        "not_met": profile.not_met,
                        "not_applicable": profile.not_applicable,
                    },
                    "reliability": profile.reliability(config),
                    "quality": profile.quality(config),
                }
            )
        return {
            "window_days": WINDOW_DAYS,
            "since": since,
            "thresholds": {
                "min_observations": config.min_observations,
                "min_evaluated_runs": config.min_evaluated_runs,
                "max_provider_failure_rate": config.max_provider_failure_rate,
                "positive_met_share": config.positive_met_share,
                "negative_not_met_share": config.negative_not_met_share,
            },
            "truncated": len(usage_rows) >= MAX_ROLE_MODEL_ROWS,
            "entries": entries,
        }

    def _findings(self, project_id: str, since: datetime, keys) -> Dict[Any, Dict[str, int]]:
        if not keys:
            return {}
        pm_ids = sorted({pm_id for _role, pm_id in keys})
        rows = self.db.execute(
            select(
                AgentVersion.role,
                ModelCall.provider_model_id,
                func.count(func.distinct(EvaluationRun.id)),
                func.sum(case((EvaluationCriterionResult.finding == EvaluationFinding.MET, 1), else_=0)),
                func.sum(case((EvaluationCriterionResult.finding == EvaluationFinding.PARTIAL, 1), else_=0)),
                func.sum(case((EvaluationCriterionResult.finding == EvaluationFinding.NOT_MET, 1), else_=0)),
                func.sum(
                    case((EvaluationCriterionResult.finding == EvaluationFinding.NOT_APPLICABLE, 1), else_=0)
                ),
            )
            .select_from(ModelCall)
            .join(EvaluationRun, EvaluationRun.subject_agent_run_id == ModelCall.agent_run_id)
            .join(EvaluationCriterionResult, EvaluationCriterionResult.evaluation_run_id == EvaluationRun.id)
            .join(AgentRun, AgentRun.id == ModelCall.agent_run_id)
            .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
            .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
            .join(Task, Task.id == TaskRun.task_id)
            .where(
                Task.project_id == project_id,
                ModelCall.provider_model_id.in_(pm_ids),
                ModelCall.started_at >= since,
                ModelCall.status == ModelCallStatus.SUCCESS,
                EvaluationRun.status == EvaluationRunStatus.COMPLETED,
            )
            .group_by(AgentVersion.role, ModelCall.provider_model_id)
        ).all()
        return {
            (role, pm_id): {
                "evaluated_runs": int(runs or 0),
                "met": int(met or 0),
                "partial": int(partial or 0),
                "not_met": int(not_met or 0),
                "not_applicable": int(na or 0),
            }
            for role, pm_id, runs, met, partial, not_met, na in rows
        }

    def _model_names(self, pm_ids) -> Dict[str, Dict[str, Any]]:
        if not pm_ids:
            return {}
        rows = self.db.execute(
            select(ProviderModel.id, Model.canonical_model_id, Provider.name)
            .join(Model, Model.id == ProviderModel.model_id)
            .join(Provider, Provider.id == ProviderModel.provider_id)
            .where(ProviderModel.id.in_(sorted(pm_ids)))
        ).all()
        return {
            pm_id: {"provider_model_id": pm_id, "canonical_model_id": canonical, "provider_name": provider}
            for pm_id, canonical, provider in rows
        }

    # -- recent issues -------------------------------------------------------------

    def recent_issues(self, project_id: str, limit: int = DEFAULT_LIST_LIMIT) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, MAX_LIST_LIMIT))
        rows = self.db.execute(
            _scoped_calls(project_id, _since())
            .add_columns(
                ModelCall.started_at,
                ModelCall.status,
                ModelCall.error,
                ModelCall.provider_model_id,
                ModelCall.agent_run_id,
                AgentVersion.role,
                Agent.name,
            )
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(ModelCall.status != ModelCallStatus.SUCCESS)
            .order_by(ModelCall.started_at.desc(), ModelCall.id)
            .limit(limit)
        ).all()
        names = self._model_names({row[3] for row in rows if row[3]})
        issues = []
        for started_at, status, error, pm_id, agent_run_id, role, agent_name in rows:
            category = _category(status, error)
            issues.append(
                {
                    "occurred_at": started_at,
                    "agent_role": role,
                    "agent_name": agent_name,
                    "agent_run_id": agent_run_id,
                    **names.get(pm_id, {"provider_model_id": pm_id}),
                    "category": category,
                    "label": ISSUE_LABELS.get(category, "Model call failed"),
                    "kind": issue_kind(category),
                }
            )
        return issues

    # -- routing decisions ---------------------------------------------------------

    def _scoped_decisions(self, project_id: str):
        return (
            select(ModelRoutingDecision, AgentVersion.role, Agent.name, AgentRun.task_run_id)
            .join(AgentRun, AgentRun.id == ModelRoutingDecision.agent_run_id)
            .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
            .join(Task, Task.id == TaskRun.task_id)
            .where(Task.project_id == project_id)
        )

    def list_decisions(
        self,
        project_id: str,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        agent_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        limit = max(1, min(limit, MAX_LIST_LIMIT))
        offset = max(0, offset)
        stmt = self._scoped_decisions(project_id)
        if agent_run_id:
            stmt = stmt.where(ModelRoutingDecision.agent_run_id == agent_run_id)
        rows = self.db.execute(
            stmt.order_by(ModelRoutingDecision.created_at.desc(), ModelRoutingDecision.id)
            .offset(offset)
            .limit(limit + 1)
        ).all()
        items = [self._decision_summary(*row) for row in rows[:limit]]
        return {"items": items, "limit": limit, "offset": offset, "has_more": len(rows) > limit}

    def get_decision(self, project_id: str, decision_id: str) -> Dict[str, Any]:
        row = self.db.execute(
            self._scoped_decisions(project_id).where(ModelRoutingDecision.id == decision_id)
        ).first()
        if row is None:
            # Same answer for "does not exist" and "belongs to another project".
            raise NotFoundError(f"Routing decision {decision_id} not found.")
        decision, role, agent_name, task_run_id = row
        details = decision.details or {}
        eligible = list(decision.eligible_candidates or [])
        names = {c.get("provider_model_id"): c.get("canonical_model_id") for c in eligible}
        names.update(
            {
                c.get("provider_model_id"): c.get("canonical_model_id")
                for c in details.get("excluded_candidates") or []
            }
        )
        evidence = details.get("evidence")
        evidence_view = None
        if isinstance(evidence, dict):
            pick_id = evidence.get("deterministic_pick_provider_model_id")
            profiles = []
            for profile in evidence.get("profiles") or []:
                profiles.append(
                    {**profile, "canonical_model_id": names.get(profile.get("provider_model_id"))}
                )
            evidence_view = {
                "status": evidence.get("status"),
                "strategy": evidence.get("strategy"),
                "router_policy_version": evidence.get("router_policy_version"),
                "agent_role": evidence.get("agent_role"),
                "config": evidence.get("config"),
                "error": evidence.get("error"),
                "profiles": profiles,
                "deterministic_pick_provider_model_id": pick_id,
                "deterministic_pick_canonical_model_id": names.get(pick_id) if pick_id else None,
                "evidence_changed_selection": evidence.get("evidence_changed_selection"),
            }
        return {
            **self._decision_summary(decision, role, agent_name, task_run_id),
            "rationale": decision.rationale,
            "tie_break": details.get("tie_break"),
            "requested_provider_model_id": details.get("requested_provider_model_id"),
            "selected_provider_id": details.get("selected_provider_id"),
            "free_preference_satisfied": details.get("free_preference_satisfied"),
            "eligible_count": details.get("eligible_count", len(eligible)),
            "excluded_count": details.get("excluded_count", 0),
            "exclusion_counts": details.get("exclusion_counts") or {},
            "eligible_candidates": eligible,
            "excluded_candidates": details.get("excluded_candidates") or [],
            "evidence": evidence_view,
        }

    @staticmethod
    def _decision_summary(decision: ModelRoutingDecision, role, agent_name, task_run_id) -> Dict[str, Any]:
        details = decision.details or {}
        evidence = details.get("evidence") if isinstance(details.get("evidence"), dict) else None
        return {
            "id": decision.id,
            "created_at": decision.created_at,
            "agent_run_id": decision.agent_run_id,
            "task_run_id": task_run_id,
            "agent_role": role,
            "agent_name": agent_name,
            "selection_mode": decision.selection_mode.value,
            "requested_policy": decision.requested_policy,
            "routing_strategy": decision.routing_strategy,
            "router_policy_version_id": decision.router_policy_version_id,
            "outcome": details.get("outcome")
            or ("selected" if decision.selected_provider_model_snapshot_id else None),
            "failure_code": details.get("failure_code"),
            "selected_provider_model_id": details.get("selected_provider_model_id"),
            "selected_canonical_model_id": details.get("selected_canonical_model_id"),
            "selected_pricing_classification": details.get("selected_pricing_classification"),
            "fallback_used": details.get("fallback_used"),
            "evidence_status": evidence.get("status") if evidence else None,
            "evidence_changed_selection": evidence.get("evidence_changed_selection") if evidence else None,
            "is_manual": decision.selection_mode == ModelSelectionMode.MANUAL,
        }

    # -- evidence policy -------------------------------------------------------------

    def _display_config(self) -> EvidenceConfig:
        """Thresholds shown next to evidence: the active policy's, else the
        installed v1 policy's, else the code defaults (all identical in v1)."""
        target = self._evidence_policy_row()
        try:
            return EvidenceConfig.from_json(target.scoring_config if target else DEFAULT_V1_CONFIG)
        except EvidenceConfigError:
            return EvidenceConfig.from_json(DEFAULT_V1_CONFIG)

    def _evidence_policy_row(self) -> Optional[RouterPolicyVersion]:
        """The installed, supported evidence policy: the active one if any,
        else the highest version whose config this code can honour."""
        rows = self.db.execute(
            select(RouterPolicyVersion).order_by(RouterPolicyVersion.version.desc())
        ).scalars()
        supported = []
        for row in rows:
            try:
                EvidenceConfig.from_json(row.scoring_config)
            except EvidenceConfigError:
                continue
            supported.append(row)
        for row in supported:
            if row.status == POLICY_ACTIVE:
                return row
        return supported[0] if supported else None

    def policy_status(self) -> Dict[str, Any]:
        active_count = self.db.execute(
            select(func.count(RouterPolicyVersion.id)).where(RouterPolicyVersion.status == POLICY_ACTIVE)
        ).scalar_one()
        row = self._evidence_policy_row()
        if row is None:
            return {"installed": False, "enabled": False, "active_versions": int(active_count)}
        config = EvidenceConfig.from_json(row.scoring_config)
        return {
            "installed": True,
            "enabled": row.status == POLICY_ACTIVE,
            "active_versions": int(active_count),
            "router_policy_version_id": row.id,
            "version": row.version,
            "strategy": config.strategy,
            "history_window_days": config.history_window_days,
            "min_observations": config.min_observations,
            "min_evaluated_runs": config.min_evaluated_runs,
            "max_provider_failure_rate": config.max_provider_failure_rate,
        }

    def set_evidence_routing(self, *, enabled: bool, actor) -> Dict[str, Any]:
        """One transaction. Enable: the installed evidence policy becomes the
        only ACTIVE version. Disable: no version stays ACTIVE. No row is
        created or deleted; routing history is never touched."""
        target = self._evidence_policy_row()
        if enabled and target is None:
            raise ConflictError(
                f"No {EVIDENCE_STRATEGY_V1} router policy is installed — run the database migrations first."
            )
        before = self.policy_status()
        others = update(RouterPolicyVersion).where(RouterPolicyVersion.status == POLICY_ACTIVE)
        if enabled:
            others = others.where(RouterPolicyVersion.id != target.id)
        self.db.execute(others.values(status=POLICY_INACTIVE))
        if enabled:
            target.status = POLICY_ACTIVE
        self.db.flush()
        if before.get("enabled") != enabled:
            AuditService(self.db).record(
                org_id=actor.org_id,
                event_type="router_policy.evidence_routing_enabled"
                if enabled
                else "router_policy.evidence_routing_disabled",
                actor_user_id=actor.id,
                target_ref=target.id if target is not None else None,
                detail={"strategy": EVIDENCE_STRATEGY_V1, "version": target.version if target else None},
                commit=False,
            )
        self.db.commit()
        return self.policy_status()
