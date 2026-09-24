"""ail4b_review_attempts

Revision ID: ail4b_review_attempts
Revises: ail3c_experiment_conclusion
Create Date: 2026-09-25 09:00:00

AIL.4B: review attempts and weekly review-prompt deliveries for the review &
retention loop.

Purely additive: two new tables, ``review_attempts`` and
``review_prompt_deliveries``, and their indexes. No existing table is touched,
no column is added to one, and no CHECK constraint is rebuilt (unlike AIL.3C),
so the upgrade needs no batch table rebuild. Both tables belong to this one
revision, so the chain stays a single line: ... -> ail3c_experiment_conclusion
-> ail4b_review_attempts.

- Lifecycle CHECK: STARTED (no completion, no evidence) / PASSED (completion
  AND evidence required) / FAILED (completion, never evidence).
- Partial UNIQUE ``(user_id, concept_id) WHERE status = 'started'``: at most
  one in-progress review per learner and Concept.
- Partial UNIQUE on ``resulting_learning_evidence_id``: one evidence row can
  result from at most one attempt.
- ``review_prompt_deliveries``: the persistence behind "at most two review
  prompts per week". ``slot`` is CHECKed to 1 or 2 and UNIQUE per (user, week),
  so a third delivery in a week cannot exist even under concurrent allocation;
  UNIQUE (user, concept, week) means one logical prompt is never counted twice.

The downgrade refuses, changing nothing, while any review attempt or prompt
delivery exists, rather than silently discarding a learner's review history.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "ail4b_review_attempts"
down_revision: Union[str, None] = "ail3c_experiment_conclusion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIVE_INDEX = "uq_review_attempts_active"
_EVIDENCE_INDEX = "uq_review_attempts_resulting_evidence"
_LOOKUP_INDEX = "ix_review_attempts_user_concept_started"


def upgrade() -> None:
    op.create_table(
        "review_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("concept_id", sa.String(length=36), nullable=False),
        sa.Column("concept_version_id", sa.String(length=36), nullable=False),
        sa.Column("learning_item_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status",
            sa.Enum("started", "passed", "failed", name="reviewattemptstatus", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("resulting_learning_evidence_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["concept_version_id"], ["concept_versions.id"]),
        sa.ForeignKeyConstraint(["learning_item_id"], ["learning_items.id"]),
        sa.ForeignKeyConstraint(["resulting_learning_evidence_id"], ["learning_evidence.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(status = 'started' AND completed_at IS NULL AND resulting_learning_evidence_id IS NULL) OR "
            "(status = 'passed' AND completed_at IS NOT NULL AND resulting_learning_evidence_id IS NOT NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND resulting_learning_evidence_id IS NULL)",
            name="ck_review_attempts_lifecycle",
        ),
    )
    op.create_index(_LOOKUP_INDEX, "review_attempts", ["user_id", "concept_id", "started_at"])
    op.create_index(
        _ACTIVE_INDEX,
        "review_attempts",
        ["user_id", "concept_id"],
        unique=True,
        sqlite_where=sa.text("status = 'started'"),
        postgresql_where=sa.text("status = 'started'"),
    )
    op.create_index(
        _EVIDENCE_INDEX,
        "review_attempts",
        ["resulting_learning_evidence_id"],
        unique=True,
        sqlite_where=sa.text("resulting_learning_evidence_id IS NOT NULL"),
        postgresql_where=sa.text("resulting_learning_evidence_id IS NOT NULL"),
    )

    op.create_table(
        "review_prompt_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("concept_id", sa.String(length=36), nullable=False),
        sa.Column("week_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("prompt_kind", sa.String(length=40), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("slot IN (1, 2)", name="ck_review_prompt_deliveries_slot"),
        sa.UniqueConstraint("user_id", "week_start", "slot", name="uq_review_prompt_deliveries_user_week_slot"),
        sa.UniqueConstraint(
            "user_id", "concept_id", "week_start", name="uq_review_prompt_deliveries_user_concept_week"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    remaining = bind.exec_driver_sql("SELECT COUNT(*) FROM review_attempts").scalar()
    if remaining:
        raise RuntimeError(
            f"cannot downgrade: {remaining} review attempt(s) exist; "
            "a learner's review history is never discarded by a downgrade"
        )
    deliveries = bind.exec_driver_sql("SELECT COUNT(*) FROM review_prompt_deliveries").scalar()
    if deliveries:
        raise RuntimeError(
            f"cannot downgrade: {deliveries} review prompt delivery record(s) exist; "
            "they are never discarded by a downgrade"
        )
    op.drop_table("review_prompt_deliveries")
    op.drop_index(_EVIDENCE_INDEX, table_name="review_attempts")
    op.drop_index(_ACTIVE_INDEX, table_name="review_attempts")
    op.drop_index(_LOOKUP_INDEX, table_name="review_attempts")
    op.drop_table("review_attempts")
