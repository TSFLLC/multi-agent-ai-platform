"""AIL.2B deterministic Radar intelligence backend.

Revision ID: ail2b_radar_intelligence
Revises: ail_wave1_integration
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ail2b_radar_intelligence"
down_revision: Union[str, None] = "ail_wave1_integration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "development_terms",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("development_id", sa.String(length=36), nullable=False),
        sa.Column("term_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", _enum("rule", "agent", "user", name="claimcreationmethod"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["development_id"], ["developments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["term_id"], ["taxonomy_terms.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("development_id", "term_id", name="uq_development_terms_pair"),
    )
    op.create_index("ix_development_terms_development_id", "development_terms", ["development_id"])
    op.create_index("ix_development_terms_term_id", "development_terms", ["term_id"])

    op.create_table(
        "attention_samples",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("development_id", sa.String(length=36), nullable=True),
        sa.Column("model_id", sa.String(length=36), nullable=True),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("metric", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Numeric(20, 8), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=True),
        sa.Column("measurement_metadata_json", sa.JSON(), nullable=True),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("external_identity", sa.String(length=500), nullable=True),
        sa.Column("sample_hash", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "((development_id IS NOT NULL) + (model_id IS NOT NULL)) = 1",
            name="attention_sample_exactly_one_subject",
        ),
        sa.ForeignKeyConstraint(["development_id"], ["developments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["radar_sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attention_samples_development_sampled", "attention_samples", ["development_id", "sampled_at"])
    op.create_index("ix_attention_samples_model_sampled", "attention_samples", ["model_id", "sampled_at"])
    op.create_index(
        "uq_attention_samples_development_identity",
        "attention_samples",
        ["development_id", "source_id", "metric", "sampled_at"],
        unique=True,
        sqlite_where=sa.text("development_id IS NOT NULL"),
    )
    op.create_index(
        "uq_attention_samples_model_identity",
        "attention_samples",
        ["model_id", "source_id", "metric", "sampled_at"],
        unique=True,
        sqlite_where=sa.text("model_id IS NOT NULL"),
    )

    op.create_table(
        "triage_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("development_id", sa.String(length=36), nullable=True),
        sa.Column("model_id", sa.String(length=36), nullable=True),
        sa.Column("decision", _enum("IGNORE", "WATCH", "LEARN", "EXPERIMENT", "INVESTIGATE", name="triagedecisionkind"), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("revisit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revisit_condition_json", sa.JSON(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_by_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            "((development_id IS NOT NULL) + (model_id IS NOT NULL)) = 1",
            name="triage_decision_exactly_one_target",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["development_id"], ["developments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["triage_decisions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_triage_decisions_user_decided", "triage_decisions", ["user_id", "decided_at"])
    op.create_index(
        "uq_triage_decisions_active_development",
        "triage_decisions",
        ["user_id", "development_id"],
        unique=True,
        sqlite_where=sa.text("development_id IS NOT NULL AND superseded_by_id IS NULL"),
    )
    op.create_index(
        "uq_triage_decisions_active_model",
        "triage_decisions",
        ["user_id", "model_id"],
        unique=True,
        sqlite_where=sa.text("model_id IS NOT NULL AND superseded_by_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_triage_decisions_active_model", table_name="triage_decisions")
    op.drop_index("uq_triage_decisions_active_development", table_name="triage_decisions")
    op.drop_index("ix_triage_decisions_user_decided", table_name="triage_decisions")
    op.drop_table("triage_decisions")
    op.drop_index("uq_attention_samples_model_identity", table_name="attention_samples")
    op.drop_index("uq_attention_samples_development_identity", table_name="attention_samples")
    op.drop_index("ix_attention_samples_model_sampled", table_name="attention_samples")
    op.drop_index("ix_attention_samples_development_sampled", table_name="attention_samples")
    op.drop_table("attention_samples")
    op.drop_index("ix_development_terms_term_id", table_name="development_terms")
    op.drop_index("ix_development_terms_development_id", table_name="development_terms")
    op.drop_table("development_terms")
