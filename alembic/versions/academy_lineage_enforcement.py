"""Academy: enforce the Learning Item lineage contract in the database.

Forward revision on top of ``ail5d_learning_item_lineage``.

That earlier revision is DEPLOYED HISTORY (it is applied on the hosted staging
database) and must stay exactly as it shipped. Its ``ail5d_`` prefix is a naming
accident: it makes Learning Items revisable (``lineage_id`` + ``version``) and is
infrastructure for the Level 1 Academy. It is NOT the future Structured Learning
Experience (lesson steps, step progress, step-scoped Professor, curriculum v3),
which has not started.

What the deployed revision left in place, and what this one adds
-----------------------------------------------------------------
* ``lineage_id`` backfilled for every row, ``(lineage_id, version)`` unique index,
  ``ix_learning_items_lineage_id``  -> deployed by ``ail5d_learning_item_lineage``.
* ``lineage_id`` stayed NULLABLE and there was no ``version >= 1`` rule (SQLite
  cannot add NOT NULL / CHECK to a table other tables reference without a risky
  rebuild)                             -> enforced HERE.

Enforcement, without rebuilding ``learning_items``:

* SQLite: ``BEFORE INSERT`` and ``BEFORE UPDATE OF lineage_id, version`` triggers
  that ABORT on a NULL lineage_id, NULL version, or version < 1 (surfaces as an
  IntegrityError, same as a CHECK would).
* Any other engine: a real NOT NULL and ``ck_learning_items_version_positive``.

The revision is shape-agnostic and idempotent so that BOTH paths reach the same
final contract: an already-migrated database (only this revision runs) and a
fresh / pre-AIL5 database (the whole chain runs). It first proves the data is
valid and refuses, changing nothing, if it is not; it creates the unique index and
lineage index only when no equivalent one already exists.

Reversible: ``downgrade`` removes only what this revision added (the triggers, or
the NOT NULL / CHECK); it never touches lineage data. The deployed
``ail5d_learning_item_lineage`` revision below it remains irreversible; see
``docs/deployment/migration-rollback-policy.md``.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "academy_lineage_enforcement"
down_revision: Union[str, None] = "ail5d_learning_item_lineage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TRIGGER_INSERT = "trg_learning_items_lineage_insert"
_TRIGGER_UPDATE = "trg_learning_items_lineage_update"
_MESSAGE = "learning_items: lineage_id is required and version must be >= 1"
_UNIQUE_INDEX = "uq_learning_items_lineage_version"
_LINEAGE_INDEX = "ix_learning_items_lineage_id"
_CHECK = "ck_learning_items_version_positive"


def _require_valid_data(bind) -> None:
    columns = {c["name"] for c in sa.inspect(bind).get_columns("learning_items")}
    if "lineage_id" not in columns:
        raise RuntimeError(
            "Cannot enforce Learning Item lineage: learning_items.lineage_id is missing "
            "(ail5d_learning_item_lineage has not been applied)"
        )
    nulls = bind.execute(sa.text("SELECT COUNT(*) FROM learning_items WHERE lineage_id IS NULL")).scalar_one()
    if nulls:
        raise RuntimeError(f"Cannot enforce Learning Item lineage: {nulls} row(s) have a NULL lineage_id")
    bad_versions = bind.execute(
        sa.text("SELECT COUNT(*) FROM learning_items WHERE version IS NULL OR version < 1")
    ).scalar_one()
    if bad_versions:
        raise RuntimeError(f"Cannot enforce Learning Item lineage: {bad_versions} row(s) have version < 1")
    duplicates = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM (SELECT 1 FROM learning_items GROUP BY lineage_id, version HAVING COUNT(*) > 1)"
        )
    ).scalar_one()
    if duplicates:
        raise RuntimeError(
            f"Cannot enforce Learning Item lineage: {duplicates} duplicate (lineage_id, version) group(s)"
        )


def _ensure_indexes(bind) -> None:
    inspector = sa.inspect(bind)
    unique_sets = [
        tuple(i["column_names"]) for i in inspector.get_indexes("learning_items") if i.get("unique")
    ]
    unique_sets += [tuple(u["column_names"]) for u in inspector.get_unique_constraints("learning_items")]
    if ("lineage_id", "version") not in unique_sets:
        op.create_index(_UNIQUE_INDEX, "learning_items", ["lineage_id", "version"], unique=True)
    if not any(
        tuple(i["column_names"])[:1] == ("lineage_id",) for i in inspector.get_indexes("learning_items")
    ):
        op.create_index(_LINEAGE_INDEX, "learning_items", ["lineage_id"])


def upgrade() -> None:
    bind = op.get_bind()
    _require_valid_data(bind)
    _ensure_indexes(bind)
    if bind.dialect.name == "sqlite":
        condition = "NEW.lineage_id IS NULL OR NEW.version IS NULL OR NEW.version < 1"
        bind.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_TRIGGER_INSERT} BEFORE INSERT ON learning_items "
            f"WHEN {condition} BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END"
        )
        bind.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_TRIGGER_UPDATE} BEFORE UPDATE OF lineage_id, version "
            f"ON learning_items WHEN {condition} BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END"
        )
        return
    # Not exercised by the SQLite-only test suite: real constraints on other engines.
    inspector = sa.inspect(bind)
    lineage = next(c for c in inspector.get_columns("learning_items") if c["name"] == "lineage_id")
    if lineage["nullable"]:
        op.alter_column("learning_items", "lineage_id", existing_type=sa.String(length=36), nullable=False)
    if _CHECK not in {c["name"] for c in inspector.get_check_constraints("learning_items")}:
        op.create_check_constraint(_CHECK, "learning_items", "version >= 1")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_TRIGGER_INSERT}")
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_TRIGGER_UPDATE}")
        return
    inspector = sa.inspect(bind)
    if _CHECK in {c["name"] for c in inspector.get_check_constraints("learning_items")}:
        op.drop_constraint(_CHECK, "learning_items", type_="check")
    op.alter_column("learning_items", "lineage_id", existing_type=sa.String(length=36), nullable=True)
