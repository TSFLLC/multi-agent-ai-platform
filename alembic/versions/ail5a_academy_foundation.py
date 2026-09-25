"""AIL.5A Academy Foundation.

Adds only the durable curriculum/enrollment layer.  Content, evidence,
learner state, plans, labs, and Professor activity remain existing AIL data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ail5a_academy_foundation"
down_revision: Union[str, None] = "ail4c_professor_agent_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rebuild_plan_origin(values: Sequence[str]) -> None:
    bind = op.get_bind()
    raw = bind.connection.dbapi_connection
    if raw.in_transaction:
        raw.commit()
    raw.execute("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("learning_plan_items", schema=None) as batch:
            batch.alter_column(
                "origin",
                existing_type=sa.Enum("planner", "user", "professor", name="planitemorigin", native_enum=False),
                type_=sa.Enum(*values, name="planitemorigin", native_enum=False, create_constraint=True),
                existing_nullable=False,
            )
    finally:
        raw.execute("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    _rebuild_plan_origin(("planner", "user", "professor", "program"))

    op.create_table(
        "programs",
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("author_user_id", sa.String(length=36), nullable=False),
        sa.Column("target_track_term_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_track_term_id"], ["taxonomy_terms.id"]),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_programs")),
        sa.UniqueConstraint("slug", name="uq_programs_slug"),
    )
    op.create_table(
        "program_versions",
        sa.Column("program_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("draft", "published", "retired", name="academyprogramversionstatus", native_enum=False, create_constraint=True), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("completion_rules_json", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["program_id"], ["programs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_program_versions")),
        sa.UniqueConstraint("program_id", "version", name="uq_program_versions_program_id_version"),
    )
    op.create_table(
        "program_items",
        sa.Column("program_version_id", sa.String(length=36), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("module_key", sa.String(length=120), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("item_kind", sa.Enum("concept", "learning_item", "checkpoint", name="academyprogramitemkind", native_enum=False, create_constraint=True), nullable=False),
        sa.Column("concept_id", sa.String(length=36), nullable=True),
        sa.Column("learning_item_id", sa.String(length=36), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("estimated_minutes", sa.Integer(), nullable=True),
        sa.Column("purpose_text", sa.Text(), nullable=True),
        sa.Column("completion_requirement_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("concept_id IS NOT NULL OR learning_item_id IS NOT NULL", name="ck_program_items_has_target"),
        sa.ForeignKeyConstraint(["program_version_id"], ["program_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["learning_item_id"], ["learning_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_program_items")),
        sa.UniqueConstraint("program_version_id", "day", "position", name="uq_program_items_version_day_position"),
    )
    op.create_table(
        "enrollments",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("program_version_id", sa.String(length=36), nullable=False),
        sa.Column("pace", sa.Enum("scheduled", "self_paced", name="academypace", native_enum=False, create_constraint=True), nullable=False),
        sa.Column("status", sa.Enum("active", "paused", "completed", "withdrawn", name="academyenrollmentstatus", native_enum=False, create_constraint=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["program_version_id"], ["program_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_enrollments")),
        sa.UniqueConstraint("user_id", "program_version_id", name="uq_enrollments_user_id_program_version_id"),
    )
    op.create_index("ix_enrollments_user_id", "enrollments", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_enrollments_user_id", table_name="enrollments")
    op.drop_table("enrollments")
    op.drop_table("program_items")
    op.drop_table("program_versions")
    op.drop_table("programs")
    _rebuild_plan_origin(("planner", "user", "professor"))
