"""AIL Wave-1 shared project and Radar/Concept contracts.

Revision ID: ail_wave1_integration
Revises: ail2a_radar_ledger
"""

from typing import Sequence, Tuple, Union

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op


revision: str = "ail_wave1_integration"
down_revision: Union[str, None] = "ail2a_radar_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PROJECTS_TABLE = "projects"
_TEMP_PROJECTS_TABLE = "_alembic_tmp_projects"
_OLD_PROJECT_COLUMNS = (
    "org_id",
    "name",
    "policy_settings_json",
    "id",
    "created_at",
)
_NEW_PROJECT_COLUMNS = _OLD_PROJECT_COLUMNS + ("kind", "ail_evidence_opt_in")
_PROJECT_KIND_CHECK = "kind IN ('standard', 'system_ail')"


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


def _cleanup_known_projects_orphan(bind: Connection) -> None:
    if not _table_exists(bind, _TEMP_PROJECTS_TABLE):
        return

    if not _table_exists(bind, _PROJECTS_TABLE):
        raise RuntimeError(
            "Refusing Wave-1 recovery: projects is absent while "
            "_alembic_tmp_projects exists"
        )

    if _table_columns(bind, _PROJECTS_TABLE) != _OLD_PROJECT_COLUMNS:
        raise RuntimeError(
            "Refusing Wave-1 recovery: projects has an unexpected schema "
            f"({_table_columns(bind, _PROJECTS_TABLE)!r})"
        )
    if _table_columns(bind, _TEMP_PROJECTS_TABLE) != _NEW_PROJECT_COLUMNS:
        raise RuntimeError(
            "Refusing Wave-1 recovery: _alembic_tmp_projects has an unexpected schema "
            f"({_table_columns(bind, _TEMP_PROJECTS_TABLE)!r})"
        )

    temp_count = bind.execute(
        sa.text(f'SELECT count(*) FROM "{_TEMP_PROJECTS_TABLE}"')
    ).scalar_one()
    if temp_count != 0:
        raise RuntimeError(
            "Refusing Wave-1 recovery: _alembic_tmp_projects "
            f"contains {temp_count} row(s)"
        )

    temp_sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": _TEMP_PROJECTS_TABLE},
    ).scalar_one_or_none()
    normalized_sql = " ".join((temp_sql or "").lower().split())
    if _PROJECT_KIND_CHECK.lower() not in normalized_sql or "ail_evidence_opt_in boolean" not in normalized_sql:
        raise RuntimeError(
            "Refusing Wave-1 recovery: _alembic_tmp_projects does not match "
            "the expected project-column constraints"
        )

    bind.execute(sa.text(f'DROP TABLE "{_TEMP_PROJECTS_TABLE}"'))


def _upgrade_projects_sqlite(bind: Connection) -> None:
    if not _table_exists(bind, _PROJECTS_TABLE):
        raise RuntimeError("Cannot apply Wave-1: projects does not exist")
    if _table_columns(bind, _PROJECTS_TABLE) != _OLD_PROJECT_COLUMNS:
        raise RuntimeError(
            "Cannot apply Wave-1: projects is neither the pre-migration schema "
            "nor a known recoverable state"
        )

    _cleanup_known_projects_orphan(bind)
    bind.execute(
        sa.text(
            'ALTER TABLE "projects" ADD COLUMN kind VARCHAR(10) '
            "NOT NULL DEFAULT 'standard' CHECK (kind IN ('standard', 'system_ail'))"
        )
    )
    bind.execute(
        sa.text(
            'ALTER TABLE "projects" ADD COLUMN ail_evidence_opt_in BOOLEAN '
            "NOT NULL DEFAULT 0"
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _upgrade_projects_sqlite(bind)
    else:
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
        if _table_columns(bind, _PROJECTS_TABLE) != _NEW_PROJECT_COLUMNS:
            raise RuntimeError("Refusing Wave-1 downgrade: unexpected projects schema")
        bind.execute(sa.text('ALTER TABLE "projects" DROP COLUMN ail_evidence_opt_in'))
        bind.execute(sa.text('ALTER TABLE "projects" DROP COLUMN kind'))
    else:
        with op.batch_alter_table("projects", schema=None) as batch_op:
            batch_op.drop_column("ail_evidence_opt_in")
            batch_op.drop_column("kind")
