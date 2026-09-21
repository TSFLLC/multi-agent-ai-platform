"""ma7.3a approval resolution note and workflow-node approval uniqueness

Revision ID: a3c9e17b5d42
Revises: bd27cdb01c15
Create Date: 2026-09-20 12:00:00.000000

Additive only: one nullable column and one partial unique index on
``approvals``. No enum/CHECK change, no data rewrite.

* ``resolution_note`` -- part of the immutable decision provenance (spec
  Section 23.3); written once, together with ``resolved_by``/``resolved_at``.
* ``uq_approvals_workflow_node_run_operation`` -- database-level guarantee of
  at most one approval per (workflow node run, operation). Partial, so other
  scopes are unaffected. The ``approvals`` table had no writer before this
  phase (ApprovalService was a stub), so creating the index cannot collide
  with existing rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3c9e17b5d42"
down_revision: Union[str, None] = "bd27cdb01c15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain nullable ADD COLUMN is a native SQLite ALTER (no table
    # rebuild), so the existing CHECK constraints on approvals.scope/status
    # are left exactly as they are.
    op.add_column("approvals", sa.Column("resolution_note", sa.Text(), nullable=True))
    op.create_index(
        "uq_approvals_workflow_node_run_operation",
        "approvals",
        ["scope", "scope_ref_id", "operation_type"],
        unique=True,
        sqlite_where=sa.text("scope = 'workflow_node_run'"),
    )


def downgrade() -> None:
    op.drop_index("uq_approvals_workflow_node_run_operation", table_name="approvals")
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.drop_column("resolution_note")
