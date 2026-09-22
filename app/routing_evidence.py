"""Routing evidence — MA8.2.

Derives, at routing time, a small per-candidate evidence profile for one
(project, Agent role) from records the platform already keeps — nothing is
aggregated or stored separately:

* ``model_calls`` — one row per provider invocation (i.e. per Agent Run
  attempt that reached a provider): outcome, error category, latency,
  tokens, cost and its exact/estimated flag. Retries are separate rows and
  so separate observations.
* ``evaluation_runs`` + ``evaluation_criterion_results`` (MA6) — COMPLETED
  evaluations of an Agent Run's artifact, attributed to the provider model
  of that run's successful ModelCall (the call that produced the artifact).

Evidence is scoped to the routing Agent Run's own project (tasks.project_id)
and to the exact ``agent_versions.role`` string, and bounded by a history
window and a row cap. It is advisory: it can only reorder candidates the
MA8.1 eligibility/policy gate already admitted (app.model_resolution).

Signals are kept apart, never folded into one number:

* reliability — provider/infrastructure failures (connection, timeout,
  invalid response) versus completed calls. Not quality evidence.
* quality — MA6 findings. MET is positive, PARTIAL is weaker than MET,
  NOT_MET is negative, NOT_APPLICABLE is "not checked" and ignored.
* efficiency — latency/tokens/cost are reported for explanation only; the
  v1 strategy does not order by them.

Each signal only moves a candidate once it has the configured minimum
evidence; below that the candidate is neutral (cold start), so a new model
is never penalized for being new. Profiles hold counts and timestamps only —
never prompts, artifact content, rationales or credentials.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import EvaluationFinding, EvaluationRunStatus, ModelCallStatus
from app.models.agents import AgentVersion
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.execution import ModelCall, RouterPolicyVersion
from app.models.tasks import AgentRun, Task, TaskRun

EVIDENCE_STRATEGY_V1 = "ma8.2-evidence-v1"

# The v1 configuration, seeded INACTIVE as router_policy_versions version 1
# by migration 8c3f6b2e9d14 (evidence routing is opt-in: until a row is
# ACTIVE, AUTO routing is exactly MA8.1). A decision reads the ACTIVE row's
# copy, so the thresholds a decision used are the ones recorded with it.
DEFAULT_V1_CONFIG: Dict[str, Any] = {
    "strategy": EVIDENCE_STRATEGY_V1,
    "history_window_days": 30,
    "max_history_rows": 2000,
    "min_observations": 5,
    "max_provider_failure_rate": 0.5,
    "min_evaluated_runs": 3,
    "positive_met_share": 0.7,
    "negative_not_met_share": 0.3,
}

# Error categories (app.services.execution_service) that are provider /
# infrastructure evidence about a model offering. Anything else a ModelCall
# can record (e.g. provider_authentication_error — a credential problem, not
# the model) is not counted either way.
PROVIDER_FAILURE_CATEGORIES = frozenset(
    {"provider_connection_error", "provider_timeout", "provider_invalid_response"}
)

RELIABILITY_OK = "ok"
RELIABILITY_DEMOTED = "demoted"
QUALITY_POSITIVE = "positive"
QUALITY_MIXED = "mixed"
QUALITY_NEGATIVE = "negative"
INSUFFICIENT = "insufficient"


class EvidenceConfigError(ValueError):
    """The active router policy version's config is not a supported strategy."""


@dataclass(frozen=True)
class EvidenceConfig:
    history_window_days: int
    max_history_rows: int
    min_observations: int
    max_provider_failure_rate: float
    min_evaluated_runs: int
    positive_met_share: float
    negative_not_met_share: float
    strategy: str = EVIDENCE_STRATEGY_V1

    @classmethod
    def from_json(cls, config: Optional[Dict[str, Any]]) -> "EvidenceConfig":
        config = config or {}
        if config.get("strategy") != EVIDENCE_STRATEGY_V1:
            raise EvidenceConfigError(f"unsupported routing strategy {config.get('strategy')!r}")
        merged = {**DEFAULT_V1_CONFIG, **config}
        try:
            parsed = cls(
                history_window_days=int(merged["history_window_days"]),
                max_history_rows=int(merged["max_history_rows"]),
                min_observations=int(merged["min_observations"]),
                max_provider_failure_rate=float(merged["max_provider_failure_rate"]),
                min_evaluated_runs=int(merged["min_evaluated_runs"]),
                positive_met_share=float(merged["positive_met_share"]),
                negative_not_met_share=float(merged["negative_not_met_share"]),
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceConfigError(f"invalid {EVIDENCE_STRATEGY_V1} config: {exc}") from exc
        if (
            min(parsed.history_window_days, parsed.max_history_rows) < 1
            or min(parsed.min_observations, parsed.min_evaluated_runs) < 1
        ):
            raise EvidenceConfigError(f"invalid {EVIDENCE_STRATEGY_V1} config: bounds must be >= 1")
        return parsed

    def to_json(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "history_window_days": self.history_window_days,
            "max_history_rows": self.max_history_rows,
            "min_observations": self.min_observations,
            "max_provider_failure_rate": self.max_provider_failure_rate,
            "min_evaluated_runs": self.min_evaluated_runs,
            "positive_met_share": self.positive_met_share,
            "negative_not_met_share": self.negative_not_met_share,
        }


@dataclass(frozen=True)
class ActiveEvidencePolicy:
    router_policy_version_id: str
    version: int
    config: EvidenceConfig


@dataclass(frozen=True)
class RoutingContext:
    """Who is being routed: the evidence scope. ``project_id`` bounds the
    history to the routing run's own project; ``agent_role`` is
    ``agent_versions.role``."""

    project_id: str
    agent_role: str


@dataclass
class EvidenceProfile:
    provider_model_id: str
    completed: int = 0
    provider_failures: int = 0
    other_failures: int = 0
    evaluated_runs: int = 0
    met: int = 0
    partial: int = 0
    not_met: int = 0
    not_applicable: int = 0
    latencies_ms: List[int] = field(default_factory=list)
    tokens_in: List[int] = field(default_factory=list)
    tokens_out: List[int] = field(default_factory=list)
    cost_exact: int = 0
    cost_estimated: int = 0
    cost_unknown: int = 0
    last_observed_at: Optional[datetime] = None

    @property
    def observations(self) -> int:
        """Calls that count toward reliability (completed + provider failures)."""
        return self.completed + self.provider_failures

    @property
    def provider_failure_rate(self) -> Optional[float]:
        return self.provider_failures / self.observations if self.observations else None

    def reliability(self, config: EvidenceConfig) -> str:
        if self.observations < config.min_observations:
            return INSUFFICIENT
        if self.provider_failure_rate > config.max_provider_failure_rate:
            return RELIABILITY_DEMOTED
        return RELIABILITY_OK

    def quality(self, config: EvidenceConfig) -> str:
        judged = self.met + self.partial + self.not_met  # NOT_APPLICABLE was never checked
        if self.evaluated_runs < config.min_evaluated_runs or judged == 0:
            return INSUFFICIENT
        if self.not_met / judged >= config.negative_not_met_share:
            return QUALITY_NEGATIVE
        if self.met / judged >= config.positive_met_share:
            return QUALITY_POSITIVE
        return QUALITY_MIXED

    def sort_key(self, config: EvidenceConfig):
        """(reliability rank, quality rank): lower is preferred. Only a
        sufficiently-evidenced demotion/positive/negative moves a candidate;
        insufficient and mixed evidence are both neutral."""
        reliability_rank = 1 if self.reliability(config) == RELIABILITY_DEMOTED else 0
        quality_rank = {QUALITY_POSITIVE: 0, QUALITY_NEGATIVE: 2}.get(self.quality(config), 1)
        return (reliability_rank, quality_rank)

    def is_sufficient(self, config: EvidenceConfig) -> bool:
        return self.reliability(config) != INSUFFICIENT or self.quality(config) != INSUFFICIENT

    def to_json(self, config: EvidenceConfig) -> Dict[str, Any]:
        rate = self.provider_failure_rate
        return {
            "provider_model_id": self.provider_model_id,
            "observations": self.observations,
            "completed": self.completed,
            "provider_failures": self.provider_failures,
            "other_failures": self.other_failures,
            "provider_failure_rate": round(rate, 4) if rate is not None else None,
            "reliability": self.reliability(config),
            "evaluated_runs": self.evaluated_runs,
            "findings": {
                "met": self.met,
                "partial": self.partial,
                "not_met": self.not_met,
                "not_applicable": self.not_applicable,
            },
            "quality": self.quality(config),
            "median_latency_ms": int(median(self.latencies_ms)) if self.latencies_ms else None,
            "median_tokens_in": int(median(self.tokens_in)) if self.tokens_in else None,
            "median_tokens_out": int(median(self.tokens_out)) if self.tokens_out else None,
            "cost_observations": {
                "exact": self.cost_exact,
                "estimated": self.cost_estimated,
                "unknown": self.cost_unknown,
            },
            "last_observed_at": self.last_observed_at.isoformat() if self.last_observed_at else None,
        }


def load_active_policy(db: Session) -> Optional[ActiveEvidencePolicy]:
    """Highest ACTIVE router_policy_versions row, or None (evidence routing
    disabled — routing is exactly MA8.1). Raises EvidenceConfigError for an
    active row whose config this code cannot honour."""
    row = db.execute(
        select(RouterPolicyVersion)
        .where(RouterPolicyVersion.status == "active")
        .order_by(RouterPolicyVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return ActiveEvidencePolicy(
        router_policy_version_id=row.id,
        version=row.version,
        config=EvidenceConfig.from_json(row.scoring_config),
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _scope(stmt, context: RoutingContext):
    return (
        stmt.join(AgentRun, AgentRun.id == ModelCall.agent_run_id)
        .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
        .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(AgentVersion.role == context.agent_role, Task.project_id == context.project_id)
    )


def load_evidence(
    db: Session,
    *,
    context: RoutingContext,
    provider_model_ids: Iterable[str],
    config: EvidenceConfig,
    as_of: Optional[datetime] = None,
) -> Dict[str, EvidenceProfile]:
    """Profiles for ``provider_model_ids`` (every id gets one, possibly
    empty). Two bounded, read-only queries."""
    ids = sorted(set(provider_model_ids))
    profiles = {pm_id: EvidenceProfile(provider_model_id=pm_id) for pm_id in ids}
    if not ids:
        return profiles
    since = (as_of or _utcnow()) - timedelta(days=config.history_window_days)

    calls = db.execute(
        _scope(
            select(
                ModelCall.provider_model_id,
                ModelCall.status,
                ModelCall.error,
                ModelCall.latency_ms,
                ModelCall.tokens_in,
                ModelCall.tokens_out,
                ModelCall.cost_amount,
                ModelCall.cost_is_estimated,
                ModelCall.started_at,
            ),
            context,
        )
        .where(
            ModelCall.provider_model_id.in_(ids),
            ModelCall.started_at >= since,
            ModelCall.status.in_([ModelCallStatus.SUCCESS, ModelCallStatus.ERROR, ModelCallStatus.TIMEOUT]),
        )
        .order_by(ModelCall.started_at.desc(), ModelCall.id)
        .limit(config.max_history_rows)
    ).all()
    for pm_id, status, error, latency, t_in, t_out, cost, estimated, started_at in calls:
        profile = profiles[pm_id]
        if profile.last_observed_at is None or started_at > profile.last_observed_at:
            profile.last_observed_at = started_at
        if status == ModelCallStatus.SUCCESS:
            profile.completed += 1
            if latency is not None:
                profile.latencies_ms.append(latency)
            if t_in is not None:
                profile.tokens_in.append(t_in)
            if t_out is not None:
                profile.tokens_out.append(t_out)
            if cost is None:
                profile.cost_unknown += 1
            elif estimated:
                profile.cost_estimated += 1
            else:
                profile.cost_exact += 1
            continue
        category = (error or {}).get("category") if isinstance(error, dict) else None
        if status == ModelCallStatus.TIMEOUT or category in PROVIDER_FAILURE_CATEGORIES:
            profile.provider_failures += 1
        else:
            profile.other_failures += 1

    findings = db.execute(
        _scope(
            select(ModelCall.provider_model_id, EvaluationRun.id, EvaluationCriterionResult.finding)
            .select_from(ModelCall)
            .join(EvaluationRun, EvaluationRun.subject_agent_run_id == ModelCall.agent_run_id)
            .join(EvaluationCriterionResult, EvaluationCriterionResult.evaluation_run_id == EvaluationRun.id),
            context,
        )
        .where(
            ModelCall.provider_model_id.in_(ids),
            ModelCall.started_at >= since,
            ModelCall.status == ModelCallStatus.SUCCESS,
            EvaluationRun.status == EvaluationRunStatus.COMPLETED,
        )
        .order_by(ModelCall.started_at.desc(), EvaluationRun.id, EvaluationCriterionResult.id)
        .limit(config.max_history_rows)
    ).all()
    seen_runs = set()
    for pm_id, evaluation_run_id, finding in findings:
        profile = profiles[pm_id]
        if evaluation_run_id not in seen_runs:
            seen_runs.add(evaluation_run_id)
            profile.evaluated_runs += 1
        if finding == EvaluationFinding.MET:
            profile.met += 1
        elif finding == EvaluationFinding.PARTIAL:
            profile.partial += 1
        elif finding == EvaluationFinding.NOT_MET:
            profile.not_met += 1
        else:
            profile.not_applicable += 1
    return profiles
