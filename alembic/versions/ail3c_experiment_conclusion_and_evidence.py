"""ail3c_experiment_conclusion_and_evidence

Revision ID: ail3c_experiment_conclusion
Revises: ail3b_experiment_execution
Create Date: 2026-09-23 16:00:00

AIL.3C: Add human conclusion columns to Experiment model.

This is a minimal, non-complex migration that only adds three nullable
columns for human-provided conclusion. No enum modification or constraint
gymnastics - the enum is updated in app/db/enums.py only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ail3c_experiment_conclusion'
down_revision: Union[str, None] = 'ail3b_experiment_execution'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add concept version provenance to freeze the version at experiment creation
    op.add_column('experiments', sa.Column('concept_version_id', sa.String(36), nullable=True))

    # Add human conclusion columns to experiments table (simple additive change)
    op.add_column('experiments', sa.Column('conclusion_type', sa.String(50), nullable=True))
    op.add_column('experiments', sa.Column('conclusion_text', sa.Text(), nullable=True))
    op.add_column('experiments', sa.Column('concluded_at', sa.DateTime(timezone=True), nullable=True))

    # Idempotency protection: application layer checks for existing evidence before creation
    # Future migration can add database-level uniqueness constraint once schema stabilizes


def downgrade() -> None:
    # Remove human conclusion columns from experiments table
    op.drop_column('experiments', 'concluded_at')
    op.drop_column('experiments', 'conclusion_text')
    op.drop_column('experiments', 'conclusion_type')

    # Remove concept version provenance
    op.drop_column('experiments', 'concept_version_id')
