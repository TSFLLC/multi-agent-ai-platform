"""AIL.5D.4: step-scoped Professor help tracking.

Adds ONE table, ``academy_professor_help``: an append-only audit of step-scoped AI Professor help a learner actually
received (mode, request, level, the H0-H5 assistance level, whether authored solution material was in the context, and a
fingerprint of the exact context). It is what a later guided/independent practice decision reads; it is never evidence.
Nothing else changes: no existing table, row, index or trigger is touched, and no evidence, assessment, Personal Lab or
Professor table is altered.

Sits on top of ``academy_step_response`` (AIL.5D.3). Unrelated to the historical ``ail5d_learning_item_lineage``.

Reversible and safe by default: ``downgrade`` drops only what this revision created and REFUSES (changing nothing) while
any help row exists, because that is learner data (docs/deployment/migration-rollback-policy.md).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "academy_professor_help"
down_revision: Union[str, None] = "academy_step_response"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "academy_professor_help"
INDEX = "ix_academy_professor_help_user_lineage_step"


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
        sa.Column("mode", sa.Enum("teaching", "guided", "independent", name="professormode", native_enum=False, create_constraint=True), nullable=False),
        sa.Column("request_kind", sa.Enum("ask", "hint", name="helprequest", native_enum=False, create_constraint=True), nullable=False),
        sa.Column("help_level", sa.Enum("clarify", "hint", "stronger_hint", "explain", name="helplevel", native_enum=False, create_constraint=True), nullable=False),
        sa.Column("assistance_level", sa.Enum("h0", "h1", "h2", "h3", "h4", "h5", name="assistancelevel", native_enum=False, create_constraint=True), nullable=True),
        sa.Column("revealed_solution", sa.Boolean(), nullable=False),
        sa.Column("interaction_id", sa.String(length=36), nullable=False),
        sa.Column("context_sha256", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["learning_item_id"], ["learning_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_academy_professor_help")),
        sa.UniqueConstraint("interaction_id", name="uq_academy_professor_help_interaction"),
    )
    op.create_index(INDEX, TABLE, ["user_id", "lineage_id", "step_key"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    rows = bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one()
    if rows:
        raise RuntimeError(
            f"Cannot drop {TABLE}: {rows} Professor help row(s) exist. "
            "Restore the pre-migration backup instead (docs/deployment/migration-rollback-policy.md)."
        )
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
