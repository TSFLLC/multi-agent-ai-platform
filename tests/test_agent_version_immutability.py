"""Agent Version immutability — Section 12.3 reproducibility invariant.

"Editing" a published Agent Version must always insert a new row with
version+1, never mutate the published row's content columns in place.
MA0 does not yet have an AgentRegistryService to enforce this behaviorally
(that lands in MA2), so these tests pin the two structural guarantees the
schema itself provides: (version) uniqueness per agent, and that changing
"what version 1 means" requires a second, distinguishable row rather than
an UPDATE.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import VersionStatus
from tests.conftest import make_agent, make_agent_version


def test_duplicate_version_number_rejected(db):
    agent = make_agent(db)
    make_agent_version(db, agent=agent, version=1, status=VersionStatus.ACTIVE)

    from app.models.agents import AgentVersion

    dup = AgentVersion(agent_id=agent.id, version=1, name="different", role="x", status=VersionStatus.DRAFT)
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_editing_a_published_version_creates_a_new_row(db):
    agent = make_agent(db)
    v1 = make_agent_version(db, agent=agent, version=1, status=VersionStatus.ACTIVE)
    original_name = v1.name

    v2 = make_agent_version(db, agent=agent, version=2, status=VersionStatus.DRAFT)

    # v1 is untouched — this is what "immutable once published" means at
    # the row level.
    db.refresh(v1)
    assert v1.name == original_name
    assert v1.status == VersionStatus.ACTIVE
    assert v2.id != v1.id
    assert v2.version == v1.version + 1


def test_historical_agent_run_keeps_referencing_its_original_version(db):
    """An Agent Run created against v1 must keep resolving to v1's content
    even after v2 is published — the reproducibility guarantee (Section
    32 Acceptance Criterion 1)."""
    from tests.conftest import make_agent_run

    agent = make_agent(db)
    v1 = make_agent_version(db, agent=agent, version=1, status=VersionStatus.ACTIVE)
    run = make_agent_run(db, agent_version=v1)

    make_agent_version(db, agent=agent, version=2, status=VersionStatus.ACTIVE)

    db.refresh(run)
    assert run.agent_version_id == v1.id
