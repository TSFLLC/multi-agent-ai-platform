"""ail1b provider model snapshot change history

Revision ID: 0f87701fabff
Revises: ef873dac62a1
Create Date: 2026-09-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0f87701fabff'
down_revision: Union[str, None] = 'ef873dac62a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # AIL.1B: distinguish the two purposes provider_model_snapshots rows
    # already serve (execution-time freeze vs. catalog-refresh history) and
    # let a catalog-refresh row record exactly which dimensions changed.
    # Existing rows predate this distinction and are honestly labeled
    # 'legacy_unknown' rather than guessed as one or the other; change_kinds
    # is left NULL for them (never backfilled with a guessed classification).
    with op.batch_alter_table('provider_model_snapshots', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'source',
                sa.Enum(
                    'execution_freeze',
                    'catalog_refresh',
                    'legacy_unknown',
                    name='snapshotsource',
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default='legacy_unknown',
            )
        )
        batch_op.add_column(sa.Column('change_kinds_json', sa.JSON(), nullable=True))
        batch_op.create_index(
            'ix_provider_model_snapshots_provider_model_id_source_snapshotted_at',
            ['provider_model_id', 'source', 'snapshotted_at'],
        )


def downgrade() -> None:
    # A native_enum=False Enum(create_constraint=True) column can surface
    # its CHECK constraint under either the bare or the
    # naming-convention-prefixed name depending on SQLAlchemy version —
    # both are dropped conditionally (checked names actually present), the
    # same pattern already used by bd27cdb01c15's downgrade, so the batch
    # table-recreate never trips over a leftover constraint referencing an
    # already-dropped column.
    bind = op.get_bind()
    existing_check_names = {
        c["name"] for c in sa.inspect(bind).get_check_constraints("provider_model_snapshots")
    }

    with op.batch_alter_table('provider_model_snapshots', schema=None) as batch_op:
        batch_op.drop_index('ix_provider_model_snapshots_provider_model_id_source_snapshotted_at')
        if "snapshotsource" in existing_check_names:
            batch_op.drop_constraint(batch_op.f("snapshotsource"), type_="check")
        if "ck_provider_model_snapshots_snapshotsource" in existing_check_names:
            batch_op.drop_constraint(batch_op.f("ck_provider_model_snapshots_snapshotsource"), type_="check")
        batch_op.drop_column('change_kinds_json')
        batch_op.drop_column('source')
