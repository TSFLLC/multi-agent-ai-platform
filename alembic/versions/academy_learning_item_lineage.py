"""Academy: establish immutable Learning Item revision lineages.

Infrastructure for the Level 1 Academy integration: a Learning Item can be
revised by appending ``version + 1`` to its lineage, never by editing a row.
This is NOT the future Structured Learning Experience (lesson steps, step
progress); it only makes Learning Items revisable.

The migration is additive. Existing Learning Item rows retain their IDs and
content; each historical row receives its own lineage as Version 1.

Schema contract
---------------
* ``lineage_id`` is populated for every row (deterministic uuid5 for history)
  and ``(lineage_id, version)`` is unique: enforced by a UNIQUE INDEX on every
  engine.
* ``lineage_id IS NOT NULL`` and ``version >= 1`` are enforced by the database
  too, without rebuilding ``learning_items`` (referenced by evidence,
  experiments, milestones and review attempts, so a SQLite batch rebuild is
  unsafe): on SQLite by ``BEFORE INSERT/UPDATE`` triggers that ABORT, on any
  other engine by a real NOT NULL and CHECK constraint. The ORM declares the
  same NOT NULL / CHECK for ``create_all`` databases and additionally validates
  in Python, so a bad value fails closed before it reaches the database.
* A future batch rebuild of ``learning_items`` drops the SQLite triggers; the
  schema-contract tests exist to catch that.

Rollback policy: IRREVERSIBLE. See ``docs/deployment/migration-rollback-policy.md``.
"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "academy_learning_item_lineage"
down_revision: Union[str, None] = "ail5c_grader_agent_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Declared so tests (and reviewers) can see that ``downgrade`` refuses on purpose.
IRREVERSIBLE = True

_TRIGGER_INSERT = "trg_learning_items_lineage_insert"
_TRIGGER_UPDATE = "trg_learning_items_lineage_update"
_TRIGGER_MESSAGE = "learning_items: lineage_id is required and version must be >= 1"

_LINEAGE_NAMESPACE = uuid.UUID("5d9a3b42-8a61-4cf8-b3e6-0a89d0de5d5d")


def _deterministic_lineage_id(item_id: str) -> str:
    """Return the stable Version-1 lineage for a historical item."""

    return str(uuid.uuid5(_LINEAGE_NAMESPACE, f"learning-item:{item_id}"))


def _validate_lineage_column(column: dict) -> None:
    """Accept only the column shape produced by this migration's safe add."""

    column_type = column["type"]
    if not isinstance(column_type, sa.String) or column_type.length != 36:
        raise RuntimeError("Cannot resume Learning Item lineage migration: lineage_id must be VARCHAR(36)")
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

    rows = bind.execute(sa.text("SELECT id, lineage_id FROM learning_items ORDER BY id")).fetchall()
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
    _enforce_not_null_and_positive_version(bind)


def _enforce_not_null_and_positive_version(bind) -> None:
    """Every row was just proven populated and valid; make the database keep it so.

    SQLite cannot add NOT NULL / CHECK to an existing table without a full
    rebuild of ``learning_items``, which is referenced by other tables and is
    unsafe here.  Triggers give the same fail-closed behaviour (the statement is
    aborted and surfaces as an IntegrityError) with no rebuild.  ``IF NOT
    EXISTS`` keeps an interrupted migration resumable.
    """
    if bind.dialect.name == "sqlite":
        condition = "NEW.lineage_id IS NULL OR NEW.version IS NULL OR NEW.version < 1"
        bind.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_TRIGGER_INSERT} BEFORE INSERT ON learning_items "
            f"WHEN {condition} BEGIN SELECT RAISE(ABORT, '{_TRIGGER_MESSAGE}'); END"
        )
        bind.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_TRIGGER_UPDATE} BEFORE UPDATE OF lineage_id, version "
            f"ON learning_items WHEN {condition} BEGIN SELECT RAISE(ABORT, '{_TRIGGER_MESSAGE}'); END"
        )
        return
    # Not exercised by the SQLite-only test suite; real constraints on other engines.
    op.alter_column("learning_items", "lineage_id", existing_type=sa.String(length=36), nullable=False)
    op.create_check_constraint("ck_learning_items_version_positive", "learning_items", "version >= 1")


def downgrade() -> None:
    # Removing the column requires the same unsafe SQLite table rebuild, and the
    # revision lineage is historical evidence.  The migration is intentionally
    # irreversible once applied: rollback is a restore of the pre-migration
    # backup, never a downgrade.  Nothing is changed before refusing.
    raise RuntimeError(
        "Refusing to downgrade Learning Item lineage; restoring a pre-lineage backup is required"
    )
