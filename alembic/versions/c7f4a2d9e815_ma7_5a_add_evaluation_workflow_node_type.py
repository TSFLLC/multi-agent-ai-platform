"""ma7.5a add the evaluation workflow node type

Revision ID: c7f4a2d9e815
Revises: a3c9e17b5d42
Create Date: 2026-09-21 12:00:00.000000

Widens ``workflow_nodes.node_type``'s CHECK constraint to additionally allow
``'evaluation'`` (app.db.enums.WorkflowNodeType.EVALUATION). Purely additive:
no new table, no new column, no data rewrite, every existing node type stays
allowed, and the table's other constraints -- in particular
``ck_workflow_nodes_repair_loop_requires_max_iterations`` -- are preserved.

SQLite cannot ALTER a CHECK constraint in place, so (exactly as for
dc6427b0a95a, which widened ``job_queue.job_type``) this uses batch mode, i.e.
a table rebuild. Unlike ``job_queue``, ``workflow_nodes`` is a PARENT table:
``workflow_edges`` and ``workflow_node_runs`` reference it, and the edges
reference it ``ON DELETE CASCADE``. Alembic's env.py runs migrations with
``PRAGMA foreign_keys=ON``, and dropping the old table under that pragma
performs an implicit DELETE that fires those cascades -- i.e. it would silently
delete every workflow edge. So the rebuild runs with foreign-key enforcement
paused (SQLite's documented procedure for this), and ``PRAGMA
foreign_key_check`` then proves no child row was orphaned before the migration
is allowed to finish. (The pragma only takes effect outside a transaction, so
any transaction an earlier migration in the same run left open is committed
first; a failure here therefore leaves the database at the previous, valid
revision.)

The downgrade refuses -- loudly, changing nothing -- while any EVALUATION node
exists, rather than deleting workflow definitions to satisfy the narrower
CHECK.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7f4a2d9e815"
down_revision: Union[str, None] = "a3c9e17b5d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = (
    "agent",
    "parallel_group",
    "conditional",
    "repair_loop",
    "judge",
    "consensus",
    "human_approval",
    "terminal",
)
_NEW_VALUES = _OLD_VALUES + ("evaluation",)


def _rebuild_with_node_types(values: Sequence[str], existing_values: Sequence[str]) -> None:
    bind = op.get_bind()
    raw = bind.connection.dbapi_connection
    if raw.in_transaction:
        raw.commit()  # PRAGMA foreign_keys is a no-op inside a transaction
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("workflow_nodes", schema=None) as batch_op:
            batch_op.alter_column(
                "node_type",
                existing_type=sa.Enum(
                    *existing_values, name="workflownodetype", native_enum=False, create_constraint=True
                ),
                type_=sa.Enum(*values, name="workflownodetype", native_enum=False, create_constraint=True),
                existing_nullable=False,
            )
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"workflow_nodes rebuild left {len(violations)} foreign-key violation(s); refusing to continue"
            )
    finally:
        if not raw.in_transaction:
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    _rebuild_with_node_types(_NEW_VALUES, _OLD_VALUES)


def downgrade() -> None:
    bind = op.get_bind()
    remaining = bind.exec_driver_sql("SELECT COUNT(*) FROM workflow_nodes WHERE node_type = 'evaluation'").scalar()
    if remaining:
        raise RuntimeError(
            f"cannot downgrade: {remaining} EVALUATION workflow node(s) exist; delete or replace them first"
        )
    _rebuild_with_node_types(_OLD_VALUES, _NEW_VALUES)
