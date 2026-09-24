"""AIL.4C: add the truthful Professor Agent Run role.

Only the existing ``agent_runs.role`` CHECK constraint changes. SQLite needs
the established batch-table rebuild because CHECK constraints cannot be
altered in place.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "ail4c_professor_agent_role"
down_revision: Union[str, None] = "ail4b_review_attempts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _role_checks(bind):
    return {constraint["name"] for constraint in sa.inspect(bind).get_check_constraints("agent_runs")}


def _role_enum(*values):
    return sa.Enum(*values, name="agentrunrole", native_enum=False, create_constraint=True)


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite batch DDL is non-transactional. If a hosted process is killed
    # after creating Alembic's temporary table but before the rename, a retry
    # must remove only that known intermediate object. The canonical table
    # must still exist; otherwise refuse to guess or discard data.
    table_names = set(sa.inspect(bind).get_table_names())
    if "_alembic_tmp_agent_runs" in table_names:
        if "agent_runs" not in table_names:
            raise RuntimeError(
                "Cannot recover agent_runs migration: canonical agent_runs table is missing."
            )
        op.drop_table("_alembic_tmp_agent_runs")
    existing = _role_checks(bind)
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        if "agentrunrole" in existing:
            batch_op.drop_constraint(batch_op.f("agentrunrole"), type_="check")
        if "ck_agent_runs_agentrunrole" in existing:
            batch_op.drop_constraint(batch_op.f("ck_agent_runs_agentrunrole"), type_="check")
        batch_op.alter_column(
            "role",
            existing_type=sa.VARCHAR(length=9),
            type_=_role_enum("primary", "reviewer", "repair", "evaluator", "professor"),
            existing_nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    professor_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM agent_runs WHERE role = 'professor'")
    ).scalar_one()
    if professor_count:
        raise RuntimeError(
            "Cannot downgrade AIL.4C Professor Agent Run role while "
            f"{professor_count} Professor AgentRun row(s) exist."
        )

    existing = _role_checks(bind)
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        if "agentrunrole" in existing:
            batch_op.drop_constraint(batch_op.f("agentrunrole"), type_="check")
        if "ck_agent_runs_agentrunrole" in existing:
            batch_op.drop_constraint(batch_op.f("ck_agent_runs_agentrunrole"), type_="check")
        batch_op.alter_column(
            "role",
            existing_type=_role_enum("primary", "reviewer", "repair", "evaluator", "professor"),
            type_=_role_enum("primary", "reviewer", "repair", "evaluator"),
            existing_nullable=True,
        )
