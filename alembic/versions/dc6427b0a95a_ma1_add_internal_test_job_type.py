"""ma1 add internal_test job type

Revision ID: dc6427b0a95a
Revises: f85ec3fb33ae
Create Date: 2026-09-16 14:34:34.358925

Widens job_queue.job_type's CHECK constraint to additionally allow
'internal_test' (app.db.enums.JobType.INTERNAL_TEST) — a harmless,
side-effect-free job type used only to prove the MA1 worker's
claim/heartbeat/fencing/complete lifecycle before real Agent execution
exists (MA3). Purely additive: no existing row's job_type value is
affected, no other table/column changes.

Alembic's autogenerate does not diff CHECK-constraint text embedded in
Enum(native_enum=False, create_constraint=True) columns, so this is
hand-written using SQLite's batch/table-rebuild mode (SQLite has no
ALTER TABLE ... DROP/ADD CONSTRAINT).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dc6427b0a95a"
down_revision: Union[str, None] = "f85ec3fb33ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = ("agent_run", "workflow_node", "evaluation")
_NEW_VALUES = ("agent_run", "workflow_node", "evaluation", "internal_test")


def upgrade() -> None:
    with op.batch_alter_table("job_queue", schema=None) as batch_op:
        batch_op.alter_column(
            "job_type",
            existing_type=sa.Enum(*_OLD_VALUES, name="jobtype", native_enum=False, create_constraint=True),
            type_=sa.Enum(*_NEW_VALUES, name="jobtype", native_enum=False, create_constraint=True),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("job_queue", schema=None) as batch_op:
        batch_op.alter_column(
            "job_type",
            existing_type=sa.Enum(*_NEW_VALUES, name="jobtype", native_enum=False, create_constraint=True),
            type_=sa.Enum(*_OLD_VALUES, name="jobtype", native_enum=False, create_constraint=True),
            existing_nullable=False,
        )
