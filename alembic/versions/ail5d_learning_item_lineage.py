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
    table_names = set(inspector.get_table_names())
    # A prior SQLite batch attempt can leave its private copy behind after the
    # DROP TABLE of the referenced canonical table is rejected.  The canonical
    # table is the source of truth; remove only the known temporary object and
    # refuse to guess if the canonical table is missing.
    if "_alembic_tmp_learning_items" in table_names:
        if "learning_items" not in table_names:
            raise RuntimeError(
                "Cannot recover Learning Item lineage migration: canonical learning_items table is missing"
            )
        op.drop_table("_alembic_tmp_learning_items")
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

    invalid_versions = bind.execute(
        sa.text("SELECT COUNT(*) FROM learning_items WHERE version IS NULL OR version < 1")
    ).scalar_one()
    if invalid_versions:
        raise RuntimeError(f"Learning Item lineage backfill found {invalid_versions} invalid version rows")

    # SQLite cannot add NOT NULL/CHECK constraints to an existing table
    # without rebuilding it.  Rebuilding learning_items is unsafe here: the
    # deployed schema has LearningEvidence, experiments, Academy items,
    # milestones, and review attempts referencing this table.  Keep the
    # additive column nullable at the SQLite schema level after proving that
    # every existing row is populated, and enforce the revision identity with
    # indexes.  Application writes already require lineage_id/version and the
    # unique index is the authoritative DB guard for duplicates.
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("learning_items")}
    if "uq_learning_items_lineage_version" not in indexes:
        op.create_index(
            "uq_learning_items_lineage_version",
            "learning_items",
            ["lineage_id", "version"],
            unique=True,
        )
    if "ix_learning_items_lineage_id" not in indexes:
        op.create_index("ix_learning_items_lineage_id", "learning_items", ["lineage_id"])


def downgrade() -> None:
    # Removing the column requires the same unsafe SQLite table rebuild.  The
    # migration is intentionally irreversible once applied; callers must use
    # a backup/restore procedure rather than risk losing historical references.
    raise RuntimeError(
        "Refusing to downgrade Learning Item lineage; restoring a pre-AIL5D backup is required"
    )
