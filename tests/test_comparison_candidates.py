"""Normalized comparison candidates — Section 24.4 #7 (no ID arrays).

MA5 note: ComparisonRun.task_run_id now means "this comparison's own
bookkeeping Task Run" (event/budget anchor only, never executed against
directly) — see app.models.artifacts_eval.ComparisonRun's docstring.
Each real candidate gets its *own* separate Task Run instead.
ComparisonCandidate.agent_version_id is required even when constructing a
row that already has a launched agent_run_id, matching how
ComparisonService actually builds these rows.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import ComparisonRunStatus
from app.models.artifacts_eval import ComparisonCandidate, ComparisonRun
from tests.conftest import make_agent_run, make_task_run


def _candidate(comparison_id: str, agent_run, label: str, **kwargs) -> ComparisonCandidate:
    return ComparisonCandidate(
        comparison_run_id=comparison_id,
        agent_version_id=agent_run.agent_version_id,
        agent_run_id=agent_run.id,
        task_run_id=agent_run.task_run_id,
        label=label,
        **kwargs,
    )


def test_candidates_are_rows_not_an_array_column(engine):
    from sqlalchemy import inspect

    columns = {c["name"] for c in inspect(engine).get_columns("comparison_runs")}
    assert "candidate_agent_run_ids" not in columns
    assert "comparison_candidates" in inspect(engine).get_table_names()


def test_three_way_comparison_normalized(db):
    task_run = make_task_run(db)
    comparison = ComparisonRun(task_run_id=task_run.id, status=ComparisonRunStatus.RUNNING)
    db.add(comparison)
    db.commit()

    candidates = [
        _candidate(comparison.id, make_agent_run(db, task_run=task_run), label)
        for label in ("Candidate A", "Candidate B", "Candidate C")
    ]
    db.add_all(candidates)
    db.commit()

    rows = db.query(ComparisonCandidate).filter_by(comparison_run_id=comparison.id).all()
    assert len(rows) == 3


def test_duplicate_agent_run_in_same_comparison_rejected(db):
    task_run = make_task_run(db)
    comparison = ComparisonRun(task_run_id=task_run.id, status=ComparisonRunStatus.RUNNING)
    db.add(comparison)
    db.commit()
    run = make_agent_run(db, task_run=task_run)

    db.add(_candidate(comparison.id, run, "A"))
    db.commit()

    db.add(_candidate(comparison.id, run, "A duplicate"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_duplicate_label_in_same_comparison_rejected(db):
    task_run = make_task_run(db)
    comparison = ComparisonRun(task_run_id=task_run.id, status=ComparisonRunStatus.RUNNING)
    db.add(comparison)
    db.commit()

    db.add(_candidate(comparison.id, make_agent_run(db, task_run=task_run), "A"))
    db.commit()

    db.add(_candidate(comparison.id, make_agent_run(db, task_run=task_run), "A"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_winner_flag_and_denormalized_fk_can_be_kept_in_sync(db):
    task_run = make_task_run(db)
    comparison = ComparisonRun(task_run_id=task_run.id, status=ComparisonRunStatus.RUNNING)
    db.add(comparison)
    db.commit()
    run = make_agent_run(db, task_run=task_run)
    winner = _candidate(comparison.id, run, "A", is_winner=True)
    db.add(winner)
    comparison.winner_agent_run_id = run.id
    comparison.status = ComparisonRunStatus.COMPLETED
    db.commit()

    db.refresh(comparison)
    assert comparison.winner_agent_run_id == winner.agent_run_id


def test_candidate_can_be_configured_before_agent_run_exists(db):
    """MA5's real flow: a candidate is created at comparison-creation
    time, before it has ever run — agent_run_id/task_run_id start NULL."""
    task_run = make_task_run(db)
    comparison = ComparisonRun(task_run_id=task_run.id, status=ComparisonRunStatus.PENDING)
    db.add(comparison)
    db.commit()

    from tests.conftest import make_agent_version

    agent_version = make_agent_version(db)
    db.commit()

    candidate = ComparisonCandidate(
        comparison_run_id=comparison.id,
        agent_version_id=agent_version.id,
        label="Candidate A",
        model_policy_override_json={"mode": "manual", "manual_provider_model_id": "pm-1"},
    )
    db.add(candidate)
    db.commit()

    db.refresh(candidate)
    assert candidate.agent_run_id is None
    assert candidate.task_run_id is None
    assert candidate.model_policy_override_json == {"mode": "manual", "manual_provider_model_id": "pm-1"}
