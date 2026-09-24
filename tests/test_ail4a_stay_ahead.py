"""AIL.4A Personal Stay-Ahead Today.

Built through production paths (LabService, ExperimentExecutionService,
ConceptGraphService, LearningEvidenceService); only registry snapshots, Radar
ledger rows and Task Run outcomes are seeded. Time is injected (``NOW``) so
window boundaries are exact."""

import itertools
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, select, text

from app.auth import get_current_user
from app.db.base import Base
from app.db.enums import (
    AgentRunStatus,
    ChangeSeverity,
    ConceptKind,
    EvidenceType,
    ExperimentStatus,
    ExperimentType,
    GradingMode,
    ModelCallStatus,
    ProjectRole,
    SnapshotSource,
    TaskRunStatus,
)
from app.main import app as fastapi_app
from app.models.execution import ModelCall
from app.models.identity import ProjectMembership
from app.models.lab import Experiment, ExperimentTaskRun
from app.models.learner import LearnerInterest
from app.models.providers import ProviderModelSnapshot
from app.models.radar import (
    Claim,
    ClaimCreationMethod,
    ClaimStatus,
    ClaimType,
    Development,
    DevelopmentConcept,
    DevelopmentConceptProposedBy,
    DevelopmentConceptState,
    DevelopmentStatus,
    TriageDecision,
    TriageDecisionKind,
)
from app.models.tasks import AgentRun, TaskRun
from app.schemas.lab import ExperimentCreate, ExperimentModelSelection
from app.services.concept_graph_service import ConceptGraphService
from app.services.experiment_execution_service import ExperimentExecutionService
from app.services.lab_service import LabService
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.stay_ahead_service import MAX_WINDOW_DAYS, StayAheadService
from tests.ail1a_factories import make_concept, make_published_version, make_user
from tests.conftest import make_agent_run, make_agent_version, make_project, make_task, make_task_run
from tests.test_ail3a_personal_lab import _active_agent, _model_bundle

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
_counter = itertools.count(1)


def ago(days=0.0, base=NOW):
    return base - timedelta(days=days)


# -- builders -------------------------------------------------------------------


def _bundle(db, name):
    return _model_bundle(db, f"stay/{name}", f"{name}-provider")


def _snap(
    db,
    bundle,
    at,
    *,
    source=SnapshotSource.CATALOG_REFRESH,
    price=("1.00", "2.00"),
    context=128000,
    caps=None,
    status=None,
    kinds=None,
):
    model, provider, pm = bundle
    capability = None
    if caps is not None or status is not None:
        capability = dict(caps or {})
        if status is not None:
            capability["model_status"] = status
    row = ProviderModelSnapshot(
        provider_model_id=pm.id,
        model_id=model.id,
        provider_id=provider.id,
        pricing_input_per_mtok=None if price is None else Decimal(price[0]),
        pricing_output_per_mtok=None if price is None else Decimal(price[1]),
        currency="USD",
        context_window=context,
        capability_snapshot=capability,
        snapshotted_at=at,
        source=source,
        change_kinds=kinds,
    )
    db.add(row)
    db.commit()
    return row


def _experiment(db, bootstrap, snapshots, *, concept_id=None, complete=True, started=None, hypothesis="a hypothesis",
                user=None, project=None, agent_status=AgentRunStatus.COMPLETED, agent_status_by_label=None):
    """A launched Model Face-off over ``snapshots`` whose slots ran on exactly
    those pinned snapshots (what the execution engine records)."""
    user = user or bootstrap.user
    project = project or bootstrap.project
    service = LabService(db)
    _, versions = service.starter_kits(user)
    agent = _active_agent(db, project.id, next(_counter) + 100, f"Stay Agent {next(_counter)}")
    db.commit()
    experiment = service.create_experiment(
        user,
        ExperimentCreate(
            experiment_type=ExperimentType.MODEL_COMPARISON,
            hypothesis=hypothesis,
            eval_set_version_id=versions[0].id,
            agent_version_ids=[agent.id],
            models=[
                ExperimentModelSelection(model_id=s.model_id, provider_model_snapshot_id=s.id) for s in snapshots
            ],
            repetitions=1,
            concept_id=concept_id,
        ),
    )
    ExperimentExecutionService(db).launch(user.id, experiment.id)
    for slot in db.execute(select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)).scalars():
        snapshot = snapshots[int(slot.label.split("-")[1]) - 1]
        agent_run = db.execute(select(AgentRun).where(AgentRun.task_run_id == slot.task_run_id)).scalar_one()
        agent_run.model_id = snapshot.model_id
        agent_run.provider_id = snapshot.provider_id
        agent_run.provider_model_snapshot_id = snapshot.id
        task_run = db.get(TaskRun, slot.task_run_id)
        task_run.started_at = started
        if complete:
            task_run.status = TaskRunStatus.COMPLETED
            agent_run.status = (agent_status_by_label or {}).get(slot.label, agent_status)
    db.commit()
    return experiment


def _control(db, at):
    """A second, never-changing model so a Model Face-off can be created."""
    bundle = _bundle(db, f"control{next(_counter)}")
    return _snap(db, bundle, at, source=SnapshotSource.EXECUTION_FREEZE)


def _lab_used(db, bootstrap, bundle, *, used_at, **experiment_kwargs):
    """Experiment that ran ``bundle`` at ``used_at`` on a frozen snapshot."""
    frozen = _snap(db, bundle, used_at, source=SnapshotSource.EXECUTION_FREEZE)
    experiment = _experiment(
        db, bootstrap, [frozen, _control(db, used_at)], started=used_at, **experiment_kwargs
    )
    return experiment, frozen


def _today(db, user, *, now=NOW, window_days=30):
    return StayAheadService(db, now=now).today(user.id, window_days=window_days)


def _ids(section):
    return [item.id for item in section.items]


def _all_ids(result):
    sections = result.sections
    return [
        i
        for s in (
            sections.worth_revisiting,
            sections.used_models_changed,
            sections.watched_developments,
            sections.experiments_to_rerun,
            sections.concepts_changed,
        )
        for i in _ids(s)
    ]


def _empty(result):
    return all(
        s.total == 0
        for s in (
            result.sections.worth_revisiting,
            result.sections.used_models_changed,
            result.sections.watched_developments,
            result.sections.experiments_to_rerun,
            result.sections.concepts_changed,
        )
    )


def _development(db, *, announced=None, first_seen=None, status=DevelopmentStatus.ACTIVE, title="A development"):
    key = f"dev-{next(_counter)}"
    row = Development(
        title=title,
        development_type="capability",
        announced_at=announced,
        first_seen_at=first_seen or announced or NOW,
        candidate_key=key,
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def _claim(db, development, claim_type, at, status=ClaimStatus.ACTIVE):
    row = Claim(
        claim_type=claim_type,
        text=f"{claim_type.value} text",
        development_id=development.id,
        as_of=at,
        created_by=ClaimCreationMethod.RULE,
        status=status,
        created_at=at,
    )
    db.add(row)
    db.commit()
    return row


def _link(db, development, concept, *, state=DevelopmentConceptState.CONFIRMED, reviewed_at=None):
    row = DevelopmentConcept(
        development_id=development.id,
        concept_id=concept.id,
        state=state,
        proposed_by=DevelopmentConceptProposedBy.RULE,
        reviewed_at=reviewed_at,
    )
    db.add(row)
    db.commit()
    return row


def _watch(db, user, development, *, at, revisit_at=None, decision=TriageDecisionKind.WATCH, superseded=False,
           revisit_condition=None):
    row = TriageDecision(
        user_id=user.id,
        development_id=development.id,
        decision=decision,
        reason_codes=[],
        revisit_at=revisit_at,
        revisit_condition=revisit_condition,
        decided_at=at,
    )
    db.add(row)
    db.commit()
    if superseded:
        row.superseded_by_id = row.id
        db.commit()
    return row


def _concept(db, name=None, *, kind=ConceptKind.DEFINITIONAL):
    n = next(_counter)
    concept = make_concept(db, slug=f"stay-concept-{n}", name=name or f"Concept {n}", kind=kind)
    version = make_published_version(db, concept)
    return concept, version


def _publish(db, concept, *, at, severity, note=None, draft_only=False):
    service = ConceptGraphService(db)
    version = service.create_draft_version(
        concept_id=concept.id, plain_definition="Updated.", change_severity=severity, change_note=note
    )
    if draft_only:
        return version
    version = service.publish_version(version.id)
    version.published_at = at
    db.commit()
    return version


def _evidence(db, user, concept, version, *, at, passed=True, n=1, kind=EvidenceType.KNOWLEDGE_CHECK):
    rows = []
    for _ in range(n):
        row = LearningEvidenceService(db).record_evidence(
            user_id=user.id,
            concept_id=concept.id,
            concept_version_id=version.id,
            evidence_type=kind,
            grader=GradingMode.DETERMINISTIC,
            passed=passed,
        )
        row.created_at = at
        db.commit()
        rows.append(row)
    return rows


def _platform_call(db, project, user_project_member, bundle, snapshot, *, at, status=ModelCallStatus.SUCCESS,
                   created_by=None):
    model, provider, pm = bundle
    task = make_task(db, project)
    task.created_by = created_by  # the template's author: never treated as who ran it
    task_run = make_task_run(db, task, status=TaskRunStatus.COMPLETED)
    run = make_agent_run(db, task_run, make_agent_version(db))
    call = ModelCall(
        agent_run_id=run.id,
        model_id=model.id,
        provider_id=provider.id,
        provider_model_id=pm.id,
        provider_model_snapshot_id=snapshot.id,
        status=status,
        started_at=at,
    )
    db.add(call)
    db.commit()
    return call


def _second_user(db, bootstrap, email="second@example.com"):
    user = make_user(db, org=bootstrap.organization, email=email)
    project = make_project(db, bootstrap.organization, name=f"Project of {email}")
    db.add(ProjectMembership(project_id=project.id, user_id=user.id, role=ProjectRole.OWNER))
    db.commit()
    return user, project


# =============================================================================
# USED_MODEL_CHANGED and EXPERIMENT_MAY_BE_STALE
# =============================================================================


def test_price_change_after_a_lab_experiment_fires_used_model_and_stale_experiment(db, bootstrap):
    bundle = _bundle(db, "a")
    experiment, frozen = _lab_used(db, bootstrap, bundle, used_at=ago(20))
    change = _snap(db, bundle, ago(5), price=("1.00", "4.00"), kinds=["price"])

    result = _today(db, bootstrap.user)

    (model_signal,) = result.sections.used_models_changed.items
    assert model_signal.family.value == "USED_MODEL_CHANGED"
    assert "MODEL_PRICE_CHANGED" in model_signal.reason_codes
    assert "PERSONAL_USAGE" in model_signal.reason_codes
    assert model_signal.changes == [
        {"kind": "price", "fields": {"output_per_mtok": {"before": "2", "after": "4"}}}
    ]
    assert model_signal.changed_at == change.snapshotted_at
    assert {(r.type, r.id) for r in model_signal.evidence_refs} >= {
        ("provider_model_snapshot", frozen.id),
        ("provider_model_snapshot", change.id),
        ("experiment", experiment.id),
    }
    assert {l.kind for l in model_signal.links} == {"model", "experiment"}
    assert "best" not in (model_signal.title + model_signal.what_changed + model_signal.why).lower()

    (stale,) = result.sections.experiments_to_rerun.items
    assert stale.id == f"EXPERIMENT_MAY_BE_STALE:{experiment.id}"
    assert stale.subject["label"] == "Model Face-off"
    assert "MODEL_PRICE_CHANGED" in stale.reason_codes


def test_no_catalog_change_means_no_signal(db, bootstrap):
    _lab_used(db, bootstrap, _bundle(db, "quiet"), used_at=ago(20))
    assert _empty(_today(db, bootstrap.user))


def test_change_that_reverted_to_the_same_state_does_not_fire(db, bootstrap):
    bundle = _bundle(db, "reverted")
    _lab_used(db, bootstrap, bundle, used_at=ago(20))
    _snap(db, bundle, ago(9), price=("1.00", "9.00"), kinds=["price"])
    _snap(db, bundle, ago(5), price=("1.00", "2.00"), kinds=["price"])  # back to the original
    assert _empty(_today(db, bootstrap.user))


def test_a_change_recorded_before_the_run_is_not_news(db, bootstrap):
    bundle = _bundle(db, "pinned-old")
    old_catalog = _snap(db, bundle, ago(30))  # what the user pinned
    _snap(db, bundle, ago(15), price=("1.00", "5.00"), kinds=["price"])  # changed BEFORE the run
    _experiment(db, bootstrap, [old_catalog, _control(db, ago(10))], started=ago(10))
    assert _empty(_today(db, bootstrap.user))


def test_reusing_the_model_after_the_change_suppresses_both_signals(db, bootstrap):
    bundle = _bundle(db, "reused")
    _lab_used(db, bootstrap, bundle, used_at=ago(20))
    changed = _snap(db, bundle, ago(10), price=("1.00", "5.00"), kinds=["price"])
    # A later experiment ran on the new catalog record.
    _experiment(db, bootstrap, [changed, _control(db, ago(3))], started=ago(3))
    assert _empty(_today(db, bootstrap.user))


@pytest.mark.parametrize(
    "kwargs,kind,code",
    [
        ({"context": 256000, "kinds": ["context"]}, "context", "MODEL_CONTEXT_CHANGED"),
        (
            {"caps": {"tool_calling_support": "full", "structured_output_support": True, "vision_capability": False},
             "kinds": ["capability"]},
            "capability",
            "MODEL_CAPABILITY_CHANGED",
        ),
    ],
)
def test_context_and_capability_changes_are_detected(db, bootstrap, kwargs, kind, code):
    bundle = _bundle(db, f"dims-{kind}")
    frozen_caps = {"tool_calling_support": "none", "structured_output_support": False, "vision_capability": False}
    frozen = _snap(db, bundle, ago(20), source=SnapshotSource.EXECUTION_FREEZE, caps=frozen_caps)
    _experiment(db, bootstrap, [frozen, _control(db, ago(20))], started=ago(20))
    _snap(db, bundle, ago(5), **kwargs)
    (signal,) = _today(db, bootstrap.user).sections.used_models_changed.items
    assert [c["kind"] for c in signal.changes] == [kind]
    assert code in signal.reason_codes


def test_status_change_is_detected_from_a_catalog_baseline_and_from_a_freeze_baseline(db, bootstrap):
    # Catalog baseline records model_status, so it is compared directly.
    catalog_bundle = _bundle(db, "status-catalog")
    base = _snap(db, catalog_bundle, ago(20), status="active")
    _experiment(db, bootstrap, [base, _control(db, ago(20))], started=ago(20))
    _snap(db, catalog_bundle, ago(5), status="unavailable", kinds=["status"])
    # A freeze baseline has no status; only a non-active latest status counts.
    freeze_bundle = _bundle(db, "status-freeze")
    frozen = _snap(db, freeze_bundle, ago(20), source=SnapshotSource.EXECUTION_FREEZE)
    _experiment(db, bootstrap, [frozen, _control(db, ago(20))], started=ago(20))
    _snap(db, freeze_bundle, ago(6), status="unavailable", kinds=["status"])
    # ...and a model that came back to active again does not fire.
    back_bundle = _bundle(db, "status-back")
    frozen_back = _snap(db, back_bundle, ago(20), source=SnapshotSource.EXECUTION_FREEZE)
    _experiment(db, bootstrap, [frozen_back, _control(db, ago(20))], started=ago(20))
    _snap(db, back_bundle, ago(8), status="unavailable", kinds=["status"])
    _snap(db, back_bundle, ago(7), status="active", kinds=["status"])

    signals = _today(db, bootstrap.user).sections.used_models_changed.items
    assert len(signals) == 2
    assert all("MODEL_STATUS_CHANGED" in s.reason_codes for s in signals)


def test_unrecorded_baseline_values_never_fabricate_a_change(db, bootstrap):
    bundle = _bundle(db, "legacy")
    legacy = _snap(db, bundle, ago(20), source=SnapshotSource.LEGACY_UNKNOWN, price=None, context=None, caps=None)
    _experiment(db, bootstrap, [legacy, _control(db, ago(20))], started=ago(20))
    _snap(db, bundle, ago(5), price=("3.00", "6.00"), context=64000,
          caps={"tool_calling_support": "full"}, kinds=["new"])
    assert _empty(_today(db, bootstrap.user))


def test_window_boundaries_and_maximum(db, bootstrap):
    bundle = _bundle(db, "window")
    _lab_used(db, bootstrap, bundle, used_at=ago(80))
    _snap(db, bundle, ago(30), price=("9.00", "9.00"), kinds=["price"])  # exactly at the 30-day edge

    assert _today(db, bootstrap.user, window_days=30).sections.used_models_changed.total == 1
    just_outside = _today(db, bootstrap.user, now=NOW + timedelta(seconds=1), window_days=30)
    assert just_outside.sections.used_models_changed.total == 0
    assert _today(db, bootstrap.user, now=NOW + timedelta(seconds=1), window_days=90).sections.used_models_changed.total == 1
    clamped = _today(db, bootstrap.user, window_days=10_000)
    assert clamped.window_days == MAX_WINDOW_DAYS == 90
    assert _today(db, bootstrap.user).window_days == 30


def test_incomplete_experiment_runs_are_not_use(db, bootstrap):
    bundle = _bundle(db, "incomplete")
    _lab_used(db, bootstrap, bundle, used_at=ago(20), complete=False)
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])
    assert _empty(_today(db, bootstrap.user))


def test_partially_complete_experiment_counts_as_use_but_is_not_flagged_stale(db, bootstrap):
    bundle = _bundle(db, "partial")
    experiment, _ = _lab_used(db, bootstrap, bundle, used_at=ago(20))
    slots = db.execute(select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)).scalars().all()
    db.get(TaskRun, slots[-1].task_run_id).status = TaskRunStatus.RUNNING
    db.commit()
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])
    result = _today(db, bootstrap.user)
    assert result.sections.used_models_changed.total == 1
    assert result.sections.experiments_to_rerun.total == 0


def test_cancelled_experiments_are_ignored(db, bootstrap):
    bundle = _bundle(db, "cancelled")
    experiment, _ = _lab_used(db, bootstrap, bundle, used_at=ago(20))
    experiment.status = ExperimentStatus.CANCELLED
    db.commit()
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])
    assert _empty(_today(db, bootstrap.user))


def test_another_users_experiment_never_leaks(db, bootstrap):
    other, other_project = _second_user(db, bootstrap)
    bundle = _bundle(db, "private")
    _lab_used(db, bootstrap, bundle, used_at=ago(20), user=other, project=other_project)
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])
    assert _empty(_today(db, bootstrap.user))
    assert _today(db, other).sections.used_models_changed.total == 1


def test_opted_in_project_calls_are_shared_evidence_and_only_for_members(db, bootstrap):
    bundle = _bundle(db, "platform")
    frozen = _snap(db, bundle, ago(20), source=SnapshotSource.EXECUTION_FREEZE)
    project = bootstrap.project
    _platform_call(db, project, bootstrap.user, bundle, frozen, at=ago(20))
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])

    # Not opted in: nothing is read.
    assert _empty(_today(db, bootstrap.user))

    project.ail_evidence_opt_in = True
    db.commit()
    (signal,) = _today(db, bootstrap.user).sections.used_models_changed.items
    assert "OPTED_IN_PROJECT_USAGE" in signal.reason_codes
    assert "PERSONAL_USAGE" not in signal.reason_codes

    # Opted in, but the requesting user is not a member: nothing.
    outsider, _ = _second_user(db, bootstrap, email="outsider@example.com")
    assert _empty(_today(db, outsider))


def _member(db, bootstrap, project, email="colleague@example.com"):
    """A second user who is ALSO a member of ``project``."""
    user, _ = _second_user(db, bootstrap, email=email)
    db.add(ProjectMembership(project_id=project.id, user_id=user.id, role=ProjectRole.MEMBER))
    db.commit()
    return user


@pytest.mark.parametrize("creator", ["colleague", "the_user_themself", "nobody"])
def test_shared_project_usage_is_never_presented_as_the_users_personal_usage(db, bootstrap, creator):
    """A, B. Nothing in the repository attributes a ModelCall to a person
    (Task.created_by is the template's author), so project membership never
    becomes personal ownership — even when the Task's author is the user."""
    colleague = _member(db, bootstrap, bootstrap.project)
    bundle = _bundle(db, f"shared-only-{creator}")
    frozen = _snap(db, bundle, ago(20), source=SnapshotSource.EXECUTION_FREEZE)
    bootstrap.project.ail_evidence_opt_in = True
    db.commit()
    created_by = {"colleague": colleague.id, "the_user_themself": bootstrap.user.id, "nobody": None}[creator]
    _platform_call(db, bootstrap.project, colleague, bundle, frozen, at=ago(20), created_by=created_by)
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])

    result = _today(db, bootstrap.user)
    (signal,) = result.sections.used_models_changed.items
    assert signal.subject["usage_scope"] == "shared_project"
    assert "OPTED_IN_PROJECT_USAGE" in signal.reason_codes
    assert "PERSONAL_USAGE" not in signal.reason_codes
    assert "opted-in project you can access" in signal.why
    assert "does not mean you personally used it" in signal.why
    wording = f"{signal.title} {signal.what_changed} {signal.why}".lower()
    assert "you last used" not in wording and "you used this model" not in wording
    assert not any(ref.type == "experiment" for ref in signal.evidence_refs)
    assert [link.kind for link in signal.links] == ["model"]
    # No personal usage means no experiment or concept signal either.
    assert result.sections.experiments_to_rerun.total == 0
    # The colleague sees the same shared evidence, equally not as personal.
    (theirs,) = _today(db, colleague).sections.used_models_changed.items
    assert theirs.subject["usage_scope"] == "shared_project"


def test_personal_and_shared_usage_are_reported_separately_on_one_card(db, bootstrap):
    colleague = _member(db, bootstrap, bootstrap.project)
    bundle = _bundle(db, "both")
    experiment, frozen = _lab_used(db, bootstrap, bundle, used_at=ago(20))
    bootstrap.project.ail_evidence_opt_in = True
    db.commit()
    _platform_call(db, bootstrap.project, colleague, bundle, frozen, at=ago(20), created_by=colleague.id)
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])

    (signal,) = _today(db, bootstrap.user).sections.used_models_changed.items
    assert signal.subject["usage_scope"] == "personal_and_shared"
    assert {"PERSONAL_USAGE", "OPTED_IN_PROJECT_USAGE"} <= set(signal.reason_codes)
    assert "You used this model in 1 Personal Lab experiment" in signal.why
    assert "also used in 1 call in opted-in projects you can access" in signal.why
    assert ("experiment", experiment.id) in {(r.type, r.id) for r in signal.evidence_refs}


def test_another_members_later_project_call_does_not_suppress_the_stale_experiment(db, bootstrap):
    """C. Suppression needs usage attributable to the authenticated user."""
    colleague = _member(db, bootstrap, bootstrap.project)
    bundle = _bundle(db, "no-suppress")
    experiment, _ = _lab_used(db, bootstrap, bundle, used_at=ago(20))
    changed = _snap(db, bundle, ago(10), price=("1.00", "5.00"), kinds=["price"])
    bootstrap.project.ail_evidence_opt_in = True
    db.commit()
    _platform_call(db, bootstrap.project, colleague, bundle, changed, at=ago(3), created_by=colleague.id)

    result = _today(db, bootstrap.user)
    assert _ids(result.sections.experiments_to_rerun) == [f"EXPERIMENT_MAY_BE_STALE:{experiment.id}"]
    (model_signal,) = result.sections.used_models_changed.items
    assert model_signal.subject["usage_scope"] == "personal_and_shared"
    assert "PERSONAL_USAGE" in model_signal.reason_codes
    # ...and the colleague, who never ran the experiment, gets no experiment signal.
    assert _today(db, colleague).sections.experiments_to_rerun.total == 0


def test_the_users_own_reuse_suppresses_even_when_a_colleague_also_called_the_model(db, bootstrap):
    """D. Personal reuse after the change still suppresses, as approved."""
    colleague = _member(db, bootstrap, bootstrap.project)
    bundle = _bundle(db, "own-reuse")
    _lab_used(db, bootstrap, bundle, used_at=ago(20))
    changed = _snap(db, bundle, ago(10), price=("1.00", "5.00"), kinds=["price"])
    bootstrap.project.ail_evidence_opt_in = True
    db.commit()
    _platform_call(db, bootstrap.project, colleague, bundle, changed, at=ago(6), created_by=colleague.id)
    _experiment(db, bootstrap, [changed, _control(db, ago(3))], started=ago(3))  # the user's own reuse
    assert _empty(_today(db, bootstrap.user))


@pytest.mark.parametrize(
    "agent_status", [AgentRunStatus.FAILED, AgentRunStatus.RUNNING, AgentRunStatus.CREATED, AgentRunStatus.STOPPED]
)
def test_stale_qualification_requires_agent_run_terminal_success(db, bootstrap, agent_status):
    """E. Task Runs COMPLETED is not enough: the Agent Runs must have COMPLETED."""
    bundle = _bundle(db, f"agent-{agent_status.value}")
    _lab_used(db, bootstrap, bundle, used_at=ago(20), agent_status=agent_status)
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])
    assert _empty(_today(db, bootstrap.user))


def test_one_unsuccessful_agent_run_disqualifies_the_experiment_but_completed_runs_still_count_as_use(db, bootstrap):
    bundle = _bundle(db, "mixed-agents")
    frozen = _snap(db, bundle, ago(20), source=SnapshotSource.EXECUTION_FREEZE)
    _experiment(
        db,
        bootstrap,
        [frozen, _control(db, ago(20))],
        started=ago(20),
        agent_status_by_label={"model-2": AgentRunStatus.FAILED},
    )
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])
    result = _today(db, bootstrap.user)
    assert result.sections.experiments_to_rerun.total == 0  # not fully, successfully executed
    assert result.sections.used_models_changed.total == 1  # model-1's completed runs are real use


def test_a_failed_task_run_makes_the_experiment_partial_and_not_stale_eligible(db, bootstrap):
    bundle = _bundle(db, "partial-failed")
    experiment, _ = _lab_used(db, bootstrap, bundle, used_at=ago(20))
    slots = db.execute(select(ExperimentTaskRun).where(ExperimentTaskRun.experiment_id == experiment.id)).scalars().all()
    db.get(TaskRun, slots[-1].task_run_id).status = TaskRunStatus.FAILED
    db.commit()
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])
    result = _today(db, bootstrap.user)
    assert result.sections.experiments_to_rerun.total == 0
    assert result.sections.used_models_changed.total == 1


def test_failed_platform_calls_are_not_use(db, bootstrap):
    bundle = _bundle(db, "platform-failed")
    frozen = _snap(db, bundle, ago(20), source=SnapshotSource.EXECUTION_FREEZE)
    bootstrap.project.ail_evidence_opt_in = True
    db.commit()
    _platform_call(db, bootstrap.project, bootstrap.user, bundle, frozen, at=ago(20), status=ModelCallStatus.ERROR)
    _snap(db, bundle, ago(5), price=("1.00", "8.00"), kinds=["price"])
    assert _empty(_today(db, bootstrap.user))


# =============================================================================
# WATCHED_DEVELOPMENT_CHANGED
# =============================================================================


def test_new_qualifying_claim_after_watch_fires_with_provenance(db, bootstrap):
    dev = _development(db, title="Big release")
    _watch(db, bootstrap.user, dev, at=ago(10), revisit_at=ago(-30))
    claim = _claim(db, dev, ClaimType.BENCHMARK_RESULT, ago(3))
    _claim(db, dev, ClaimType.FACT, ago(2))

    (signal,) = _today(db, bootstrap.user).sections.watched_developments.items
    assert signal.family.value == "WATCHED_DEVELOPMENT_CHANGED"
    assert "WATCH_NEW_EVIDENCE" in signal.reason_codes
    assert "1 benchmark result" in signal.what_changed and "1 fact" in signal.what_changed
    assert ("claim", claim.id) in {(r.type, r.id) for r in signal.evidence_refs}
    assert signal.subject["verification_level"] == "Independently Measured"
    assert signal.links[0].kind == "development"
    assert signal.since == ago(10)


def test_community_signal_and_ai_explanation_alone_never_trigger(db, bootstrap):
    dev = _development(db)
    _watch(db, bootstrap.user, dev, at=ago(10), revisit_at=ago(-30))
    _claim(db, dev, ClaimType.COMMUNITY_SIGNAL, ago(2))
    _claim(db, dev, ClaimType.AI_EXPLANATION, ago(2))
    assert _empty(_today(db, bootstrap.user))


def test_claims_before_the_watch_after_the_window_or_inactive_do_not_trigger(db, bootstrap):
    dev = _development(db)
    _watch(db, bootstrap.user, dev, at=ago(40), revisit_at=ago(-30))
    _claim(db, dev, ClaimType.FACT, ago(45))  # before the watch
    _claim(db, dev, ClaimType.FACT, ago(35))  # after the watch but outside the 30-day window
    _claim(db, dev, ClaimType.FACT, ago(2), status=ClaimStatus.SUPERSEDED)
    result = _today(db, bootstrap.user)
    assert _empty(result)
    assert _today(db, bootstrap.user, window_days=90).sections.watched_developments.total == 1


def test_only_an_active_watch_counts(db, bootstrap):
    learn = _development(db)
    _watch(db, bootstrap.user, learn, at=ago(10), decision=TriageDecisionKind.LEARN)
    _claim(db, learn, ClaimType.FACT, ago(2))
    superseded = _development(db)
    _watch(db, bootstrap.user, superseded, at=ago(10), revisit_at=ago(-30), superseded=True)
    _claim(db, superseded, ClaimType.FACT, ago(2))
    merged = _development(db, status=DevelopmentStatus.MERGED)
    _watch(db, bootstrap.user, merged, at=ago(10), revisit_at=ago(-30))
    _claim(db, merged, ClaimType.FACT, ago(2))
    assert _empty(_today(db, bootstrap.user))


def test_revisit_date_reached_triggers_and_a_future_date_does_not(db, bootstrap):
    reached = _development(db, title="Reached")
    _watch(db, bootstrap.user, reached, at=ago(20), revisit_at=ago(2))
    future = _development(db, title="Future")
    _watch(db, bootstrap.user, future, at=ago(20), revisit_at=ago(-5))
    (signal,) = _today(db, bootstrap.user).sections.watched_developments.items
    assert signal.title == "Reached"
    assert "WATCH_REVISIT_DATE_REACHED" in signal.reason_codes
    assert signal.evidence_refs[0].type == "triage_decision"


def test_revisit_at_is_evaluated_deterministically_at_its_boundaries(db, bootstrap):
    """F. ``revisit_at`` is the one supported trigger: reached means
    window_start <= revisit_at <= now."""
    cases = {
        "exactly now": NOW,
        "one second ahead": NOW + timedelta(seconds=1),
        "window edge": ago(30),
        "just before the window": ago(30) - timedelta(seconds=1),
    }
    for title, revisit_at in cases.items():
        dev = _development(db, title=title)
        _watch(db, bootstrap.user, dev, at=ago(60), revisit_at=revisit_at)
    titles = {s.title for s in _today(db, bootstrap.user).sections.watched_developments.items}
    assert titles == {"exactly now", "window edge"}
    assert _today(db, bootstrap.user, window_days=90).sections.watched_developments.total == 3


@pytest.mark.parametrize(
    "condition",
    [
        {"kind": "verification_at_least", "level": "Documented"},
        {"kind": "date", "at": "2026-09-01T00:00:00+00:00"},
        {"kind": "attention_state", "state": "HIGH"},
    ],
)
def test_unsupported_revisit_condition_is_never_evaluated(db, bootstrap, condition):
    """G. Deferred limitation: ``revisit_condition`` has no defined shape in the
    repository beyond ``kind``, so AIL.4A does not evaluate it — even when the
    condition, read naively, would already be satisfied."""
    dev = _development(db, title="Conditioned")
    _claim(db, dev, ClaimType.FACT, ago(50))  # the development IS Documented now, before the watch began
    _watch(db, bootstrap.user, dev, at=ago(20), revisit_at=None, revisit_condition=condition)
    assert _empty(_today(db, bootstrap.user))

    # A future revisit_at plus a naively satisfied condition still does not fire.
    later = _development(db, title="Conditioned with a future date")
    _claim(db, later, ClaimType.FACT, ago(50))
    _watch(db, bootstrap.user, later, at=ago(20), revisit_at=ago(-5), revisit_condition=condition)
    assert _empty(_today(db, bootstrap.user))

    # The supported trigger (revisit_at) still works alongside such a condition.
    reached = _development(db, title="Conditioned but date reached")
    _watch(db, bootstrap.user, reached, at=ago(20), revisit_at=ago(2), revisit_condition=condition)
    (signal,) = _today(db, bootstrap.user).sections.watched_developments.items
    assert signal.title == "Conditioned but date reached"
    assert "WATCH_REVISIT_DATE_REACHED" in signal.reason_codes


def test_confirmed_concept_link_after_watch_triggers_but_a_proposed_one_does_not(db, bootstrap):
    concept, _ = _concept(db)
    confirmed = _development(db, title="Confirmed link")
    _watch(db, bootstrap.user, confirmed, at=ago(10), revisit_at=ago(-30))
    _link(db, confirmed, concept, reviewed_at=ago(3))
    proposed = _development(db, title="Proposed link")
    _watch(db, bootstrap.user, proposed, at=ago(10), revisit_at=ago(-30))
    _link(db, proposed, concept, state=DevelopmentConceptState.PROPOSED)
    (signal,) = _today(db, bootstrap.user).sections.watched_developments.items
    assert signal.title == "Confirmed link"
    assert "WATCH_CONCEPT_LINK_CONFIRMED" in signal.reason_codes


def test_another_users_watch_never_leaks(db, bootstrap):
    other, _ = _second_user(db, bootstrap)
    dev = _development(db)
    _watch(db, other, dev, at=ago(10), revisit_at=ago(-30))
    _claim(db, dev, ClaimType.FACT, ago(2))
    assert _empty(_today(db, bootstrap.user))
    assert _today(db, other).sections.watched_developments.total == 1


# =============================================================================
# CONCEPT_CHANGED
# =============================================================================


def test_material_version_for_a_watched_concept_without_evidence_is_a_standalone_card(db, bootstrap):
    concept, _ = _concept(db, "Tool Calling")
    db.add(LearnerInterest(user_id=bootstrap.user.id, concept_id=concept.id, watch=True, created_at=ago(20)))
    db.commit()
    _publish(db, concept, at=ago(4), severity=ChangeSeverity.MATERIAL, note="Definition tightened.")
    (signal,) = _today(db, bootstrap.user).sections.concepts_changed.items
    assert signal.family.value == "CONCEPT_CHANGED"
    assert "WATCHING_CONCEPT" in signal.reason_codes and "CONCEPT_NEW_MATERIAL_VERSION" in signal.reason_codes
    assert "Definition tightened." in signal.what_changed
    assert signal.learner_state is None


def test_watching_is_opt_in_and_a_minor_version_is_not_a_change(db, bootstrap):
    concept, _ = _concept(db)
    _publish(db, concept, at=ago(4), severity=ChangeSeverity.MATERIAL)
    assert _empty(_today(db, bootstrap.user))  # not watched, no evidence
    db.add(LearnerInterest(user_id=bootstrap.user.id, concept_id=concept.id, watch=False, created_at=ago(20)))
    db.commit()
    assert _empty(_today(db, bootstrap.user))  # interest without watch
    minor, _ = _concept(db)
    db.add(LearnerInterest(user_id=bootstrap.user.id, concept_id=minor.id, watch=True, created_at=ago(20)))
    db.commit()
    _publish(db, minor, at=ago(4), severity=ChangeSeverity.MINOR)
    assert _empty(_today(db, bootstrap.user))


def test_multi_hop_material_change_is_found_even_when_the_latest_hop_is_minor(db, bootstrap):
    concept, v1 = _concept(db, "Multi hop")
    _evidence(db, bootstrap.user, concept, v1, at=ago(60))
    v2 = _publish(db, concept, at=ago(10), severity=ChangeSeverity.MATERIAL, note="Big shift.")
    _publish(db, concept, at=ago(3), severity=ChangeSeverity.MINOR, note="Typo.")
    # Learner state only inspects the single latest hop; the rollup card must not.
    (card,) = _today(db, bootstrap.user).sections.worth_revisiting.items
    (reason,) = card.reasons
    assert [c["version"] for c in reason.changes] == [2]
    assert ("concept_version", v2.id) in {(r.type, r.id) for r in reason.evidence_refs}


def test_versions_at_or_before_the_evidence_and_drafts_are_not_changes(db, bootstrap):
    concept, _ = _concept(db)
    v2 = _publish(db, concept, at=ago(50), severity=ChangeSeverity.MATERIAL)
    _evidence(db, bootstrap.user, concept, v2, at=ago(40))  # learned against v2
    _publish(db, concept, at=ago(3), severity=ChangeSeverity.MATERIAL, draft_only=True)  # never published
    assert _empty(_today(db, bootstrap.user))


def test_failed_or_self_reported_evidence_is_not_learning(db, bootstrap):
    concept, v1 = _concept(db)
    _evidence(db, bootstrap.user, concept, v1, at=ago(40), passed=False)
    _evidence(db, bootstrap.user, concept, v1, at=ago(40), kind=EvidenceType.SELF_REPORT)
    _publish(db, concept, at=ago(3), severity=ChangeSeverity.MATERIAL)
    assert _empty(_today(db, bootstrap.user))


def test_linked_development_must_be_confirmed_documented_and_newer_than_the_evidence(db, bootstrap):
    concept, _ = _concept(db, "Linked")
    db.add(LearnerInterest(user_id=bootstrap.user.id, concept_id=concept.id, watch=True, created_at=ago(40)))
    db.commit()
    good = _development(db, announced=ago(10), title="Documented and new")
    _claim(db, good, ClaimType.FACT, ago(9))
    _link(db, good, concept, reviewed_at=ago(8))
    provider_only = _development(db, announced=ago(10), title="Provider claim only")
    _claim(db, provider_only, ClaimType.PROVIDER_CLAIM, ago(9))
    _link(db, provider_only, concept, reviewed_at=ago(8))
    community_only = _development(db, announced=ago(10), title="Community only")
    _claim(db, community_only, ClaimType.COMMUNITY_SIGNAL, ago(9))
    _link(db, community_only, concept, reviewed_at=ago(8))
    proposed = _development(db, announced=ago(10), title="Not confirmed")
    _claim(db, proposed, ClaimType.FACT, ago(9))
    _link(db, proposed, concept, state=DevelopmentConceptState.PROPOSED)
    merged = _development(db, announced=ago(10), title="Merged", status=DevelopmentStatus.MERGED)
    _claim(db, merged, ClaimType.FACT, ago(9))
    _link(db, merged, concept, reviewed_at=ago(8))
    older = _development(db, announced=ago(60), title="Announced before the concept was watched")
    _claim(db, older, ClaimType.FACT, ago(59))
    _link(db, older, concept, reviewed_at=ago(8))

    (signal,) = _today(db, bootstrap.user).sections.concepts_changed.items
    assert [c["title"] for c in signal.changes] == ["Documented and new"]
    assert signal.changes[0]["verification_level"] == "Documented"
    assert "CONCEPT_NEW_LINKED_DEVELOPMENT" in signal.reason_codes


# =============================================================================
# WORTH_REVISITING rollup
# =============================================================================


def _learned(db, bootstrap, name, *, at, passes=1):
    concept, v1 = _concept(db, name)
    _evidence(db, bootstrap.user, concept, v1, at=at, n=passes)
    return concept, v1


def test_rollup_absorbs_concept_linked_signals_and_never_duplicates_them(db, bootstrap):
    bundle = _bundle(db, "rollup")
    concept, v1 = _concept(db, "Model Routing")
    _lab_used(db, bootstrap, bundle, used_at=ago(30), concept_id=concept.id)
    _evidence(db, bootstrap.user, concept, v1, at=ago(29), n=2)
    _snap(db, bundle, ago(5), price=("1.00", "7.00"), kinds=["price"])
    _publish(db, concept, at=ago(4), severity=ChangeSeverity.MATERIAL, note="Routing rewritten.")
    linked = _development(db, announced=ago(3), title="Router paper")
    _claim(db, linked, ClaimType.FACT, ago(3))
    _link(db, linked, concept, reviewed_at=ago(2))
    watched = _development(db, title="Watched router")
    _watch(db, bootstrap.user, watched, at=ago(10), revisit_at=ago(-30))
    _claim(db, watched, ClaimType.BENCHMARK_RESULT, ago(2))
    _link(db, watched, concept, reviewed_at=ago(20))
    unrelated = _development(db, title="Unrelated watched")
    _watch(db, bootstrap.user, unrelated, at=ago(10), revisit_at=ago(-30))
    _claim(db, unrelated, ClaimType.FACT, ago(2))

    result = _today(db, bootstrap.user)
    (card,) = result.sections.worth_revisiting.items
    assert card.family.value == "WORTH_REVISITING"
    assert card.learner_state["ladder"] == "demonstrated"
    assert {r.family.value for r in card.reasons} == {
        "CONCEPT_CHANGED",
        "EXPERIMENT_MAY_BE_STALE",
        "WATCHED_DEVELOPMENT_CHANGED",
    }
    assert card.distinct_reason_count == len({c for r in card.reasons for c in r.reason_codes} - {"HAS_LEARNING_EVIDENCE"})
    assert "HAS_LEARNING_EVIDENCE" in card.reason_codes
    assert [r.type for r in card.evidence_refs] == ["learning_evidence"] * 2
    for reason in card.reasons:  # each absorbed signal keeps its own evidence
        assert reason.reason_codes and reason.evidence_refs

    # Absorbed signals are not shown a second time; unrelated ones remain.
    assert result.sections.experiments_to_rerun.total == 0
    assert result.sections.concepts_changed.total == 0
    assert _ids(result.sections.watched_developments) == [f"WATCHED_DEVELOPMENT_CHANGED:{unrelated.id}"]
    # Model-level facts are platform facts, not concept facts.
    assert result.sections.used_models_changed.total == 1
    ids = _all_ids(result)
    assert len(ids) == len(set(ids))


def test_experiment_with_an_unlearned_concept_stays_in_its_own_section(db, bootstrap):
    bundle = _bundle(db, "unlearned")
    concept, _ = _concept(db)
    _lab_used(db, bootstrap, bundle, used_at=ago(20), concept_id=concept.id)
    _snap(db, bundle, ago(5), price=("1.00", "7.00"), kinds=["price"])
    result = _today(db, bootstrap.user)
    assert result.sections.worth_revisiting.total == 0
    assert result.sections.experiments_to_rerun.total == 1


def test_rollup_order_uses_visible_properties_only(db, bootstrap):
    demonstrated, _ = _learned(db, bootstrap, "Zeta demonstrated", at=ago(40), passes=2)
    understood_many, _ = _learned(db, bootstrap, "Alpha many reasons", at=ago(40))
    understood_recent, _ = _learned(db, bootstrap, "Beta recent", at=ago(40))
    understood_old, _ = _learned(db, bootstrap, "Gamma old", at=ago(40))
    _publish(db, demonstrated, at=ago(20), severity=ChangeSeverity.MATERIAL)
    _publish(db, understood_many, at=ago(15), severity=ChangeSeverity.MATERIAL)
    extra = _development(db, announced=ago(14))
    _claim(db, extra, ClaimType.FACT, ago(14))
    _link(db, extra, understood_many, reviewed_at=ago(13))
    _publish(db, understood_recent, at=ago(2), severity=ChangeSeverity.MATERIAL)
    _publish(db, understood_old, at=ago(9), severity=ChangeSeverity.MATERIAL)
    result = _today(db, bootstrap.user)
    assert _ids(result.sections.worth_revisiting) == [
        f"WORTH_REVISITING:{demonstrated.id}",  # highest learner-state rung first
        f"WORTH_REVISITING:{understood_many.id}",  # then more distinct reasons
        f"WORTH_REVISITING:{understood_recent.id}",  # then most recent change
    ]
    assert result.sections.worth_revisiting.total == 4 and result.sections.worth_revisiting.shown == 3


def test_repeated_reads_are_identical(db, bootstrap):
    bundle = _bundle(db, "stable")
    _lab_used(db, bootstrap, bundle, used_at=ago(20))
    _snap(db, bundle, ago(5), price=("1.00", "7.00"), kinds=["price"])
    dev = _development(db)
    _watch(db, bootstrap.user, dev, at=ago(10), revisit_at=ago(-30))
    _claim(db, dev, ClaimType.FACT, ago(2))
    first = _today(db, bootstrap.user).model_dump_json()
    second = _today(db, bootstrap.user).model_dump_json()
    assert first == second


def test_no_opaque_score_or_ranking_fields_anywhere(db, bootstrap):
    concept, _ = _learned(db, bootstrap, "Scoreless", at=ago(40))
    _publish(db, concept, at=ago(2), severity=ChangeSeverity.MATERIAL)
    payload = _today(db, bootstrap.user).model_dump(mode="json")

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    forbidden = {"score", "priority", "rank", "ranking", "importance", "relevance_score", "winner", "best"}
    assert not (set(keys(payload)) & forbidden)
    text_blob = json.dumps(payload).lower()
    for phrase in ("priority score", "importance score", "#1", "most important", "best model"):
        assert phrase not in text_blob


# =============================================================================
# Read-only, authorization, contract
# =============================================================================


def _world(db, bootstrap, base):
    """A populated scenario relative to ``base`` (real now for API tests)."""
    bundle = _bundle(db, f"world{next(_counter)}")
    concept, v1 = _concept(db, "World concept")
    experiment, _ = _lab_used(db, bootstrap, bundle, used_at=ago(20, base), concept_id=concept.id,
                              hypothesis="SECRET-HYPOTHESIS-TEXT")
    _evidence(db, bootstrap.user, concept, v1, at=ago(19, base), n=2)
    _snap(db, bundle, ago(5, base), price=("1.00", "4.00"), kinds=["price"])
    _publish(db, concept, at=ago(4, base), severity=ChangeSeverity.MATERIAL, note="Changed.")
    dev = _development(db, title="World watched")
    _watch(db, bootstrap.user, dev, at=ago(10, base), revisit_at=ago(-30, base))
    _claim(db, dev, ClaimType.FACT, ago(2, base))
    return experiment


def _write_capture(engine):
    statements = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    return statements, lambda: event.remove(engine, "before_cursor_execute", _before)


def _table_dump(engine):
    with engine.connect() as conn:
        return {
            table.name: conn.execute(text(f'SELECT * FROM "{table.name}" ORDER BY 1')).fetchall()
            for table in Base.metadata.sorted_tables
        }


def test_service_issues_no_writes_and_leaves_every_table_untouched(db, bootstrap, engine):
    _world(db, bootstrap, NOW)
    before = _table_dump(engine)
    statements, stop = _write_capture(engine)
    try:
        result = _today(db, bootstrap.user)
    finally:
        stop()
    assert result.sections.worth_revisiting.total == 1  # the world really produced signals
    assert statements == []
    assert not db.new and not db.dirty and not db.deleted
    assert _table_dump(engine) == before


@pytest.fixture()
def as_user():
    def _override(user):
        fastapi_app.dependency_overrides[get_current_user] = lambda: user

    yield _override
    fastapi_app.dependency_overrides.pop(get_current_user, None)


def test_api_requires_authentication(client, bootstrap):
    assert client.get("/stay-ahead/today").status_code == 401


def test_api_get_is_read_only_and_leaks_no_private_content(client, auth_headers, db, bootstrap, engine):
    base = datetime.now(timezone.utc)
    experiment = _world(db, bootstrap, base)
    before = _table_dump(engine)
    statements, stop = _write_capture(engine)
    try:
        response = client.get("/stay-ahead/today", headers=auth_headers)
    finally:
        stop()
    assert response.status_code == 200, response.text
    assert statements == []
    assert _table_dump(engine) == before
    body = response.json()
    assert body["window_days"] == 30
    assert body["sections"]["worth_revisiting"]["total"] == 1
    assert body["sections"]["used_models_changed"]["total"] == 1
    assert body["sections"]["watched_developments"]["total"] == 1
    # The owner's private text and task content never appear.
    assert "SECRET-HYPOTHESIS-TEXT" not in response.text
    assert "Do the thing" not in response.text
    # Experiment status is not settled by reading Today.
    assert db.get(Experiment, experiment.id).status == experiment.status


def test_api_rejects_a_client_supplied_user_and_bad_windows(client, auth_headers, bootstrap):
    assert client.get("/stay-ahead/today?user_id=someone-else", headers=auth_headers).status_code == 422
    assert client.get("/stay-ahead/today?window_days=0", headers=auth_headers).status_code == 422
    assert client.get("/stay-ahead/today?window_days=91", headers=auth_headers).status_code == 422
    assert client.get("/stay-ahead/today?window_days=90", headers=auth_headers).status_code == 200


def test_api_cross_user_isolation(client, db, bootstrap, as_user):
    base = datetime.now(timezone.utc)
    _world(db, bootstrap, base)
    other, _ = _second_user(db, bootstrap)
    as_user(bootstrap.user)
    owner_view = client.get("/stay-ahead/today").json()
    as_user(other)
    other_view = client.get("/stay-ahead/today").json()
    assert owner_view["sections"]["used_models_changed"]["total"] == 1
    assert all(section["total"] == 0 for section in other_view["sections"].values())


def test_no_schema_change_and_no_later_slice_tables():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert heads == ["ail3c_experiment_conclusion"]
    tables = set(Base.metadata.tables)
    assert not tables & {"review_attempts", "interest_rules", "opportunities", "stay_ahead_signals", "signal_dismissals"}
