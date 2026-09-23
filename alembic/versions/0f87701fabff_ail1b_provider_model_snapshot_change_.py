"""ail1b provider model snapshot change history

Revision ID: 0f87701fabff
Revises: ef873dac62a1
Create Date: 2026-09-22 00:00:00.000000

"""
from typing import Sequence, Tuple, Union

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0f87701fabff'
down_revision: Union[str, None] = 'ef873dac62a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SNAPSHOT_TABLE = "provider_model_snapshots"
_TEMP_SNAPSHOT_TABLE = "_alembic_tmp_provider_model_snapshots"
_OLD_COLUMNS = (
    "provider_model_id",
    "model_id",
    "provider_id",
    "pricing_input_per_mtok",
    "pricing_output_per_mtok",
    "currency",
    "context_window",
    "capability_snapshot_json",
    "snapshotted_at",
    "id",
)
_NEW_COLUMNS = _OLD_COLUMNS + ("source", "change_kinds_json")
_SOURCE_CHECK = "source IN ('execution_freeze', 'catalog_refresh', 'legacy_unknown')"
_SNAPSHOT_INDEX = "ix_provider_model_snapshots_provider_model_id_source_snapshotted_at"


def _table_exists(bind: Connection, table_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": table_name},
        ).scalar()
    )


def _table_columns(bind: Connection, table_name: str) -> Tuple[str, ...]:
    rows = bind.execute(sa.text(f'PRAGMA table_info("{table_name}")')).mappings().all()
    return tuple(row["name"] for row in rows)


def _known_orphan_temp_table(bind: Connection) -> bool:
    """Validate and remove only the exact failed AIL.1B batch state.

    The first staging attempt created the temporary table, copied no rows,
    then failed while dropping the referenced canonical table. A retry must
    not blindly remove an arbitrary similarly-named table: a non-empty or
    differently-shaped table may contain data from an interrupted operation.
    """

    if not _table_exists(bind, _TEMP_SNAPSHOT_TABLE):
        return False

    if not _table_exists(bind, _SNAPSHOT_TABLE):
        raise RuntimeError(
            "Refusing AIL.1B recovery: provider_model_snapshots is absent while "
            "_alembic_tmp_provider_model_snapshots exists"
        )

    canonical_columns = _table_columns(bind, _SNAPSHOT_TABLE)
    temp_columns = _table_columns(bind, _TEMP_SNAPSHOT_TABLE)
    if canonical_columns != _OLD_COLUMNS:
        raise RuntimeError(
            "Refusing AIL.1B recovery: provider_model_snapshots has an unexpected "
            f"schema ({canonical_columns!r})"
        )
    if temp_columns != _NEW_COLUMNS:
        raise RuntimeError(
            "Refusing AIL.1B recovery: _alembic_tmp_provider_model_snapshots has "
            f"an unexpected schema ({temp_columns!r})"
        )

    temp_count = bind.execute(
        sa.text(f'SELECT count(*) FROM "{_TEMP_SNAPSHOT_TABLE}"')
    ).scalar_one()
    if temp_count != 0:
        raise RuntimeError(
            "Refusing AIL.1B recovery: _alembic_tmp_provider_model_snapshots "
            f"contains {temp_count} row(s)"
        )

    temp_sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": _TEMP_SNAPSHOT_TABLE},
    ).scalar_one_or_none()
    normalized_sql = " ".join((temp_sql or "").lower().split())
    if _SOURCE_CHECK.lower() not in normalized_sql:
        raise RuntimeError(
            "Refusing AIL.1B recovery: _alembic_tmp_provider_model_snapshots "
            "does not contain the expected source CHECK constraint"
        )

    bind.execute(sa.text(f'DROP TABLE "{_TEMP_SNAPSHOT_TABLE}"'))
    return True


def _assert_sqlite_snapshot_state(bind: Connection, *, expected_columns: Tuple[str, ...]) -> None:
    actual_columns = _table_columns(bind, _SNAPSHOT_TABLE)
    if actual_columns != expected_columns:
        raise RuntimeError(
            f"Unexpected provider_model_snapshots schema after AIL.1B migration: {actual_columns!r}"
        )


def _ensure_foreign_keys_on(bind: Connection) -> None:
    """Restore the connection invariant after an earlier SQLite migration.

    MA7.5A commits before changing this connection pragma, but leaves the
    pragma disabled when Alembic continues on the same connection.  SQLite
    ignores ``PRAGMA foreign_keys=ON`` while a transaction is active, so the
    commit is intentional and mirrors the established migration pattern.
    """

    if bind.execute(sa.text("PRAGMA foreign_keys")).scalar_one() == 1:
        return

    raw = bind.connection.dbapi_connection
    if raw.in_transaction:
        raw.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")
    if bind.execute(sa.text("PRAGMA foreign_keys")).scalar_one() != 1:
        raise RuntimeError("Cannot apply AIL.1B: SQLite foreign-key enforcement is disabled")


def _upgrade_sqlite(bind: Connection) -> None:
    _ensure_foreign_keys_on(bind)
    if not _table_exists(bind, _SNAPSHOT_TABLE):
        raise RuntimeError("Cannot apply AIL.1B: provider_model_snapshots does not exist")

    # A stale Alembic version with the final columns already present is
    # ambiguous and must not be silently normalized.
    if _table_columns(bind, _SNAPSHOT_TABLE) != _OLD_COLUMNS:
        raise RuntimeError(
            "Cannot apply AIL.1B: provider_model_snapshots is neither the "
            "pre-migration schema nor a known recoverable state"
        )

    _known_orphan_temp_table(bind)

    # SQLite ADD COLUMN does not recreate the referenced table, so foreign-key
    # enforcement remains ON and existing agent/routing/call references remain
    # valid. The CHECK is attached to the new column and existing rows receive
    # the documented legacy_unknown default.
    bind.execute(
        sa.text(
            f'ALTER TABLE "{_SNAPSHOT_TABLE}" ADD COLUMN source VARCHAR(16) '
            f"NOT NULL DEFAULT 'legacy_unknown' CHECK ({_SOURCE_CHECK})"
        )
    )
    bind.execute(
        sa.text(f'ALTER TABLE "{_SNAPSHOT_TABLE}" ADD COLUMN change_kinds_json JSON')
    )
    op.create_index(
        _SNAPSHOT_INDEX,
        _SNAPSHOT_TABLE,
        ["provider_model_id", "source", "snapshotted_at"],
    )
    _assert_sqlite_snapshot_state(bind, expected_columns=_NEW_COLUMNS)
    if bind.execute(sa.text("PRAGMA foreign_keys")).scalar_one() != 1:
        raise RuntimeError("AIL.1B migration left SQLite foreign-key enforcement disabled")


def upgrade() -> None:
    # AIL.1B: distinguish the two purposes provider_model_snapshots rows
    # already serve (execution-time freeze vs. catalog-refresh history) and
    # let a catalog-refresh row record exactly which dimensions changed.
    # Existing rows predate this distinction and are honestly labeled
    # 'legacy_unknown' rather than guessed as one or the other; change_kinds
    # is left NULL for them (never backfilled with a guessed classification).
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _upgrade_sqlite(bind)
        return

    with op.batch_alter_table(_SNAPSHOT_TABLE, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.Enum(
                    "execution_freeze",
                    "catalog_refresh",
                    "legacy_unknown",
                    name="snapshotsource",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default="legacy_unknown",
            )
        )
        batch_op.add_column(sa.Column("change_kinds_json", sa.JSON(), nullable=True))
        batch_op.create_index(_SNAPSHOT_INDEX, ["provider_model_id", "source", "snapshotted_at"])


def downgrade() -> None:
    # A native_enum=False Enum(create_constraint=True) column can surface
    # its CHECK constraint under either the bare or the
    # naming-convention-prefixed name depending on SQLAlchemy version —
    # both are dropped conditionally (checked names actually present), the
    # same pattern already used by bd27cdb01c15's downgrade, so the batch
    # table-recreate never trips over a leftover constraint referencing an
    # already-dropped column.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        if _table_columns(bind, _SNAPSHOT_TABLE) != _NEW_COLUMNS:
            raise RuntimeError("Refusing AIL.1B downgrade: unexpected provider_model_snapshots schema")
        inspector = sa.inspect(bind)
        if any(index["name"] == _SNAPSHOT_INDEX for index in inspector.get_indexes(_SNAPSHOT_TABLE)):
            op.drop_index(_SNAPSHOT_INDEX, table_name=_SNAPSHOT_TABLE)
        bind.execute(sa.text(f'ALTER TABLE "{_SNAPSHOT_TABLE}" DROP COLUMN change_kinds_json'))
        bind.execute(sa.text(f'ALTER TABLE "{_SNAPSHOT_TABLE}" DROP COLUMN source'))
        _assert_sqlite_snapshot_state(bind, expected_columns=_OLD_COLUMNS)
        return

    existing_check_names = {
        c["name"] for c in sa.inspect(bind).get_check_constraints(_SNAPSHOT_TABLE)
    }

    with op.batch_alter_table(_SNAPSHOT_TABLE, schema=None) as batch_op:
        batch_op.drop_index(_SNAPSHOT_INDEX)
        if "snapshotsource" in existing_check_names:
            batch_op.drop_constraint(batch_op.f("snapshotsource"), type_="check")
        if "ck_provider_model_snapshots_snapshotsource" in existing_check_names:
            batch_op.drop_constraint(batch_op.f("ck_provider_model_snapshots_snapshotsource"), type_="check")
        batch_op.drop_column('change_kinds_json')
        batch_op.drop_column('source')
