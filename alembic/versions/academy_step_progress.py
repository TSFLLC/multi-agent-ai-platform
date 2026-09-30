"""AIL.5D.1 Structured Learning Foundation: learner step progress.

Adds ONE table, ``academy_step_progress``: mutable, user-scoped learner state for the ordered authored steps that a
Learning Item carries in ``spec_json.steps``. Nothing else changes:

* no column is added to, or removed from, ``learning_items`` (authored steps are ordinary immutable, versioned
  Learning Item content; there is no second content store and no second versioning system);
* no existing row is read, rewritten or backfilled -- a Learning Item without steps has no progress rows;
* no evidence, assessment, Professor or Personal Lab table is touched.

Naming: this is the *Structured Learning Experience* work. It is unrelated to the historical
``ail5d_learning_item_lineage`` revision (deployed history, Learning Item revision lineage), which is neither
renamed nor modified; this revision sits on top of ``academy_lineage_enforcement``.

Reversible, and safe by default: ``downgrade`` drops only what this revision created and REFUSES (changing
nothing) while any learner progress exists, because that is learner data. The platform's rollback mechanism
remains restoring the pre-migration backup (docs/deployment/migration-rollback-policy.md).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "academy_step_progress"
down_revision: Union[str, None] = "academy_lineage_enforcement"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "academy_step_progress"
INDEX = "ix_academy_step_progress_user_lineage"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in sa.inspect(bind).get_table_names():
        raise RuntimeError(f"Cannot create {TABLE}: it already exists outside this migration")
    op.create_table(
        TABLE,
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("learning_item_id", sa.String(length=36), nullable=False),
        sa.Column("lineage_id", sa.String(length=36), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column("step_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("opened", "completed", "skipped", name="academystepstatus", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("open_count", sa.Integer(), nullable=False),
        sa.Column("first_opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_basis_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("open_count >= 1", name=op.f("ck_academy_step_progress_open_count")),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL)",
            name=op.f("ck_academy_step_progress_completed_at"),
        ),
        sa.CheckConstraint(
            "(status = 'skipped' AND skipped_at IS NOT NULL) OR (status <> 'skipped' AND skipped_at IS NULL)",
            name=op.f("ck_academy_step_progress_skipped_at"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["learning_item_id"], ["learning_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_academy_step_progress")),
        sa.UniqueConstraint("user_id", "learning_item_id", "step_key", name="uq_academy_step_progress_user_item_step"),
    )
    op.create_index(INDEX, TABLE, ["user_id", "lineage_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    rows = bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one()
    if rows:
        raise RuntimeError(
            f"Cannot drop {TABLE}: {rows} learner step-progress row(s) exist. "
            "Restore the pre-migration backup instead (docs/deployment/migration-rollback-policy.md)."
        )
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
