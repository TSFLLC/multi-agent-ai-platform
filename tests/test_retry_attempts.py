"""Retry attempt preservation — Section 24.4 #10.

A retried Agent Run gets a new agent_run_attempts row, never a mutation of
the prior attempt — this is what keeps cost/evaluation accounting for
"failed then succeeded on attempt 2" unambiguous.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import AgentRunAttemptStatus
from app.models.tasks import AgentRunAttempt
from tests.conftest import make_agent_run


def test_multiple_attempts_preserved_as_distinct_rows(db):
    run = make_agent_run(db)
    a1 = AgentRunAttempt(agent_run_id=run.id, attempt_number=1, status=AgentRunAttemptStatus.FAILED)
    a2 = AgentRunAttempt(agent_run_id=run.id, attempt_number=2, status=AgentRunAttemptStatus.COMPLETED)
    db.add_all([a1, a2])
    db.commit()

    attempts = (
        db.query(AgentRunAttempt)
        .filter_by(agent_run_id=run.id)
        .order_by(AgentRunAttempt.attempt_number)
        .all()
    )
    assert [a.status for a in attempts] == [AgentRunAttemptStatus.FAILED, AgentRunAttemptStatus.COMPLETED]
    assert a1.id != a2.id


def test_duplicate_attempt_number_rejected(db):
    run = make_agent_run(db)
    db.add(AgentRunAttempt(agent_run_id=run.id, attempt_number=1, status=AgentRunAttemptStatus.FAILED))
    db.commit()

    db.add(AgentRunAttempt(agent_run_id=run.id, attempt_number=1, status=AgentRunAttemptStatus.RUNNING))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_failed_then_succeeded_run_is_never_collapsed(db):
    """The prior-attempt row's FAILED status/error must remain readable
    after a later attempt succeeds — this is the whole point of a
    per-attempt table instead of mutating agent_runs.status in place."""
    run = make_agent_run(db)
    failed = AgentRunAttempt(
        agent_run_id=run.id,
        attempt_number=1,
        status=AgentRunAttemptStatus.FAILED,
        error={"message": "timeout"},
    )
    db.add(failed)
    db.commit()

    succeeded = AgentRunAttempt(agent_run_id=run.id, attempt_number=2, status=AgentRunAttemptStatus.COMPLETED)
    db.add(succeeded)
    db.commit()

    db.refresh(failed)
    assert failed.status == AgentRunAttemptStatus.FAILED
    assert failed.error == {"message": "timeout"}
