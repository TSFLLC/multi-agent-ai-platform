"""AIL.3A Personal Lab foundation.

Revision ID: ail3a_personal_lab_foundation
Revises: ail2b_radar_intelligence
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ail3a_personal_lab_foundation"
down_revision: Union[str, None] = "ail2b_radar_intelligence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "eval_sets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role_term_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_term_id"], ["taxonomy_terms.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_eval_sets_user_id_name"),
    )
    op.create_index("ix_eval_sets_user_id", "eval_sets", ["user_id"])

    op.create_table(
        "eval_set_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("eval_set_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _enum("draft", "published", "frozen", name="evalsetversionstatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["eval_set_id"], ["eval_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("eval_set_id", "version", name="uq_eval_set_versions_set_id_version"),
    )

    op.create_table(
        "eval_set_version_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("eval_set_version_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("task_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["eval_set_version_id"], ["eval_set_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("eval_set_version_id", "position", name="uq_eval_set_version_tasks_position"),
        sa.UniqueConstraint("eval_set_version_id", "task_id", name="uq_eval_set_version_tasks_task"),
    )
    op.create_index("ix_eval_set_version_tasks_version_id", "eval_set_version_tasks", ["eval_set_version_id"])

    op.create_table(
        "experiments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("experiment_type", _enum("model_comparison", "prompt_comparison", "variance", name="experimenttype"), nullable=False),
        sa.Column("status", _enum("draft", "estimated", "awaiting_approval", "approved", "running", "completed", "failed", "cancelled", name="experimentstatus"), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        sa.Column("eval_set_version_id", sa.String(length=36), nullable=True),
        sa.Column("development_id", sa.String(length=36), nullable=True),
        sa.Column("concept_id", sa.String(length=36), nullable=True),
        sa.Column("learning_item_id", sa.String(length=36), nullable=True),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column("config_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("estimated_currency", sa.String(length=10), nullable=False),
        sa.Column("cost_estimate_kind", _enum("known", "estimated", "unknown", name="costestimatekind"), nullable=False),
        sa.Column("budget_id", sa.String(length=36), nullable=True),
        sa.Column("approval_id", sa.String(length=36), nullable=True),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["eval_set_version_id"], ["eval_set_versions.id"]),
        sa.ForeignKeyConstraint(["development_id"], ["developments.id"]),
        sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"]),
        sa.ForeignKeyConstraint(["learning_item_id"], ["learning_items.id"]),
        sa.ForeignKeyConstraint(["budget_id"], ["budgets.id"]),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_experiments_user_id", "experiments", ["user_id"])

    op.create_table(
        "experiment_agent_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("agent_version_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "agent_version_id", name="uq_experiment_agent_versions_pair"),
    )

    op.create_table(
        "experiment_models",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("provider_model_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["provider_model_snapshot_id"], ["provider_model_snapshots.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "provider_model_snapshot_id", name="uq_experiment_models_snapshot"),
    )

    # Additive SQLite-safe seam; no existing run is tagged and no table
    # recreation is required.
    # Alembic's generic add_column emits a second ALTER for the FK, which
    # SQLite rejects. SQLite supports this nullable additive form directly;
    # it avoids recreating the heavily referenced task_runs table.
    op.execute(
        'ALTER TABLE "task_runs" ADD COLUMN experiment_id VARCHAR(36) '
        'REFERENCES experiments(id) ON DELETE SET NULL'
    )
    op.create_index("ix_task_runs_experiment_id", "task_runs", ["experiment_id"])


def downgrade() -> None:
    op.drop_index("ix_task_runs_experiment_id", table_name="task_runs")
    op.execute('ALTER TABLE "task_runs" DROP COLUMN experiment_id')
    op.drop_table("experiment_models")
    op.drop_table("experiment_agent_versions")
    op.drop_index("ix_experiments_user_id", table_name="experiments")
    op.drop_table("experiments")
    op.drop_index("ix_eval_set_version_tasks_version_id", table_name="eval_set_version_tasks")
    op.drop_table("eval_set_version_tasks")
    op.drop_table("eval_set_versions")
    op.drop_index("ix_eval_sets_user_id", table_name="eval_sets")
    op.drop_table("eval_sets")
