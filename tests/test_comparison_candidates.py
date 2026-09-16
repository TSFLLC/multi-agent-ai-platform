"""Normalized comparison candidates — Section 24.4 #7 (no ID arrays)."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import ComparisonRunStatus
from app.models.artifacts_eval import ComparisonCandidate, ComparisonRun
from tests.conftest import make_agent_run, make_task_run


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
        ComparisonCandidate(
            comparison_run_id=comparison.id,
            agent_run_id=make_agent_run(db, task_run=task_run).id,
            label=label,
        )
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

    db.add(ComparisonCandidate(comparison_run_id=comparison.id, agent_run_id=run.id, label="A"))
    db.commit()

    db.add(ComparisonCandidate(comparison_run_id=comparison.id, agent_run_id=run.id, label="A duplicate"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_duplicate_label_in_same_comparison_rejected(db):
    task_run = make_task_run(db)
    comparison = ComparisonRun(task_run_id=task_run.id, status=ComparisonRunStatus.RUNNING)
    db.add(comparison)
    db.commit()

    db.add(
        ComparisonCandidate(
            comparison_run_id=comparison.id, agent_run_id=make_agent_run(db, task_run=task_run).id, label="A"
        )
    )
    db.commit()

    db.add(
        ComparisonCandidate(
            comparison_run_id=comparison.id, agent_run_id=make_agent_run(db, task_run=task_run).id, label="A"
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_winner_flag_and_denormalized_fk_can_be_kept_in_sync(db):
    task_run = make_task_run(db)
    comparison = ComparisonRun(task_run_id=task_run.id, status=ComparisonRunStatus.RUNNING)
    db.add(comparison)
    db.commit()
    run = make_agent_run(db, task_run=task_run)
    winner = ComparisonCandidate(
        comparison_run_id=comparison.id, agent_run_id=run.id, label="A", is_winner=True
    )
    db.add(winner)
    comparison.winner_agent_run_id = run.id
    comparison.status = ComparisonRunStatus.COMPLETED
    db.commit()

    db.refresh(comparison)
    assert comparison.winner_agent_run_id == winner.agent_run_id
