"""AIL.2A Radar evidence ledger foundation.

Concept links are intentionally deferred until the AIL.1 concepts migration
is integrated; this migration does not create a competing concepts table.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ail2a_radar_ledger"
down_revision: Union[str, None] = "0f87701fabff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(*values: str, name: str):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "radar_sources",
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source_class", _enum("s1", "s2", "s3", "s4", "s5", "s6", "s7", name="radarsourceclass"), nullable=False),
        sa.Column("endpoint_url", sa.String(1000), nullable=True),
        sa.Column("independence_group", sa.String(120), nullable=False),
        sa.Column("fetch_method", sa.String(40), nullable=False),
        sa.Column("cadence_minutes", sa.Integer(), nullable=True),
        sa.Column("state", _enum("candidate", "approved", "active", "paused", "retired", name="radarsourcestate"), nullable=False),
        sa.Column("tos_note", sa.Text(), nullable=True),
        sa.Column("owner_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tos_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("credential_ref", sa.String(500), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_radar_sources")),
        sa.UniqueConstraint("name", name="uq_radar_sources_name"),
    )
    op.create_table(
        "developments",
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("development_type", sa.String(80), nullable=False),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidate_key", sa.String(500), nullable=False),
        sa.Column("status", _enum("active", "merged", "archived", name="developmentstatus"), nullable=False),
        sa.Column("merged_into_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["merged_into_id"], ["developments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_developments")),
        sa.UniqueConstraint("candidate_key", name="uq_developments_candidate_key"),
    )
    op.create_table(
        "radar_items",
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("development_id", sa.String(36), nullable=True),
        sa.Column("external_identity", sa.String(1000), nullable=True),
        sa.Column("canonical_url", sa.String(2000), nullable=True),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=False),
        sa.Column("storage_ref", sa.String(1000), nullable=True),
        sa.Column("processing_state", _enum("received", "processed", "failed", "rejected", name="radaritemstate"), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["development_id"], ["developments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_id"], ["radar_sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_radar_items")),
        sa.UniqueConstraint("source_id", "external_identity", name="uq_radar_items_source_external_identity"),
        sa.UniqueConstraint("source_id", "content_hash", name="uq_radar_items_source_hash"),
    )
    op.create_table(
        "development_models",
        sa.Column("development_id", sa.String(36), nullable=False),
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["development_id"], ["developments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_development_models")),
        sa.UniqueConstraint("development_id", "model_id", name="uq_development_models_pair"),
    )
    op.create_table(
        "claims",
        sa.Column("claim_type", _enum("FACT", "PROVIDER_CLAIM", "RESEARCH_RESULT", "BENCHMARK_RESULT", "COMMUNITY_SIGNAL", "PLATFORM_OBSERVATION", "AI_EXPLANATION", name="claimtype"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("development_id", sa.String(36), nullable=True),
        sa.Column("model_id", sa.String(36), nullable=True),
        sa.Column("quote_span", sa.Text(), nullable=True),
        sa.Column("conditions_json", sa.JSON(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", _enum("rule", "agent", "user", name="claimcreationmethod"), nullable=False),
        sa.Column("status", _enum("active", "superseded", "disputed", name="claimstatus"), nullable=False),
        sa.Column("superseded_by_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint("development_id IS NOT NULL OR model_id IS NOT NULL", name="ck_claims_claim_subject_required"),
        sa.ForeignKeyConstraint(["development_id"], ["developments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["claims.id"]),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
    )
    op.create_table(
        "claim_origins",
        sa.Column("claim_id", sa.String(36), nullable=False),
        sa.Column("origin_kind", _enum("source_item", "platform_evaluation", "ail_agent_run", name="claimoriginkind"), nullable=False),
        sa.Column("source_item_id", sa.String(36), nullable=True),
        sa.Column("evaluation_id", sa.String(36), nullable=True),
        sa.Column("agent_run_id", sa.String(36), nullable=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint("((source_item_id IS NOT NULL) + (evaluation_id IS NOT NULL) + (agent_run_id IS NOT NULL)) = 1", name="ck_claim_origins_claim_origin_exactly_one_ref"),
        sa.CheckConstraint("(origin_kind = 'source_item' AND source_item_id IS NOT NULL AND evaluation_id IS NULL AND agent_run_id IS NULL) OR (origin_kind = 'platform_evaluation' AND evaluation_id IS NOT NULL AND source_item_id IS NULL AND agent_run_id IS NULL) OR (origin_kind = 'ail_agent_run' AND agent_run_id IS NOT NULL AND source_item_id IS NULL AND evaluation_id IS NULL)", name="ck_claim_origins_claim_origin_kind_matches_ref"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["evaluation_id"], ["evaluations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_item_id"], ["radar_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_origins")),
        sa.UniqueConstraint("claim_id", name="uq_claim_origins_claim_id"),
    )
    op.create_table(
        "claim_citations",
        sa.Column("explanation_claim_id", sa.String(36), nullable=False),
        sa.Column("cited_claim_id", sa.String(36), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint("explanation_claim_id <> cited_claim_id", name="ck_claim_citations_claim_citation_not_self"),
        sa.ForeignKeyConstraint(["cited_claim_id"], ["claims.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["explanation_claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_citations")),
        sa.UniqueConstraint("explanation_claim_id", "cited_claim_id", name="uq_claim_citations_pair"),
    )


def downgrade() -> None:
    op.drop_table("claim_citations")
    op.drop_table("claim_origins")
    op.drop_table("claims")
    op.drop_table("development_models")
    op.drop_table("radar_items")
    op.drop_table("developments")
    op.drop_table("radar_sources")
