"""AIL.5D.3: learner step responses.

Adds ONE table, ``academy_step_responses``: append-only, user-scoped learner responses (text, a structured answer, a
prediction, an observation, a reflection, an outline) to the authored steps a Learning Item carries in
``spec_json.steps``. Nothing else changes: no column is added to or removed from any existing table, no existing row is
read or rewritten, and no evidence, assessment, Professor or Personal Lab table is touched.

Sits on top of ``academy_step_progress`` (AIL.5D.1). Unrelated to the historical ``ail5d_learning_item_lineage``
revision, which is neither renamed nor modified.

Reversible and safe by default: ``downgrade`` drops only what this revision created and REFUSES (changing nothing) while
any learner response exists, because that is learner data. The platform's rollback mechanism remains restoring the
pre-migration backup (docs/deployment/migration-rollback-policy.md).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "academy_step_response"
down_revision: Union[str, None] = "academy_step_progress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "academy_step_responses"
INDEX = "ix_academy_step_responses_user_lineage_step"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in sa.inspect(bind).get_table_names():
        raise RuntimeError(f"Cannot create {TABLE}: it already exists outside this migration")
    op.create_table(
        TABLE,
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("learning_item_id", sa.String(length=36), nullable=False),
        sa.Column("lineage_id", sa.String(length=36), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column("step_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("answer", "prediction", "observation", "reflection", "outline", "note", name="academystepresponsekind", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("response_key", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("ref_type", sa.String(length=30), nullable=True),
        sa.Column("ref_id", sa.String(length=36), nullable=True),
        sa.Column("assistance_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_academy_step_responses_revision_positive")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["learning_item_id"], ["learning_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_academy_step_responses")),
        sa.UniqueConstraint("user_id", "learning_item_id", "step_key", "kind", "response_key", "revision", name="uq_academy_step_responses_revision"),
    )
    op.create_index(INDEX, TABLE, ["user_id", "lineage_id", "step_key"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    rows = bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one()
    if rows:
        raise RuntimeError(
            f"Cannot drop {TABLE}: {rows} learner response row(s) exist. "
            "Restore the pre-migration backup instead (docs/deployment/migration-rollback-policy.md)."
        )
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
