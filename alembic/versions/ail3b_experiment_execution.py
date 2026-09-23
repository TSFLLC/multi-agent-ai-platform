"""AIL.3B experiment execution metadata.

Additive columns only: existing MA5 comparisons remain unchanged.
"""

from alembic import op
import sqlalchemy as sa

revision = "ail3b_experiment_execution"
down_revision = "ail3a_personal_lab_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experiment_task_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("task_run_id", sa.String(length=36), nullable=False),
        sa.Column("task_position", sa.Integer(), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "task_run_id", name="uq_experiment_task_runs_pair"),
        sa.UniqueConstraint("experiment_id", "task_position", "repetition", "label", name="uq_experiment_task_runs_slot"),
    )
    op.create_index("ix_experiment_task_runs_experiment_id", "experiment_task_runs", ["experiment_id"])
    op.execute(
        'ALTER TABLE "comparison_runs" ADD COLUMN experiment_id VARCHAR(36) '
        'REFERENCES experiments(id) ON DELETE CASCADE'
    )
    op.add_column("comparison_runs", sa.Column("experiment_task_position", sa.Integer(), nullable=True))
    op.add_column("comparison_runs", sa.Column("experiment_repetition", sa.Integer(), nullable=True))
    op.create_index("ix_comparison_runs_experiment_id", "comparison_runs", ["experiment_id"])


def downgrade() -> None:
    op.drop_index("ix_experiment_task_runs_experiment_id", table_name="experiment_task_runs")
    op.drop_table("experiment_task_runs")
    op.drop_index("ix_comparison_runs_experiment_id", table_name="comparison_runs")
    op.drop_column("comparison_runs", "experiment_repetition")
    op.drop_column("comparison_runs", "experiment_task_position")
    # SQLite supports DROP COLUMN in the supported runtime; this column is
    # nullable and additive, so no data-preserving rebuild is required.
    op.drop_column("comparison_runs", "experiment_id")
