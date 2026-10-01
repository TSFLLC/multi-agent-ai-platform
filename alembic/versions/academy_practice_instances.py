"""AIL.5D.5: Educational Lab Kit Practice Instances.

Adds TWO tables and changes nothing else:

* ``academy_practice_instances`` - a learner's own, server-owned Practice Instance of an authored Lab Kit scenario (frozen
  scenario, exact Day version/step, forward-only status, per-instance run cap, canonical assistance summary, the single
  practice evidence row it produced);
* ``academy_practice_runs`` - one governed run per row, linking the learner's chosen variables to the existing Task Run (the raw
  execution result stays in the existing execution system).

No existing table, row, index or trigger is touched; no evidence, Professor, Personal Lab or AIL.5C table is altered.

Sits on top of ``academy_professor_help`` (AIL.5D.4). Reversible and safe by default: ``downgrade`` drops only what this
revision created and REFUSES (changing nothing) while any practice row exists, because that is learner data
(docs/deployment/migration-rollback-policy.md).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "academy_practice_instances"
down_revision: Union[str, None] = "academy_professor_help"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INSTANCES = "academy_practice_instances"
RUNS = "academy_practice_runs"
INSTANCE_INDEX = "ix_academy_practice_instances_user_lineage_step"


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in (INSTANCES, RUNS):
        if table in existing:
            raise RuntimeError(f"Cannot create {table}: it already exists outside this migration")
    op.create_table(
        INSTANCES,
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("learning_item_id", sa.String(length=36), nullable=False),
        sa.Column("lineage_id", sa.String(length=36), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.Enum("guided", "independent", name="practicemode", native_enum=False, create_constraint=True), nullable=False),
        sa.Column("kit_key", sa.String(length=80), nullable=False),
        sa.Column("kit_version", sa.Integer(), nullable=False),
        sa.Column("scenario_key", sa.String(length=80), nullable=False),
        sa.Column("scenario_sha256", sa.String(length=64), nullable=False),
        sa.Column("scenario_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("created", "predicted", "running", "observing", "comparing", "reflecting", "completed", "abandoned",
                    name="practiceinstancestatus", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("max_runs", sa.Integer(), nullable=False),
        sa.Column("budget_id", sa.String(length=36), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_id", sa.String(length=36), nullable=True),
        sa.Column("assistance_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_runs >= 1", name=op.f("ck_academy_practice_instances_max_runs")),
        sa.CheckConstraint("attempt_no >= 1", name=op.f("ck_academy_practice_instances_attempt_no")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["learning_item_id"], ["learning_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_academy_practice_instances")),
    )
    op.create_index(INSTANCE_INDEX, INSTANCES, ["user_id", "lineage_id", "step_key"], unique=False)
    op.create_table(
        RUNS,
        sa.Column("instance_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("variables_json", sa.JSON(), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("model_canonical_id", sa.String(length=255), nullable=True),
        sa.Column("task_run_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instance_id"], [f"{INSTANCES}.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_academy_practice_runs")),
        sa.UniqueConstraint("instance_id", "seq", name="uq_academy_practice_runs_instance_seq"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    rows = 0
    for table in (RUNS, INSTANCES):
        if table in existing:
            rows += bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
    if rows:
        raise RuntimeError(
            f"Cannot drop the practice tables: {rows} practice row(s) exist. "
            "Restore the pre-migration backup instead (docs/deployment/migration-rollback-policy.md)."
        )
    if RUNS in existing:
        op.drop_table(RUNS)
    if INSTANCES in existing:
        op.drop_index(INSTANCE_INDEX, table_name=INSTANCES)
        op.drop_table(INSTANCES)
