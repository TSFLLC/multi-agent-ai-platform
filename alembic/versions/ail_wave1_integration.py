"""AIL Wave-1 shared project and Radar/Concept contracts.

Revision ID: ail_wave1_integration
Revises: ail2a_radar_ledger
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ail_wave1_integration"
down_revision: Union[str, None] = "ail2a_radar_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.Enum(
                    "standard",
                    "system_ail",
                    name="projectkind",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default="standard",
            )
        )
        batch_op.add_column(
            sa.Column(
                "ail_evidence_opt_in",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

    op.execute(
        sa.text(
            "UPDATE projects SET kind = 'standard' WHERE kind IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE projects SET ail_evidence_opt_in = 0 "
            "WHERE ail_evidence_opt_in IS NULL"
        )
    )

    op.create_table(
        "development_concepts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "development_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column(
            "concept_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "proposed",
                "confirmed",
                "rejected",
                name="developmentconceptstate",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default="proposed",
        ),
        sa.Column(
            "proposed_by",
            sa.Enum(
                "rule",
                "agent",
                "user",
                name="developmentconceptproposedby",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["development_id"],
            ["developments.id"],
            name=op.f("fk_development_concepts_development_id_developments"),
        ),
        sa.ForeignKeyConstraint(
            ["concept_id"],
            ["concepts.id"],
            name=op.f("fk_development_concepts_concept_id_concepts"),
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name=op.f("fk_development_concepts_reviewed_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_development_concepts")),
        sa.UniqueConstraint(
            "development_id",
            "concept_id",
            name="uq_development_concepts_pair",
        ),
    )
    op.create_index(
        "ix_development_concepts_development_state",
        "development_concepts",
        ["development_id", "state"],
    )
    op.create_index(
        "ix_development_concepts_concept_id",
        "development_concepts",
        ["concept_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_development_concepts_concept_id",
        table_name="development_concepts",
    )
    op.drop_index(
        "ix_development_concepts_development_state",
        table_name="development_concepts",
    )
    op.drop_table("development_concepts")

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys=OFF")
        op.create_table(
            "_projects_wave1_downgrade",
            sa.Column("org_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("policy_settings_json", sa.JSON(), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["org_id"],
                ["organizations.id"],
                name="fk_projects_org_id_organizations",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_projects"),
        )
        op.execute(
            sa.text(
                "INSERT INTO _projects_wave1_downgrade "
                "(org_id, name, policy_settings_json, id, created_at) "
                "SELECT org_id, name, policy_settings_json, id, created_at FROM projects"
            )
        )
        op.drop_table("projects")
        op.rename_table("_projects_wave1_downgrade", "projects")
        op.execute("PRAGMA foreign_keys=ON")
    else:
        with op.batch_alter_table("projects", schema=None) as batch_op:
            batch_op.drop_column("ail_evidence_opt_in")
            batch_op.drop_column("kind")