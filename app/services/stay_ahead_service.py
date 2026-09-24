"""AIL.4A Personal Stay-Ahead Today — deterministic, read-only derivation.

Question answered: "What has changed that matters to me, based on things I
explicitly learned, tested, used or watched?"

Everything is computed on read from existing records. This module performs
NO writes and never calls a service whose read path could write (notably not
``ExperimentExecutionService.refresh``, which settles experiment status on
GET). There is no persisted signal, no dismissal state, no score, no ranking
of models and no generated text: every sentence is a template filled with a
recorded fact, and every signal carries reason codes plus references to the
exact records it rests on.

Authorization: the caller supplies the authenticated ``user_id``. User-scoped
tables (experiments, learning evidence, interests, triage) are always
filtered on it. Project ModelCalls are read only through the pre-existing
``projects.ail_evidence_opt_in`` mechanism, only through the user's own
project membership, and only as model metadata (ids, snapshot ids, timestamps,
counts) — never task text, prompts, code, artifacts or outputs.

Personal vs shared usage (AIL.4A correction). Two different facts are kept
apart and never blurred:

* PERSONAL_USAGE — a completed run of a Personal Lab experiment owned by the
  authenticated user (``experiments.user_id``). Only this is ever described
  as "you used".
* OPTED_IN_PROJECT_USAGE — a successful ModelCall in a project that carries
  ``ail_evidence_opt_in`` and that the user can access. The repository does
  not record who caused a ModelCall or Task Run (``tasks.created_by`` is the
  Task template's author, and ``task_runs``/``agent_runs``/``model_calls``
  carry no user), so personal ownership can NOT be established from project
  membership and is never inferred. This is shared project evidence and is
  worded as such.

Re-use suppression follows the same line: only the authenticated user's own
Personal Lab usage after a catalog change suppresses an EXPERIMENT_MAY_BE_STALE
signal. Another member's later project call never does.

Experiment completion reuses the canonical execution semantics rather than a
second definition: ``ExperimentExecutionService._execution_state`` (read-only)
must report COMPLETED, and — because the engine claims an Agent Run terminal
COMPLETED before it completes its Task Run — every slot's Agent Run(s) must be
``AgentRunStatus.COMPLETED`` with a recorded model snapshot.

AIL.4B adds the ``review`` section (REVIEW_DUE / REVIEW_FAILED cards for prompts
already delivered this week — GET never delivers, see ``review_prompt_service``); the five families above are unchanged. A Concept can
therefore appear both in WORTH_REVISITING (a recorded change) and in the review
section (a review is due) — different questions, deliberately not deduplicated.

Deferred limitation — ``triage_decisions.revisit_condition``. The repository
validates only ``kind`` for it and defines no shape or evaluation semantics
(no ``verification_at_least`` level, no ``attention_state`` threshold, no date
field), and nothing tests any. AIL.4A therefore evaluates ONLY the
deterministic ``revisit_at`` timestamp. A ``revisit_condition`` is never
evaluated, never inferred and never fires a signal. Full ``revisit_condition``
support is deferred until its shape is defined and tested.

Signal families (see ``app.schemas.stay_ahead``):

* USED_MODEL_CHANGED        a model the user used has a newer catalog record
                            that differs from the record they last used.
* EXPERIMENT_MAY_BE_STALE   the same test, per fully executed Personal Lab
                            experiment.
* WATCHED_DEVELOPMENT_CHANGED  new qualifying evidence / confirmed Concept
                            link / reached revisit date on a WATCHed
                            Development.
* CONCEPT_CHANGED           a Concept the user learned (or explicitly
                            watches) has a new material ConceptVersion or a
                            new qualifying linked Development.
* WORTH_REVISITING          a presentation rollup: one card per Concept with
                            learner-state evidence, absorbing the signals
                            above that attach to it. Each absorbed signal
                            keeps its own reason codes and evidence.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.db.enums import (
    AgentRunStatus,
    ChangeSeverity,
    EvidenceRefType,
    EvidenceType,
    ExperimentStatus,
    ExperimentType,
    ModelCallStatus,
    SnapshotChangeKind,
    SnapshotSource,
    TaskRunStatus,
    VersionStatus,
)
from app.models.concepts import Concept
from app.models.execution import ModelCall
from app.models.identity import Project, ProjectMembership
from app.models.lab import Experiment, ExperimentTaskRun
from app.models.learner import LearnerInterest, LearningEvidence
from app.models.providers import Model, Provider, ProviderModelSnapshot
from app.models.radar import (
    Claim,
    ClaimStatus,
    ClaimType,
    Development,
    DevelopmentConcept,
    DevelopmentConceptState,
    DevelopmentStatus,
    TriageDecision,
    TriageDecisionKind,
)
from app.models.tasks import AgentRun, Task, TaskRun
from app.schemas.stay_ahead import (
    StayAheadEvidenceRef,
    StayAheadFamily,
    StayAheadLink,
    StayAheadReason,
    StayAheadReasonCode,
    StayAheadReviewCard,
    StayAheadReviewSection,
    StayAheadSection,
    StayAheadSections,
    StayAheadSignal,
    StayAheadToday,
)
from app.services.concept_graph_service import ConceptGraphService
from app.services.experiment_execution_service import ExperimentExecutionService
from app.services.learner_state_service import (
    DEMONSTRATED,
    EXPOSED,
    NOT_STARTED,
    PRACTICED,
    UNDERSTOOD,
    LearnerConceptState,
    LearnerStateService,
)
from app.services.radar_service import derive_verification
from app.services.review_attempt_service import ReviewAttemptService
from app.services.review_prompt_service import PromptPreview, ReviewPromptService
from app.services.review_retention_service import ReviewReasonCode, prompt_sort_key

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 90

# Presentation caps only (a section never backfills with weaker items).
SECTION_CAPS = {
    "worth_revisiting": 3,
    "used_models_changed": 5,
    "watched_developments": 5,
    "experiments_to_rerun": 3,
    "concepts_changed": 5,
}

# Evidence a WATCHed Development can gain that counts as a change. Community
# attention is never a learning/revisit trigger by itself, and generated
# explanations are not evidence.
_NON_QUALIFYING_CLAIM_TYPES = frozenset({ClaimType.COMMUNITY_SIGNAL, ClaimType.AI_EXPLANATION})

# Concept-linked Developments must be at least Documented (a FACT exists).
_VERIFICATION_ORDER = ["Claimed", "Documented", "Available", "Independently Measured", "Tested by Us"]
_MIN_CONCEPT_DEVELOPMENT_VERIFICATION = "Documented"

_LADDER_RANK = {NOT_STARTED: 0, EXPOSED: 1, UNDERSTOOD: 2, PRACTICED: 3, DEMONSTRATED: 4}
_LADDER_LABEL = {
    NOT_STARTED: "not started",
    EXPOSED: "exposed",
    UNDERSTOOD: "understood",
    PRACTICED: "practiced",
    DEMONSTRATED: "demonstrated",
}
_EXPERIMENT_LABEL = {
    ExperimentType.MODEL_COMPARISON: "Model Face-off",
    ExperimentType.PROMPT_COMPARISON: "Prompt Comparison",
    ExperimentType.VARIANCE: "Consistency Check",
}
_CLAIM_LABEL = {
    ClaimType.FACT: "fact",
    ClaimType.PROVIDER_CLAIM: "provider claim",
    ClaimType.RESEARCH_RESULT: "research result",
    ClaimType.BENCHMARK_RESULT: "benchmark result",
    ClaimType.PLATFORM_OBSERVATION: "platform observation",
}
_CHANGE_REASON = {
    SnapshotChangeKind.PRICE.value: StayAheadReasonCode.MODEL_PRICE_CHANGED,
    SnapshotChangeKind.CONTEXT.value: StayAheadReasonCode.MODEL_CONTEXT_CHANGED,
    SnapshotChangeKind.CAPABILITY.value: StayAheadReasonCode.MODEL_CAPABILITY_CHANGED,
    SnapshotChangeKind.STATUS.value: StayAheadReasonCode.MODEL_STATUS_CHANGED,
}
_CAPABILITY_KEYS = ("tool_calling_support", "structured_output_support", "vision_capability")
_IN_CHUNK = 400
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _chunks(values: Iterable[str]) -> List[List[str]]:
    items = sorted(set(values))
    return [items[i : i + _IN_CHUNK] for i in range(0, len(items), _IN_CHUNK)]


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _day(value: Optional[datetime]) -> str:
    aware = _aware(value)
    return aware.strftime("%Y-%m-%d") if aware else "an unknown date"


def _dec(value: Any) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


def _plain(value: Optional[Decimal]) -> Optional[str]:
    """Storage-precision-independent price text (2.00 and 2.00000000 both
    read as "2")."""
    return None if value is None else f"{value.normalize():f}"


def _money(value: Optional[Decimal]) -> str:
    return "unknown" if value is None else f"${_plain(value)}"


# -- internal records ----------------------------------------------------------


@dataclass
class _Use:
    """One authorized use of a provider model, at the granularity needed to
    compare against later catalog changes. Metadata only."""

    provider_model_id: str
    model_id: str
    provider_id: str
    snapshot_id: str
    used_at: datetime
    # "personal_lab": the user's own experiment (PERSONAL_USAGE).
    # "opted_in_project": shared project evidence (OPTED_IN_PROJECT_USAGE).
    source: str
    experiment_id: Optional[str] = None
    calls: int = 1


@dataclass
class _ModelChange:
    baseline: ProviderModelSnapshot
    latest: ProviderModelSnapshot
    changed_at: datetime
    changes: List[Dict[str, Any]]


@dataclass
class _Item:
    signal: StayAheadSignal
    concept_ids: Set[str] = field(default_factory=set)


class StayAheadService:
    def __init__(self, db: Session, *, now: Optional[datetime] = None):
        self.db = db
        self.now = _aware(now) or datetime.now(timezone.utc)
        self._concepts = ConceptGraphService(db)
        self._learner_state = LearnerStateService(db)

    # -- public ------------------------------------------------------------------

    def today(self, user_id: str, *, window_days: int = DEFAULT_WINDOW_DAYS) -> StayAheadToday:
        window_days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
        window_start = self.now - timedelta(days=window_days)

        learned = self._learned_concepts(user_id)

        uses = self._collect_uses(user_id)
        snapshots = self._load_snapshots({use.snapshot_id for use in uses})
        catalog = self._catalog_events({snap.provider_model_id for snap in snapshots.values()})
        model_names = self._model_names(uses)

        used_models, personal_latest_use = self._used_model_signals(
            uses, snapshots, catalog, model_names, window_start
        )
        experiments = self._experiment_signals(
            user_id, uses, snapshots, catalog, model_names, personal_latest_use, window_start
        )
        watched = self._watched_signals(user_id, window_start)
        concepts = self._concept_signals(user_id, learned, window_start)

        # Rollup: signals attached to a Concept the user has learner-state
        # evidence for are absorbed into that Concept's card so the same fact
        # is never shown twice. Model-level signals are platform facts, not
        # Concept facts, and stay in their own section.
        standalone: Dict[str, List[_Item]] = {
            "used_models_changed": used_models,
            "watched_developments": [],
            "experiments_to_rerun": [],
            "concepts_changed": [],
        }
        absorbed: Dict[str, List[StayAheadReason]] = {}
        for key, items in (
            ("watched_developments", watched),
            ("experiments_to_rerun", experiments),
            ("concepts_changed", concepts),
        ):
            for item in items:
                targets = sorted(cid for cid in item.concept_ids if cid in learned)
                if not targets:
                    standalone[key].append(item)
                    continue
                for cid in targets:
                    absorbed.setdefault(cid, []).append(self._as_reason(item.signal))

        worth = self._worth_revisiting(learned, absorbed)
        review_cards, prompts = self._review_cards(user_id, learned)

        def section(key: str, items: List[StayAheadSignal]) -> StayAheadSection:
            ordered = items if key == "worth_revisiting" else self._by_recency(items)
            cap = SECTION_CAPS[key]
            return StayAheadSection(total=len(ordered), shown=min(len(ordered), cap), items=ordered[:cap])

        return StayAheadToday(
            generated_at=self.now,
            window_days=window_days,
            window_start=window_start,
            sections=StayAheadSections(
                worth_revisiting=section("worth_revisiting", worth),
                used_models_changed=section("used_models_changed", [i.signal for i in standalone["used_models_changed"]]),
                watched_developments=section("watched_developments", [i.signal for i in standalone["watched_developments"]]),
                experiments_to_rerun=section("experiments_to_rerun", [i.signal for i in standalone["experiments_to_rerun"]]),
                concepts_changed=section("concepts_changed", [i.signal for i in standalone["concepts_changed"]]),
                review=StayAheadReviewSection(
                    total=len(review_cards),
                    shown=len(review_cards),
                    items=review_cards,
                    quota={
                        "limit": prompts.limit,
                        "delivered": len(prompts.delivered),
                        "remaining": max(0, prompts.limit - len(prompts.delivered)),
                        "week_start": _aware(prompts.week_start),
                    },
                    not_prompted=prompts.not_prompted,
                    allocation_pending=prompts.allocation_pending,
                ),
            ),
        )

    # -- ordering ----------------------------------------------------------------

    @staticmethod
    def _by_recency(signals: List[StayAheadSignal]) -> List[StayAheadSignal]:
        """Presentation order only: newest recorded change first, then id."""
        return sorted(signals, key=lambda s: (-_aware(s.changed_at).timestamp(), s.id))

    # -- model usage (authorized metadata only) -------------------------------------

    def _collect_uses(self, user_id: str) -> List[_Use]:
        uses: List[_Use] = []
        by_snapshot: Dict[Tuple[str, str], _Use] = {}

        # Personal Lab: the user's own completed experiment runs (Task Run AND
        # Agent Run both terminal-success). The Agent Run's snapshot is the
        # record that was actually pinned/frozen when the run executed.
        lab_rows = self.db.execute(
            select(
                ExperimentTaskRun.experiment_id,
                AgentRun.model_id,
                AgentRun.provider_id,
                AgentRun.provider_model_snapshot_id,
                TaskRun.started_at,
            )
            .select_from(ExperimentTaskRun)
            .join(Experiment, Experiment.id == ExperimentTaskRun.experiment_id)
            .join(TaskRun, TaskRun.id == ExperimentTaskRun.task_run_id)
            .join(AgentRun, AgentRun.task_run_id == TaskRun.id)
            .where(
                Experiment.user_id == user_id,
                Experiment.status != ExperimentStatus.CANCELLED,
                TaskRun.status == TaskRunStatus.COMPLETED,
                AgentRun.status == AgentRunStatus.COMPLETED,
                AgentRun.provider_model_snapshot_id.isnot(None),
                AgentRun.model_id.isnot(None),
                AgentRun.provider_id.isnot(None),
            )
        ).all()
        for experiment_id, model_id, provider_id, snapshot_id, started_at in lab_rows:
            key = (experiment_id, snapshot_id)
            existing = by_snapshot.get(key)
            started = _aware(started_at)
            if existing is None:
                by_snapshot[key] = _Use(
                    provider_model_id="",  # resolved from the snapshot below
                    model_id=model_id,
                    provider_id=provider_id,
                    snapshot_id=snapshot_id,
                    used_at=started or _EPOCH,  # no start time: the snapshot time governs
                    source="personal_lab",
                    experiment_id=experiment_id,
                )
            else:
                existing.calls += 1
                if started is not None and started > existing.used_at:
                    existing.used_at = started
        uses.extend(by_snapshot.values())

        # Opted-in project calls: only projects the user is a member of AND
        # that already carry ail_evidence_opt_in. This is SHARED project
        # evidence: nothing here attributes a call to the user, so it is never
        # treated as personal usage. Other members' Personal Lab experiments
        # are never read through this path.
        platform_rows = self.db.execute(
            select(
                ModelCall.provider_model_id,
                ModelCall.provider_model_snapshot_id,
                ModelCall.model_id,
                ModelCall.provider_id,
                func.max(ModelCall.started_at),
                func.count(ModelCall.id),
            )
            .join(AgentRun, AgentRun.id == ModelCall.agent_run_id)
            .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
            .join(Task, Task.id == TaskRun.task_id)
            .join(Project, Project.id == Task.project_id)
            .join(ProjectMembership, ProjectMembership.project_id == Project.id)
            .where(
                ProjectMembership.user_id == user_id,
                Project.ail_evidence_opt_in.is_(True),
                ModelCall.status == ModelCallStatus.SUCCESS,
                ModelCall.provider_model_id.isnot(None),
                ModelCall.provider_model_snapshot_id.isnot(None),
                TaskRun.id.not_in(select(ExperimentTaskRun.task_run_id)),
            )
            .group_by(
                ModelCall.provider_model_id,
                ModelCall.provider_model_snapshot_id,
                ModelCall.model_id,
                ModelCall.provider_id,
            )
        ).all()
        for provider_model_id, snapshot_id, model_id, provider_id, last_started, calls in platform_rows:
            uses.append(
                _Use(
                    provider_model_id=provider_model_id,
                    model_id=model_id,
                    provider_id=provider_id,
                    snapshot_id=snapshot_id,
                    used_at=_aware(last_started) or _EPOCH,
                    source="opted_in_project",
                    calls=int(calls or 0),
                )
            )
        return uses

    def _load_snapshots(self, snapshot_ids: Set[str]) -> Dict[str, ProviderModelSnapshot]:
        found: Dict[str, ProviderModelSnapshot] = {}
        for chunk in _chunks(snapshot_ids):
            for snap in self.db.execute(
                select(ProviderModelSnapshot).where(ProviderModelSnapshot.id.in_(chunk))
            ).scalars():
                found[snap.id] = snap
        return found

    def _catalog_events(self, provider_model_ids: Set[str]) -> Dict[str, List[ProviderModelSnapshot]]:
        events: Dict[str, List[ProviderModelSnapshot]] = {}
        for chunk in _chunks(provider_model_ids):
            rows = self.db.execute(
                select(ProviderModelSnapshot)
                .where(
                    ProviderModelSnapshot.provider_model_id.in_(chunk),
                    ProviderModelSnapshot.source == SnapshotSource.CATALOG_REFRESH,
                )
                .order_by(ProviderModelSnapshot.snapshotted_at, ProviderModelSnapshot.id)
            ).scalars()
            for snap in rows:
                events.setdefault(snap.provider_model_id, []).append(snap)
        return events

    def _model_names(self, uses: List[_Use]) -> Dict[str, Dict[str, str]]:
        model_ids = {use.model_id for use in uses}
        provider_ids = {use.provider_id for use in uses}
        names: Dict[str, Dict[str, str]] = {"model": {}, "provider": {}}
        for chunk in _chunks(model_ids):
            for row_id, name in self.db.execute(
                select(Model.id, Model.canonical_model_id).where(Model.id.in_(chunk))
            ).all():
                names["model"][row_id] = name
        for chunk in _chunks(provider_ids):
            for row_id, name in self.db.execute(select(Provider.id, Provider.name).where(Provider.id.in_(chunk))).all():
                names["provider"][row_id] = name
        return names

    def _use_time(self, use: _Use, snapshots: Dict[str, ProviderModelSnapshot]) -> datetime:
        """The moment after which a catalog change is news to the user: the
        later of the recorded snapshot and the actual run (a run can use an
        older pinned snapshot; a change before the run is not news)."""
        snapshot = snapshots[use.snapshot_id]
        return max(_aware(snapshot.snapshotted_at), use.used_at)

    # -- model change detection ---------------------------------------------------

    def _detect_change(
        self,
        baseline: ProviderModelSnapshot,
        baseline_time: datetime,
        events: List[ProviderModelSnapshot],
    ) -> Optional[_ModelChange]:
        later = [e for e in events if e.id != baseline.id and _aware(e.snapshotted_at) > baseline_time]
        if not later:
            return None
        latest = later[-1]
        status_event = any(SnapshotChangeKind.STATUS.value in (e.change_kinds or []) for e in later)
        changes = self._diff(baseline, latest, status_event)
        if not changes:
            return None  # e.g. a change that later reverted: no net difference
        return _ModelChange(
            baseline=baseline, latest=latest, changed_at=_aware(latest.snapshotted_at), changes=changes
        )

    @staticmethod
    def _diff(baseline: ProviderModelSnapshot, latest: ProviderModelSnapshot, status_event: bool) -> List[Dict[str, Any]]:
        """Net difference between two catalog records. A price/context that
        was unrecorded (None) at baseline is never reported as changed, and
        capability/status are compared only where BOTH records carry the key,
        so a legacy or partial record on either side can't fabricate a
        change."""
        changes: List[Dict[str, Any]] = []

        price_fields: Dict[str, Dict[str, Any]] = {}
        for label, before, after in (
            ("input_per_mtok", baseline.pricing_input_per_mtok, latest.pricing_input_per_mtok),
            ("output_per_mtok", baseline.pricing_output_per_mtok, latest.pricing_output_per_mtok),
        ):
            if _dec(before) is not None and _dec(before) != _dec(after):
                price_fields[label] = {"before": _plain(_dec(before)), "after": _plain(_dec(after))}
        if baseline.currency and latest.currency and baseline.currency != latest.currency:
            price_fields["currency"] = {"before": baseline.currency, "after": latest.currency}
        if price_fields:
            changes.append({"kind": SnapshotChangeKind.PRICE.value, "fields": price_fields})

        if baseline.context_window is not None and baseline.context_window != latest.context_window:
            changes.append(
                {
                    "kind": SnapshotChangeKind.CONTEXT.value,
                    "fields": {"context_window": {"before": baseline.context_window, "after": latest.context_window}},
                }
            )

        before_caps = baseline.capability_snapshot or {}
        after_caps = latest.capability_snapshot or {}
        cap_fields = {
            key: {"before": before_caps[key], "after": after_caps.get(key)}
            for key in _CAPABILITY_KEYS
            if key in before_caps and key in after_caps and before_caps[key] != after_caps[key]
        }
        if cap_fields:
            changes.append({"kind": SnapshotChangeKind.CAPABILITY.value, "fields": cap_fields})

        after_status = after_caps.get("model_status")
        if "model_status" in before_caps:
            if after_status is not None and before_caps["model_status"] != after_status:
                changes.append(
                    {
                        "kind": SnapshotChangeKind.STATUS.value,
                        "fields": {"model_status": {"before": before_caps["model_status"], "after": after_status}},
                    }
                )
        elif status_event and after_status not in (None, "active"):
            # Execution-freeze baselines don't record status; only report a
            # status event when the latest recorded status isn't ACTIVE.
            changes.append(
                {
                    "kind": SnapshotChangeKind.STATUS.value,
                    "fields": {"model_status": {"before": None, "after": after_status}},
                }
            )
        return changes

    @staticmethod
    def _describe_changes(changes: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for change in changes:
            kind, fields = change["kind"], change["fields"]
            if kind == SnapshotChangeKind.PRICE.value:
                bits = []
                for key, label in (("input_per_mtok", "input"), ("output_per_mtok", "output")):
                    if key in fields:
                        bits.append(
                            f"{label} {_money(_dec(fields[key]['before']))} to {_money(_dec(fields[key]['after']))} per million tokens"
                        )
                if "currency" in fields:
                    bits.append(f"currency {fields['currency']['before']} to {fields['currency']['after']}")
                parts.append("price (" + "; ".join(bits) + ")")
            elif kind == SnapshotChangeKind.CONTEXT.value:
                ctx = fields["context_window"]
                parts.append(f"context window ({ctx['before']} to {ctx['after']} tokens)")
            elif kind == SnapshotChangeKind.CAPABILITY.value:
                bits = [f"{k.replace('_', ' ')}: {v['before']} to {v['after']}" for k, v in sorted(fields.items())]
                parts.append("capabilities (" + "; ".join(bits) + ")")
            elif kind == SnapshotChangeKind.STATUS.value:
                st = fields["model_status"]
                if st["before"] is None:
                    parts.append(f"availability status (now recorded as {st['after']})")
                else:
                    parts.append(f"availability status ({st['before']} to {st['after']})")
        return ", ".join(parts)

    @staticmethod
    def _model_reason_codes(changes: List[Dict[str, Any]]) -> List[str]:
        return sorted({_CHANGE_REASON[c["kind"]].value for c in changes})

    # -- USED_MODEL_CHANGED -----------------------------------------------------

    def _used_model_signals(
        self,
        uses: List[_Use],
        snapshots: Dict[str, ProviderModelSnapshot],
        catalog: Dict[str, List[ProviderModelSnapshot]],
        names: Dict[str, Dict[str, str]],
        window_start: datetime,
    ) -> Tuple[List[_Item], Dict[str, datetime]]:
        """Returns the signals plus, per provider model, the time of the
        authenticated user's most recent PERSONAL use. That map — and only
        that map — is what suppresses stale-experiment signals for models the
        user has since re-used; shared project calls never enter it.

        Personal and shared usage are detected against their own baselines
        (the latest use in each scope), so a shared project call made after a
        change can never hide a change the user has not personally seen."""
        by_pm: Dict[str, List[_Use]] = {}
        for use in uses:
            pm_id = snapshots[use.snapshot_id].provider_model_id
            use.provider_model_id = pm_id
            by_pm.setdefault(pm_id, []).append(use)

        def detect(scope_uses: List[_Use], events: List[ProviderModelSnapshot]):
            if not scope_uses:
                return None
            latest = max(scope_uses, key=lambda u: (self._use_time(u, snapshots), u.snapshot_id))
            baseline_time = self._use_time(latest, snapshots)
            change = self._detect_change(snapshots[latest.snapshot_id], baseline_time, events)
            if change is not None and change.changed_at < window_start:
                change = None
            return latest, baseline_time, change

        personal_latest_time: Dict[str, datetime] = {}
        items: List[_Item] = []
        for pm_id in sorted(by_pm):
            group = by_pm[pm_id]
            personal = [u for u in group if u.source == "personal_lab"]
            shared = [u for u in group if u.source == "opted_in_project"]
            events = catalog.get(pm_id, [])

            personal_result = detect(personal, events)
            shared_result = detect(shared, events)
            if personal_result is not None:
                personal_latest_time[pm_id] = personal_result[1]
            personal_hit = personal_result is not None and personal_result[2] is not None
            shared_hit = shared_result is not None and shared_result[2] is not None
            if not personal_hit and not shared_hit:
                continue

            latest, baseline_time, change = personal_result if personal_hit else shared_result
            experiment_ids = sorted({u.experiment_id for u in personal if u.experiment_id})
            shared_calls = sum(u.calls for u in shared)
            model_name = names["model"].get(latest.model_id, latest.model_id)
            provider_name = names["provider"].get(latest.provider_id, latest.provider_id)

            reason_codes = set(self._model_reason_codes(change.changes))
            if personal_hit:
                reason_codes.add(StayAheadReasonCode.PERSONAL_USAGE.value)
            if shared:
                reason_codes.add(StayAheadReasonCode.OPTED_IN_PROJECT_USAGE.value)
            usage_scope = ("personal_and_shared" if shared else "personal") if personal_hit else "shared_project"

            if personal_hit:
                title = f"{model_name} changed since you last used it"
                why = (
                    f"You used this model in {_plural(len(experiment_ids), 'Personal Lab experiment')} "
                    f"(last on {_day(baseline_time)}), and its catalog record has changed since then."
                )
                if shared:
                    why += f" It was also used in {_plural(shared_calls, 'call')} in opted-in projects you can access."
            else:
                title = f"{model_name} changed since it was last used in an opted-in project"
                why = (
                    f"This model was used in an opted-in project you can access ({_plural(shared_calls, 'call')}, "
                    f"last on {_day(baseline_time)}), and its catalog record has changed since then. "
                    "This is shared project usage; it does not mean you personally used it."
                )

            refs = [
                StayAheadEvidenceRef(type="provider_model_snapshot", id=change.baseline.id, role="last_used_record"),
                StayAheadEvidenceRef(type="provider_model_snapshot", id=change.latest.id, role="latest_catalog_record"),
            ] + [StayAheadEvidenceRef(type="experiment", id=eid, role="used_in") for eid in experiment_ids[:5]]
            links = [StayAheadLink(kind="model", id=latest.model_id, label="Review change")]
            if experiment_ids:
                newest_experiment = max(
                    (u for u in personal if u.experiment_id), key=lambda u: (self._use_time(u, snapshots), u.experiment_id)
                )
                links.append(StayAheadLink(kind="experiment", id=newest_experiment.experiment_id, label="Open previous experiment"))

            signal = StayAheadSignal(
                id=f"{StayAheadFamily.USED_MODEL_CHANGED.value}:{pm_id}",
                family=StayAheadFamily.USED_MODEL_CHANGED,
                title=title,
                what_changed=f"Recorded changes since then: {self._describe_changes(change.changes)}.",
                why=why,
                reason_codes=sorted(reason_codes),
                since=baseline_time,
                changed_at=change.changed_at,
                subject={
                    "kind": "model",
                    "id": latest.model_id,
                    "name": model_name,
                    "provider": provider_name,
                    "usage_scope": usage_scope,
                },
                changes=change.changes,
                evidence_refs=refs,
                links=links,
            )
            items.append(_Item(signal=signal))
        return items, personal_latest_time

    # -- EXPERIMENT_MAY_BE_STALE --------------------------------------------------

    def _complete_experiments(self, user_id: str, experiment_ids: Set[str]) -> Dict[str, Experiment]:
        """Owner's fully executed experiments, by the canonical definition.

        1. ``ExperimentExecutionService._execution_state`` (read-only; the
           settling ``refresh`` is deliberately not called) must report
           COMPLETED — the same rule Count Toward Learning uses.
        2. Every slot's Agent Run(s) must be terminal success
           (``AgentRunStatus.COMPLETED``) with a recorded model snapshot. The
           engine claims the Agent Run COMPLETED before it completes the Task
           Run, so a Task Run alone is not enough to trust the run as a use.

        ``Experiment.status`` is not consulted (it is settled lazily by a
        service whose read path writes). Only the columns needed are loaded.
        """
        complete: Dict[str, Experiment] = {}
        engine = ExperimentExecutionService(self.db)
        for chunk in _chunks(experiment_ids):
            candidates = (
                self.db.execute(
                    select(Experiment)
                    .options(
                        load_only(
                            Experiment.id,
                            Experiment.user_id,
                            Experiment.status,
                            Experiment.concept_id,
                            Experiment.experiment_type,
                            Experiment.created_at,
                        )
                    )
                    .where(
                        Experiment.id.in_(chunk),
                        Experiment.user_id == user_id,
                        Experiment.status != ExperimentStatus.CANCELLED,
                    )
                )
                .scalars()
                .all()
            )
            slot_agents: Dict[str, Dict[str, List[Tuple[Optional[AgentRunStatus], Optional[str]]]]] = {}
            for experiment_id, slot_id, agent_status, snapshot_id in self.db.execute(
                select(
                    ExperimentTaskRun.experiment_id,
                    ExperimentTaskRun.id,
                    AgentRun.status,
                    AgentRun.provider_model_snapshot_id,
                )
                .select_from(ExperimentTaskRun)
                .outerjoin(AgentRun, AgentRun.task_run_id == ExperimentTaskRun.task_run_id)
                .where(ExperimentTaskRun.experiment_id.in_(chunk))
            ).all():
                slot_agents.setdefault(experiment_id, {}).setdefault(slot_id, []).append((agent_status, snapshot_id))

            for experiment in candidates:
                execution_status, _runs = engine._execution_state(experiment)
                if execution_status != "COMPLETED":
                    continue
                slots = slot_agents.get(experiment.id, {})
                if not slots or not all(
                    all(status == AgentRunStatus.COMPLETED and snapshot is not None for status, snapshot in agents)
                    for agents in slots.values()
                ):
                    continue
                complete[experiment.id] = experiment
        return complete

    def _experiment_signals(
        self,
        user_id: str,
        uses: List[_Use],
        snapshots: Dict[str, ProviderModelSnapshot],
        catalog: Dict[str, List[ProviderModelSnapshot]],
        names: Dict[str, Dict[str, str]],
        personal_latest_time: Dict[str, datetime],
        window_start: datetime,
    ) -> List[_Item]:
        complete = self._complete_experiments(user_id, {u.experiment_id for u in uses if u.experiment_id})
        if not complete:
            return []

        # Latest use of each provider model within each complete experiment.
        per_experiment: Dict[str, Dict[str, _Use]] = {}
        for use in uses:
            if not use.experiment_id or use.experiment_id not in complete:
                continue
            pm_id = snapshots[use.snapshot_id].provider_model_id
            current = per_experiment.setdefault(use.experiment_id, {}).get(pm_id)
            if current is None or (self._use_time(use, snapshots), use.snapshot_id) > (
                self._use_time(current, snapshots),
                current.snapshot_id,
            ):
                per_experiment[use.experiment_id][pm_id] = use

        counted: Set[str] = set()
        for chunk in _chunks(per_experiment):
            counted.update(
                self.db.execute(
                    select(LearningEvidence.ref_id).where(
                        LearningEvidence.user_id == user_id,
                        LearningEvidence.ref_type == EvidenceRefType.EXPERIMENT,
                        LearningEvidence.ref_id.in_(chunk),
                    )
                ).scalars()
            )
        concept_names = self._concept_names({e.concept_id for e in complete.values() if e.concept_id})

        items: List[_Item] = []
        for experiment_id in sorted(per_experiment):
            experiment = complete[experiment_id]
            concept_id, experiment_type, created_at = (
                experiment.concept_id,
                experiment.experiment_type,
                experiment.created_at,
            )
            changed: List[Tuple[str, _Use, _ModelChange]] = []
            for pm_id, use in sorted(per_experiment[experiment_id].items()):
                change = self._detect_change(
                    snapshots[use.snapshot_id], self._use_time(use, snapshots), catalog.get(pm_id, [])
                )
                if change is None or change.changed_at < window_start:
                    continue
                # Re-used by THIS user after the change: they have already seen
                # the model as it is now, so the experiment is not flagged.
                # Another member's project call never counts as that.
                if personal_latest_time.get(pm_id, change.changed_at) >= change.changed_at:
                    continue
                changed.append((pm_id, use, change))
            if not changed:
                continue

            model_labels = [names["model"].get(use.model_id, use.model_id) for _, use, _ in changed]
            what = " ".join(
                f"{names['model'].get(use.model_id, use.model_id)} — {self._describe_changes(change.changes)}."
                for _, use, change in changed
            )
            reason_codes = {StayAheadReasonCode.PERSONAL_USAGE.value}
            refs = [StayAheadEvidenceRef(type="experiment", id=experiment_id, role="experiment")]
            for _, use, change in changed:
                reason_codes.update(self._model_reason_codes(change.changes))
                refs.append(StayAheadEvidenceRef(type="provider_model_snapshot", id=change.baseline.id, role="recorded_in_experiment"))
                refs.append(StayAheadEvidenceRef(type="provider_model_snapshot", id=change.latest.id, role="latest_catalog_record"))
            if experiment_id in counted:
                reason_codes.add(StayAheadReasonCode.EXPERIMENT_COUNTED_TOWARD_LEARNING.value)
            label = _EXPERIMENT_LABEL.get(experiment_type, "experiment")
            since = min(self._use_time(use, snapshots) for _, use, _ in changed)
            signal = StayAheadSignal(
                id=f"{StayAheadFamily.EXPERIMENT_MAY_BE_STALE.value}:{experiment_id}",
                family=StayAheadFamily.EXPERIMENT_MAY_BE_STALE,
                title=f"Your {label} from {_day(created_at)} may be out of date",
                what_changed=what,
                why=(
                    f"This Personal Lab experiment ran on {', '.join(model_labels)} on {_day(since)}. "
                    "The catalog record for "
                    f"{'that model' if len(changed) == 1 else 'those models'} has changed since, so the results may not "
                    "describe the model as it is now."
                    + (" You counted it toward your learning." if experiment_id in counted else "")
                ),
                reason_codes=sorted(reason_codes),
                since=since,
                changed_at=max(change.changed_at for _, _, change in changed),
                subject={"kind": "experiment", "id": experiment_id, "experiment_type": experiment_type.value, "label": label},
                changes=[
                    {"model_id": use.model_id, "model": names["model"].get(use.model_id, use.model_id), "changes": change.changes}
                    for _, use, change in changed
                ],
                evidence_refs=refs,
                links=[StayAheadLink(kind="experiment", id=experiment_id, label="Open previous experiment")]
                + [StayAheadLink(kind="model", id=use.model_id, label="Review change") for _, use, _ in changed[:2]],
                concept=(
                    {"id": concept_id, **concept_names[concept_id]} if concept_id and concept_id in concept_names else None
                ),
            )
            items.append(_Item(signal=signal, concept_ids={concept_id} if concept_id else set()))
        return items

    def _concept_names(self, concept_ids: Set[str]) -> Dict[str, Dict[str, str]]:
        found: Dict[str, Dict[str, str]] = {}
        for chunk in _chunks(concept_ids):
            for cid, slug, name in self.db.execute(
                select(Concept.id, Concept.slug, Concept.name).where(Concept.id.in_(chunk))
            ).all():
                found[cid] = {"slug": slug, "name": name}
        return found

    # -- WATCHED_DEVELOPMENT_CHANGED ----------------------------------------------

    def _watched_signals(self, user_id: str, window_start: datetime) -> List[_Item]:
        triages = self.db.execute(
            select(TriageDecision).where(
                TriageDecision.user_id == user_id,
                TriageDecision.decision == TriageDecisionKind.WATCH,
                TriageDecision.development_id.isnot(None),
                TriageDecision.superseded_by_id.is_(None),
            )
        ).scalars().all()
        items: List[_Item] = []
        for triage in sorted(triages, key=lambda t: (t.development_id or "", t.id)):
            development = self.db.get(Development, triage.development_id)
            if development is None or development.status != DevelopmentStatus.ACTIVE:
                continue
            watch_since = _aware(triage.decided_at)
            floor = max(watch_since, window_start)

            claims = [
                c
                for c in self.db.execute(
                    select(Claim).where(Claim.development_id == development.id, Claim.status == ClaimStatus.ACTIVE)
                ).scalars()
                if c.claim_type not in _NON_QUALIFYING_CLAIM_TYPES and _aware(c.created_at) > floor
            ]
            links = self.db.execute(
                select(DevelopmentConcept).where(
                    DevelopmentConcept.development_id == development.id,
                    DevelopmentConcept.state == DevelopmentConceptState.CONFIRMED,
                )
            ).scalars().all()
            new_links = [k for k in links if k.reviewed_at is not None and _aware(k.reviewed_at) > floor]
            revisit_at = _aware(triage.revisit_at)
            revisit_reached = revisit_at is not None and window_start <= revisit_at <= self.now

            reason_codes = {StayAheadReasonCode.WATCHING_DEVELOPMENT.value}
            what: List[str] = []
            times: List[datetime] = []
            refs = [
                StayAheadEvidenceRef(type="triage_decision", id=triage.id, role="your_watch"),
                StayAheadEvidenceRef(type="development", id=development.id, role="development"),
            ]
            if claims:
                reason_codes.add(StayAheadReasonCode.WATCH_NEW_EVIDENCE.value)
                counts: Dict[ClaimType, int] = {}
                for c in claims:
                    counts[c.claim_type] = counts.get(c.claim_type, 0) + 1
                what.append(
                    "New evidence recorded since you started watching: "
                    + ", ".join(_plural(n, _CLAIM_LABEL[t]) for t, n in sorted(counts.items(), key=lambda kv: kv[0].value))
                    + "."
                )
                times.extend(_aware(c.created_at) for c in claims)
                refs.extend(
                    StayAheadEvidenceRef(type="claim", id=c.id, role=c.claim_type.value)
                    for c in sorted(claims, key=lambda c: (_aware(c.created_at), c.id))[:5]
                )
            if new_links:
                reason_codes.add(StayAheadReasonCode.WATCH_CONCEPT_LINK_CONFIRMED.value)
                what.append(f"{_plural(len(new_links), 'Concept link')} confirmed since you started watching.")
                times.extend(_aware(k.reviewed_at) for k in new_links)
            if revisit_reached:
                reason_codes.add(StayAheadReasonCode.WATCH_REVISIT_DATE_REACHED.value)
                what.append(f"The revisit date you set ({_day(revisit_at)}) has arrived.")
                times.append(revisit_at)
            if not what:
                continue

            concept_ids = {k.concept_id for k in links}
            verification = derive_verification(self.db, development.id)
            signal = StayAheadSignal(
                id=f"{StayAheadFamily.WATCHED_DEVELOPMENT_CHANGED.value}:{development.id}",
                family=StayAheadFamily.WATCHED_DEVELOPMENT_CHANGED,
                title=development.title,
                what_changed=" ".join(what),
                why=(
                    f"You chose to watch this development on {_day(watch_since)}. "
                    f"Current verification: {verification}."
                ),
                reason_codes=sorted(reason_codes),
                since=watch_since,
                changed_at=max(times),
                subject={
                    "kind": "development",
                    "id": development.id,
                    "name": development.title,
                    "development_type": development.development_type,
                    "verification_level": verification,
                },
                evidence_refs=refs,
                links=[StayAheadLink(kind="development", id=development.id, label="Inspect evidence")],
            )
            items.append(_Item(signal=signal, concept_ids=concept_ids))
        return items

    # -- CONCEPT_CHANGED -----------------------------------------------------------

    def _learned_concepts(self, user_id: str) -> Dict[str, LearnerConceptState]:
        """Concepts on which the canonical Learner State Service says the
        user has reached at least EXPOSED. (Reuses the one learner-state
        engine; a failed check or a self-report alone is NOT_STARTED.)"""
        concept_ids = self.db.execute(
            select(LearningEvidence.concept_id).where(LearningEvidence.user_id == user_id).distinct()
        ).scalars().all()
        learned: Dict[str, LearnerConceptState] = {}
        for concept_id in sorted(concept_ids):
            state = self._learner_state.state(user_id, concept_id, now=self.now)
            if state.ladder != NOT_STARTED:
                learned[concept_id] = state
        return learned

    def _concept_signals(
        self, user_id: str, learned: Dict[str, LearnerConceptState], window_start: datetime
    ) -> List[_Item]:
        watch_interests = {
            i.concept_id: _aware(i.created_at)
            for i in self.db.execute(
                select(LearnerInterest).where(
                    LearnerInterest.user_id == user_id,
                    LearnerInterest.concept_id.isnot(None),
                    LearnerInterest.watch.is_(True),
                )
            ).scalars()
        }
        targets = sorted(set(learned) | set(watch_interests))
        if not targets:
            return []
        names = self._concept_names(set(targets))

        link_rows: Dict[str, List[Tuple[DevelopmentConcept, Development]]] = {}
        for chunk in _chunks(targets):
            for link, development in self.db.execute(
                select(DevelopmentConcept, Development)
                .join(Development, Development.id == DevelopmentConcept.development_id)
                .where(
                    DevelopmentConcept.concept_id.in_(chunk),
                    DevelopmentConcept.state == DevelopmentConceptState.CONFIRMED,
                    Development.status == DevelopmentStatus.ACTIVE,
                )
            ).all():
                link_rows.setdefault(link.concept_id, []).append((link, development))

        verification_cache: Dict[str, str] = {}
        items: List[_Item] = []
        for concept_id in targets:
            if concept_id not in names:
                continue
            state = learned.get(concept_id)
            versions = self._concepts.list_versions(concept_id)
            published = [v for v in versions if v.status in (VersionStatus.ACTIVE, VersionStatus.DEPRECATED) and v.published_at]

            if state is not None:
                counted = [
                    e
                    for e in state.evidence
                    if e.evidence_type != EvidenceType.SELF_REPORT and e.passed is not False
                ] or [e for e in state.evidence if e.evidence_type != EvidenceType.SELF_REPORT]
                baseline_time = max(_aware(e.created_at) for e in counted)
                by_id = {v.id: v.version for v in versions}
                cited = [by_id[e.concept_version_id] for e in counted if e.concept_version_id in by_id]
                baseline_version = max(cited) if cited else 0
                newer = [v for v in published if v.version > baseline_version]
                basis = f"{_LADDER_LABEL[state.ladder]} evidence recorded against version {baseline_version}"
                relationship = StayAheadReasonCode.HAS_LEARNING_EVIDENCE.value
            else:
                baseline_time = watch_interests[concept_id]
                newer = [v for v in published if _aware(v.published_at) > baseline_time]
                basis = "a Concept you chose to watch"
                relationship = StayAheadReasonCode.WATCHING_CONCEPT.value

            reason_codes: Set[str] = {relationship}
            what: List[str] = []
            changes: List[Dict[str, Any]] = []
            refs: List[StayAheadEvidenceRef] = []
            links: List[StayAheadLink] = []
            times: List[datetime] = []

            material = [
                v
                for v in newer
                if v.change_severity == ChangeSeverity.MATERIAL and _aware(v.published_at) >= window_start
            ]
            for v in sorted(material, key=lambda v: v.version):
                reason_codes.add(StayAheadReasonCode.CONCEPT_NEW_MATERIAL_VERSION.value)
                note = f": {v.change_note}" if v.change_note else ""
                what.append(f"Version {v.version} was published as a material change{note}")
                changes.append(
                    {
                        "kind": "concept_version",
                        "version": v.version,
                        "change_severity": v.change_severity.value,
                        "change_note": v.change_note,
                        "published_at": _aware(v.published_at).isoformat(),
                    }
                )
                refs.append(StayAheadEvidenceRef(type="concept_version", id=v.id, role="new_material_version"))
                times.append(_aware(v.published_at))

            for link, development in sorted(link_rows.get(concept_id, []), key=lambda r: (r[1].id)):
                dev_time = _aware(development.announced_at or development.first_seen_at)
                signal_time = max(dev_time, _aware(link.reviewed_at) or dev_time)
                if dev_time <= baseline_time or signal_time < window_start:
                    continue
                if development.id not in verification_cache:
                    verification_cache[development.id] = derive_verification(self.db, development.id)
                level = verification_cache[development.id]
                if _VERIFICATION_ORDER.index(level) < _VERIFICATION_ORDER.index(_MIN_CONCEPT_DEVELOPMENT_VERIFICATION):
                    continue  # unverified provider claims / attention alone don't change a Concept
                reason_codes.add(StayAheadReasonCode.CONCEPT_NEW_LINKED_DEVELOPMENT.value)
                what.append(f"A new development is linked to this Concept: “{development.title}” ({level}).")
                changes.append(
                    {
                        "kind": "linked_development",
                        "development_id": development.id,
                        "title": development.title,
                        "verification_level": level,
                    }
                )
                refs.append(StayAheadEvidenceRef(type="development", id=development.id, role="linked_development"))
                links.append(StayAheadLink(kind="development", id=development.id, label="Inspect evidence"))
                times.append(signal_time)

            if not times:
                continue
            name = names[concept_id]["name"]
            items.append(
                _Item(
                    signal=StayAheadSignal(
                        id=f"{StayAheadFamily.CONCEPT_CHANGED.value}:{concept_id}",
                        family=StayAheadFamily.CONCEPT_CHANGED,
                        title=f"“{name}” has changed",
                        what_changed=" ".join(what),
                        why=f"This Concept is on your radar because of {basis}.",
                        reason_codes=sorted(reason_codes),
                        since=baseline_time,
                        changed_at=max(times),
                        subject={"kind": "concept", "id": concept_id, "name": name, "slug": names[concept_id]["slug"]},
                        changes=changes,
                        evidence_refs=refs,
                        links=links,
                        concept={"id": concept_id, **names[concept_id]},
                        learner_state=(
                            {"ladder": state.ladder, "overlays": sorted(state.overlays)} if state is not None else None
                        ),
                    ),
                    concept_ids={concept_id},
                )
            )
        return items

    # -- AIL.4B: review section ------------------------------------------------------

    def _review_cards(
        self, user_id: str, learned: Dict[str, LearnerConceptState]
    ) -> Tuple[List[StayAheadReviewCard], PromptPreview]:
        """One card per Concept whose review prompt has been DELIVERED this week
        and that is still REVIEW_DUE or REVIEW_FAILED, straight from the
        learner-state overlays (``LearnerStateService`` stays the authority).

        Strictly read-only: prompt delivery is written only by the explicit
        allocation action (``ReviewPromptService.allocate``), never here, so
        recomputing or refreshing Today cannot consume the weekly quota. A
        Concept gets a single card: a failed latest review takes precedence
        over a material change, which takes precedence over an elapsed
        interval."""
        prompts = ReviewPromptService(self.db, now=self.now).preview(user_id, learned=learned)
        delivered = {d.concept_id: d for d in prompts.delivered}
        candidates = [
            (concept_id, state)
            for concept_id, state in sorted(learned.items())
            if concept_id in delivered and state.review is not None and (state.review.failed or state.review.due)
        ]
        if not candidates:
            return [], prompts
        names = self._concept_names({cid for cid, _ in candidates})
        reviews = ReviewAttemptService(self.db, now=self.now)
        cards: List[StayAheadReviewCard] = []
        for concept_id, state in candidates:
            review = state.review
            info = names.get(concept_id)
            if info is None:
                continue
            name = info["name"]
            if review.failed:
                kind = "REVIEW_FAILED"
                title = f"Your latest review of “{name}” needs attention"
                what = "Your most recent review of this Concept did not pass."
                why = (
                    "It is still recorded as Demonstrated — a failed review never changes that. "
                    "This is a prompt to review it again."
                )
            elif ReviewReasonCode.REVIEW_CONCEPT_CHANGED.value in review.due_reasons:
                kind = "CONCEPT_CHANGED_REVIEW"
                latest = max(review.material_changes, key=lambda c: c["version"])
                note = f": {latest['change_note']}" if latest["change_note"] else "."
                title = f"“{name}” changed; a review is recommended"
                what = f"Version {latest['version']} was published as a material change{note}"
                why = (
                    f"You demonstrated this Concept against version {review.baseline.concept_version or '?'}. "
                    "A review checks that you still hold it as it is now."
                )
            else:
                kind = "REVIEW_DUE"
                title = f"Review due: “{name}”"
                what = f"Your review interval for this Concept ({review.interval_days} days) has elapsed."
                extended = (
                    f", extended after {_plural(review.successful_reviews_on_schedule, 'successful review')}" if review.successful_reviews_on_schedule else ""
                )
                why = (
                    f"Your latest evidence for this {review.kind} Concept is from {_day(review.baseline.recorded_at)}. "
                    f"The interval is {review.base_interval_days} days{extended}, and it has passed."
                )
            eligibility = "core Concept" if "core" in review.eligible_via else "Concept in your active plan"
            if not review.failed:
                why += f" It is reviewed because it is a {eligibility}."

            attempt_view = review.active_attempt or review.latest_completed
            availability = reviews.availability(user_id, concept_id, state=state)
            action: Dict[str, Any] = {"kind": {"start": "start_review", "continue": "continue_review"}.get(
                availability.action, "unavailable"
            )}
            if availability.reason:
                action["reason"] = availability.reason
            if availability.available_after:
                action["available_after"] = availability.available_after
            if availability.learning_item_id:
                action["learning_item_id"] = availability.learning_item_id
            if availability.attempt_id:
                action["attempt_id"] = availability.attempt_id

            refs: List[StayAheadEvidenceRef] = []
            if review.baseline is not None:
                refs.append(StayAheadEvidenceRef(type="learning_evidence", id=review.baseline.evidence_id, role="baseline"))
                refs.append(
                    StayAheadEvidenceRef(
                        type="concept_version", id=review.baseline.concept_version_id, role="baseline_version"
                    )
                )
            for change in review.material_changes:
                refs.append(StayAheadEvidenceRef(type="concept_version", id=change["concept_version_id"], role="new_material_version"))
            if attempt_view is not None:
                refs.append(StayAheadEvidenceRef(type="review_attempt", id=attempt_view.id, role="attempt"))

            cards.append(
                StayAheadReviewCard(
                    id=f"REVIEW:{concept_id}",
                    kind=kind,
                    title=title,
                    what=what,
                    why=why,
                    reason_codes=list(review.reason_codes),
                    concept={"id": concept_id, "kind": review.kind, **info},
                    learner_state={"ladder": state.ladder, "overlays": sorted(state.overlays)},
                    baseline=(
                        {
                            "evidence_id": review.baseline.evidence_id,
                            "evidence_type": review.baseline.evidence_type,
                            "recorded_at": review.baseline.recorded_at,
                            "concept_version": review.baseline.concept_version,
                        }
                        if review.baseline is not None
                        else None
                    ),
                    interval={
                        "days": review.interval_days,
                        "base_days": review.base_interval_days,
                        "successful_reviews_on_schedule": review.successful_reviews_on_schedule,
                        "due_at": review.due_at,
                    },
                    material_changes=[
                        {**c, "published_at": c["published_at"].isoformat() if c["published_at"] else None}
                        for c in review.material_changes
                    ],
                    attempt=(
                        {
                            "id": attempt_view.id,
                            "status": attempt_view.status,
                            "started_at": attempt_view.started_at,
                            "completed_at": attempt_view.completed_at,
                        }
                        if attempt_view is not None
                        else None
                    ),
                    action=action,
                    evidence_refs=refs,
                    due_at=review.due_at,
                    prompt={
                        "delivered_at": _aware(delivered[concept_id].delivered_at),
                        "week_start": _aware(delivered[concept_id].week_start),
                        "slot": delivered[concept_id].slot,
                    },
                )
            )
        # Presentation order only, on visible fields (the same order the allocator
        # uses): failed reviews first, then material changes, then elapsed
        # intervals; within a group the longest overdue first, then name. No score.
        return (
            sorted(cards, key=lambda c: prompt_sort_key(c.kind, c.due_at, c.concept["name"], c.concept["id"])),
            prompts,
        )

    # -- WORTH_REVISITING (rollup) --------------------------------------------------

    @staticmethod
    def _as_reason(signal: StayAheadSignal) -> StayAheadReason:
        return StayAheadReason(**{k: getattr(signal, k) for k in StayAheadReason.model_fields})

    def _worth_revisiting(
        self, learned: Dict[str, LearnerConceptState], absorbed: Dict[str, List[StayAheadReason]]
    ) -> List[StayAheadSignal]:
        if not absorbed:
            return []
        names = self._concept_names(set(absorbed))
        cards: List[StayAheadSignal] = []
        for concept_id, reasons in absorbed.items():
            if concept_id not in names:
                continue
            state = learned[concept_id]
            ordered = sorted(reasons, key=lambda r: (-_aware(r.changed_at).timestamp(), r.id))
            codes = sorted({c for r in ordered for c in r.reason_codes} | {StayAheadReasonCode.HAS_LEARNING_EVIDENCE.value})
            distinct = len({c for r in ordered for c in r.reason_codes} - {StayAheadReasonCode.HAS_LEARNING_EVIDENCE.value})
            name = names[concept_id]["name"]
            evidence = [e for e in state.evidence if e.evidence_type != EvidenceType.SELF_REPORT and e.passed is not False]
            latest_evidence = sorted(evidence, key=lambda e: (_aware(e.created_at), e.id))[-3:]
            cards.append(
                StayAheadSignal(
                    id=f"{StayAheadFamily.WORTH_REVISITING.value}:{concept_id}",
                    family=StayAheadFamily.WORTH_REVISITING,
                    title=f"Worth revisiting: {name}",
                    what_changed=f"{_plural(len(ordered), 'recorded change')} relate to this Concept: "
                    + ", ".join(sorted({r.family.value.replace('_', ' ').lower() for r in ordered}))
                    + ".",
                    why=(
                        f"You have {_LADDER_LABEL[state.ladder]} evidence for “{name}”, and the recorded changes below "
                        "are connected to it."
                    ),
                    reason_codes=codes,
                    since=max((_aware(e.created_at) for e in evidence), default=None),
                    changed_at=max(_aware(r.changed_at) for r in ordered),
                    subject={"kind": "concept", "id": concept_id, "name": name, "slug": names[concept_id]["slug"]},
                    evidence_refs=[
                        StayAheadEvidenceRef(type="learning_evidence", id=e.id, role=e.evidence_type.value)
                        for e in latest_evidence
                    ],
                    concept={"id": concept_id, **names[concept_id]},
                    learner_state={"ladder": state.ladder, "overlays": sorted(state.overlays)},
                    distinct_reason_count=distinct,
                    reasons=ordered,
                )
            )
        # Presentation order only, on visible fields: learner-state rung,
        # number of distinct reasons, most recent change, then name.
        return sorted(
            cards,
            key=lambda c: (
                -_LADDER_RANK[c.learner_state["ladder"]],
                -(c.distinct_reason_count or 0),
                -_aware(c.changed_at).timestamp(),
                c.title.lower(),
                c.id,
            ),
        )
