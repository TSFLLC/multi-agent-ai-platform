"""AIL.5B product experience records and retryable milestone attempts."""
from alembic import op
import sqlalchemy as sa

revision = "c31b7f4a1a5e"
down_revision = "97c6dbe9ae3c"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("milestone_attempts") as batch:
        batch.drop_constraint("uq_milestone_attempts", type_="unique")
        batch.create_index("ix_milestone_attempts_project_milestone", ["project_attempt_id", "project_milestone_id"])
    op.create_table(
        "project_experiment_links",
        sa.Column("project_attempt_id", sa.String(36), nullable=False),
        sa.Column("milestone_attempt_id", sa.String(36), nullable=True),
        sa.Column("experiment_id", sa.String(36), nullable=False),
        sa.Column("project_question", sa.Text(), nullable=True),
        sa.Column("learner_decision", sa.Text(), nullable=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_attempt_id"], ["project_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["milestone_attempt_id"], ["milestone_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("milestone_attempt_id", "experiment_id", name="uq_project_experiment_link"),
    )
    op.create_table(
        "explain_back_responses",
        sa.Column("user_id", sa.String(36), nullable=False), sa.Column("project_attempt_id", sa.String(36), nullable=False),
        sa.Column("milestone_attempt_id", sa.String(36), nullable=False), sa.Column("concept_id", sa.String(36), nullable=True),
        sa.Column("question", sa.Text(), nullable=False), sa.Column("response", sa.Text(), nullable=False),
        sa.Column("assistance_level", sa.Enum("h0", "h1", "h2", "h3", "h4", "h5", name="assistancelevel", native_enum=False), nullable=True),
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["project_attempt_id"], ["project_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["milestone_attempt_id"], ["milestone_attempts.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_explain_back_responses_user_id", "explain_back_responses", ["user_id"])
    op.create_table(
        "candidate_evidence",
        sa.Column("user_id", sa.String(36), nullable=False), sa.Column("project_attempt_id", sa.String(36), nullable=False), sa.Column("milestone_attempt_id", sa.String(36), nullable=True),
        sa.Column("concept_id", sa.String(36), nullable=True), sa.Column("source_type", sa.String(50), nullable=False), sa.Column("source_id", sa.String(36), nullable=True),
        sa.Column("evidence_type", sa.String(50), nullable=False), sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("assistance_level", sa.Enum("h0", "h1", "h2", "h3", "h4", "h5", name="assistancelevel", native_enum=False), nullable=True),
        sa.Column("execution_verification", sa.Enum("platform_verified", "sandbox_verified", "self_reported", "not_applicable", name="executionverification", native_enum=False), nullable=True),
        sa.Column("qualified", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("learning_evidence_id", sa.String(36), nullable=True),
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["project_attempt_id"], ["project_attempts.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["milestone_attempt_id"], ["milestone_attempts.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["learning_evidence_id"], ["learning_evidence.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_candidate_evidence_user_id", "candidate_evidence", ["user_id"])
    op.create_table(
        "assessment_ready_submissions",
        sa.Column("user_id", sa.String(36), nullable=False), sa.Column("project_attempt_id", sa.String(36), nullable=False, unique=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="ready"), sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True), sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["project_attempt_id"], ["project_attempts.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_assessment_ready_submissions_user_id", "assessment_ready_submissions", ["user_id"])


def downgrade():
    op.drop_index("ix_assessment_ready_submissions_user_id", table_name="assessment_ready_submissions")
    op.drop_table("assessment_ready_submissions")
    op.drop_index("ix_candidate_evidence_user_id", table_name="candidate_evidence")
    op.drop_table("candidate_evidence")
    op.drop_index("ix_explain_back_responses_user_id", table_name="explain_back_responses")
    op.drop_table("explain_back_responses")
    op.drop_table("project_experiment_links")
    with op.batch_alter_table("milestone_attempts") as batch:
        batch.drop_index("ix_milestone_attempts_project_milestone")
        batch.create_unique_constraint("uq_milestone_attempts", ["project_attempt_id", "project_milestone_id"])
