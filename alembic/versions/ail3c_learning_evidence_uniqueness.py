"""ail3c_learning_evidence_uniqueness

Revision ID: ail3c_learning_evidence_uniqueness
Revises: ail3c_experiment_conclusion
Create Date: 2026-09-23 16:30:00

AIL.3C: Add database-level uniqueness protection for experiment-backed evidence.

Prevents concurrent duplicate LearningEvidence rows for the same
(user, experiment) pair. This is critical for idempotent count-toward-learning
behavior in concurrent request scenarios.

Uses filtered unique index (SQLite compatible) to enforce uniqueness only
for ref_type='experiment', allowing other ref_types to remain flexible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ail3c_learning_evidence_uniqueness'
down_revision: Union[str, None] = 'ail3c_experiment_conclusion'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create unique index for experiment-backed evidence
    # Ensures exactly one evidence row per (user_id, ref_id) where ref_type='experiment'
    # This provides atomic protection for concurrent count-toward-learning requests
    # Using raw SQL for SQLite compatibility with filtered indexes
    op.execute(
        "CREATE UNIQUE INDEX uq_learning_evidence_experiment_ref "
        "ON learning_evidence(user_id, ref_id) "
        "WHERE ref_type='experiment'"
    )


def downgrade() -> None:
    # Remove the uniqueness index
    op.execute("DROP INDEX IF EXISTS uq_learning_evidence_experiment_ref")
