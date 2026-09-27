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

_LINEAGE_NAMESPACE = uuid.UUID("5d9a3b42-8a61-4cf8-b3e6-0a89d0de5d5d")


def _deterministic_lineage_id(item_id: str) -> str:
    """Return the stable Version-1 lineage for a historical item."""

    return str(uuid.uuid5(_LINEAGE_NAMESPACE, f"learning-item:{item_id}"))


def _validate_lineage_column(column: dict) -> None:
    """Accept only the column shape produced by this migration's safe add."""

    column_type = column["type"]
    if not isinstance(column_type, sa.String) or column_type.length != 36:
        raise RuntimeError(
            "Cannot resume Learning Item lineage migration: lineage_id must be VARCHAR(36)"
        )
    if column.get("default") is not None:
        raise RuntimeError(
            "Cannot resume Learning Item lineage migration: lineage_id has an unexpected default"
        )


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
    learning_item_columns = inspector.get_columns("learning_items")
    lineage_column = next(
        (column for column in learning_item_columns if column["name"] == "lineage_id"),
        None,
    )
    if lineage_column is None:
        op.add_column("learning_items", sa.Column("lineage_id", sa.String(length=36), nullable=True))
    else:
        # SQLite can persist the ADD COLUMN from the interrupted attempt while
        # Alembic remains at ail5c. Resume only the exact compatible shape; do
        # not guess at arbitrary pre-existing lineage schemas.
        _validate_lineage_column(lineage_column)

    rows = bind.execute(
        sa.text("SELECT id, lineage_id FROM learning_items ORDER BY id")
    ).fetchall()
    for item_id, lineage_id in rows:
        if lineage_id is None:
            bind.execute(
                sa.text("UPDATE learning_items SET lineage_id = :lineage_id WHERE id = :item_id"),
                {"lineage_id": _deterministic_lineage_id(item_id), "item_id": item_id},
            )
            continue
        try:
            parsed_lineage_id = uuid.UUID(str(lineage_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise RuntimeError(
                f"Cannot resume Learning Item lineage migration: invalid lineage_id for {item_id}"
            ) from exc
        if str(parsed_lineage_id) != str(lineage_id).lower():
            raise RuntimeError(
                f"Cannot resume Learning Item lineage migration: non-canonical lineage_id for {item_id}"
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
