"""AIL.5D: establish immutable Learning Item revision lineages.

The migration is additive. Existing Learning Item rows retain their IDs and
content; each historical row receives its own lineage as Version 1.
"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "ail5d_learning_item_lineage"
down_revision: Union[str, None] = "ail5c_grader_agent_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("learning_items")}
    if "lineage_id" not in columns:
        op.add_column("learning_items", sa.Column("lineage_id", sa.String(length=36), nullable=True))

    rows = bind.execute(
        sa.text("SELECT id FROM learning_items WHERE lineage_id IS NULL ORDER BY id")
    ).fetchall()
    for (item_id,) in rows:
        bind.execute(
            sa.text("UPDATE learning_items SET lineage_id = :lineage_id WHERE id = :item_id"),
            {"lineage_id": str(uuid.uuid4()), "item_id": item_id},
        )

    null_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM learning_items WHERE lineage_id IS NULL")
    ).scalar_one()
    if null_count:
        raise RuntimeError(f"Learning Item lineage backfill left {null_count} NULL rows")

    duplicates = bind.execute(
        sa.text(
            "SELECT lineage_id, version, COUNT(*) FROM learning_items "
            "GROUP BY lineage_id, version HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if duplicates:
        raise RuntimeError("Existing Learning Items contain duplicate lineage/version pairs")

    with op.batch_alter_table("learning_items", schema=None) as batch_op:
        batch_op.alter_column(
            "lineage_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_learning_items_lineage_version", ["lineage_id", "version"]
        )
        batch_op.create_check_constraint(
            "ck_learning_items_version_positive", "version >= 1"
        )

    op.create_index("ix_learning_items_lineage_id", "learning_items", ["lineage_id"])


def downgrade() -> None:
    bind = op.get_bind()
    has_revisions = bind.execute(
        sa.text("SELECT COUNT(*) FROM learning_items WHERE version > 1")
    ).scalar_one()
    if has_revisions:
        raise RuntimeError("Refusing to remove Learning Item lineage while revisions exist")

    op.drop_index("ix_learning_items_lineage_id", table_name="learning_items")
    with op.batch_alter_table("learning_items", schema=None) as batch_op:
        batch_op.drop_constraint("uq_learning_items_lineage_version", type_="unique")
        batch_op.drop_constraint("ck_learning_items_version_positive", type_="check")
        batch_op.drop_column("lineage_id")
