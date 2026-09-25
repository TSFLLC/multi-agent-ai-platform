"""ail5c_assessment_tables

Revision ID: ail5c_assessment_tables
Revises: c31b7f4a1a5e
Create Date: 2026-09-25 08:24:31

AIL.5C R1 (additive only): the five assessment tables. No existing table is
touched. The ``learning_evidence`` enum extension (R2) and the ``agent_runs``
role extension (R3) are deliberately separate revisions because each is a
SQLite table rebuild.

The downgrade refuses, changing nothing, while any assessment attempt exists:
assessment history is learner data and is never deleted by a downgrade.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ail5c_assessment_tables'
down_revision: Union[str, None] = 'c31b7f4a1a5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('assessment_definitions',
    sa.Column('definition_key', sa.String(length=160), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('draft', 'published', 'retired', name='assessmentdefinitionstatus', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('assessment_kind', sa.Enum('knowledge_check', 'explain_back', 'modification', 'reproduction', 'debugging', 'experiment_interpretation', 'project', 'capstone', name='assessmentkind', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('instructions_md', sa.Text(), nullable=False),
    sa.Column('produces_evidence_type', sa.Enum('lesson_completed', 'knowledge_check', 'observation', 'lab', 'interpretation', 'self_report', 'explain_back', 'modification', 'reproduction', 'debugging', 'project_assessment', name='evidencetype', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('project_template_id', sa.String(length=36), nullable=True),
    sa.Column('criteria_json', sa.JSON(), nullable=False),
    sa.Column('challenge_spec_json', sa.JSON(), nullable=True),
    sa.Column('independence_policy_json', sa.JSON(), nullable=False),
    sa.Column('grading_policy_json', sa.JSON(), nullable=False),
    sa.Column('allowed_resources_json', sa.JSON(), nullable=False),
    sa.Column('requires_platform_capability', sa.String(length=120), nullable=True),
    sa.Column('author_user_id', sa.String(length=36), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('content_hash', sa.String(length=64), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['author_user_id'], ['users.id'], name=op.f('fk_assessment_definitions_author_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['project_template_id'], ['project_templates.id'], name=op.f('fk_assessment_definitions_project_template_id_project_templates'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_definitions')),
    sa.UniqueConstraint('definition_key', 'version', name='uq_assessment_definitions_key_version')
    )
    with op.batch_alter_table('assessment_definitions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assessment_definitions_definition_key'), ['definition_key'], unique=False)

    op.create_table('assessment_definition_concepts',
    sa.Column('definition_id', sa.String(length=36), nullable=False),
    sa.Column('concept_id', sa.String(length=36), nullable=False),
    sa.Column('concept_version_id', sa.String(length=36), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('criterion_keys_json', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_assessment_definition_concepts_concept_id_concepts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['concept_version_id'], ['concept_versions.id'], name=op.f('fk_assessment_definition_concepts_concept_version_id_concept_versions')),
    sa.ForeignKeyConstraint(['definition_id'], ['assessment_definitions.id'], name=op.f('fk_assessment_definition_concepts_definition_id_assessment_definitions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_definition_concepts')),
    sa.UniqueConstraint('definition_id', 'concept_id', name='uq_assessment_definition_concepts_definition_concept')
    )
    op.create_table('assessment_attempts',
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('definition_id', sa.String(length=36), nullable=False),
    sa.Column('origin', sa.Enum('learner_started', 'project_submission', 'changed_knowledge', 'retry', 'human_requested', name='assessmentorigin', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('project_attempt_id', sa.String(length=36), nullable=True),
    sa.Column('source_submission_id', sa.String(length=36), nullable=True),
    sa.Column('enrollment_id', sa.String(length=36), nullable=True),
    sa.Column('previous_attempt_id', sa.String(length=36), nullable=True),
    sa.Column('status', sa.Enum('draft', 'submitted', 'checking', 'awaiting_grading', 'grading', 'finalized', 'abandoned', name='assessmentattemptstatus', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('pinned_versions_json', sa.JSON(), nullable=False),
    sa.Column('fresh_required', sa.Boolean(), nullable=False),
    sa.Column('fresh_reason', sa.String(length=60), nullable=True),
    sa.Column('challenge_seed', sa.String(length=64), nullable=True),
    sa.Column('challenge_instance_json', sa.JSON(), nullable=True),
    sa.Column('draft_json', sa.JSON(), nullable=True),
    sa.Column('submission_json', sa.JSON(), nullable=True),
    sa.Column('submission_hash', sa.String(length=64), nullable=True),
    sa.Column('input_manifest_json', sa.JSON(), nullable=True),
    sa.Column('input_manifest_hash', sa.String(length=64), nullable=True),
    sa.Column('attestation_json', sa.JSON(), nullable=True),
    sa.Column('grading_round', sa.Integer(), nullable=False),
    sa.Column('grader_agent_version_id', sa.String(length=36), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('idempotency_key', sa.String(length=120), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['definition_id'], ['assessment_definitions.id'], name=op.f('fk_assessment_attempts_definition_id_assessment_definitions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['enrollment_id'], ['enrollments.id'], name=op.f('fk_assessment_attempts_enrollment_id_enrollments'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['grader_agent_version_id'], ['agent_versions.id'], name=op.f('fk_assessment_attempts_grader_agent_version_id_agent_versions')),
    sa.ForeignKeyConstraint(['previous_attempt_id'], ['assessment_attempts.id'], name=op.f('fk_assessment_attempts_previous_attempt_id_assessment_attempts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_attempt_id'], ['project_attempts.id'], name=op.f('fk_assessment_attempts_project_attempt_id_project_attempts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_submission_id'], ['assessment_ready_submissions.id'], name=op.f('fk_assessment_attempts_source_submission_id_assessment_ready_submissions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_assessment_attempts_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_attempts'))
    )
    with op.batch_alter_table('assessment_attempts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assessment_attempts_user_id'), ['user_id'], unique=False)
        batch_op.create_index('uq_assessment_attempts_idempotency', ['user_id', 'idempotency_key'], unique=True, sqlite_where=sa.text('idempotency_key IS NOT NULL'), postgresql_where=sa.text('idempotency_key IS NOT NULL'))
        batch_op.create_index('uq_assessment_attempts_one_active', ['user_id', 'definition_id'], unique=True, sqlite_where=sa.text("status IN ('draft', 'submitted', 'checking', 'awaiting_grading', 'grading')"), postgresql_where=sa.text("status IN ('draft', 'submitted', 'checking', 'awaiting_grading', 'grading')"))

    op.create_table('assessment_results',
    sa.Column('attempt_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('round', sa.Integer(), nullable=False),
    sa.Column('result_kind', sa.Enum('deterministic', 'grader', 'final', 'human', name='assessmentresultkind', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('outcome', sa.Enum('passed', 'needs_work', 'provisional', 'human_review_required', 'unable_to_assess', name='assessmentoutcome', native_enum=False, create_constraint=True), nullable=True),
    sa.Column('demonstration_effect', sa.Enum('counts_toward_demonstrated', 'counts_toward_practiced_only', 'formative_only', 'none', name='demonstrationeffect', native_enum=False, create_constraint=True), nullable=True),
    sa.Column('criteria_json', sa.JSON(), nullable=False),
    sa.Column('facts_json', sa.JSON(), nullable=False),
    sa.Column('gaps_json', sa.JSON(), nullable=False),
    sa.Column('remediation_json', sa.JSON(), nullable=False),
    sa.Column('report_json', sa.JSON(), nullable=True),
    sa.Column('grader_agent_run_ids_json', sa.JSON(), nullable=True),
    sa.Column('grader_agent_version_id', sa.String(length=36), nullable=True),
    sa.Column('grading_contract_version', sa.String(length=40), nullable=True),
    sa.Column('review_id', sa.String(length=36), nullable=True),
    sa.Column('supersedes_result_id', sa.String(length=36), nullable=True),
    sa.Column('record_snapshot_json', sa.JSON(), nullable=True),
    sa.Column('record_hash', sa.String(length=64), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("result_kind != 'final' OR outcome IS NOT NULL", name=op.f('ck_assessment_results_final_has_outcome')),
    sa.CheckConstraint("result_kind != 'human' OR (outcome IS NOT NULL AND supersedes_result_id IS NOT NULL)", name=op.f('ck_assessment_results_human_supersedes_and_decides')),
    sa.ForeignKeyConstraint(['attempt_id'], ['assessment_attempts.id'], name=op.f('fk_assessment_results_attempt_id_assessment_attempts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['grader_agent_version_id'], ['agent_versions.id'], name=op.f('fk_assessment_results_grader_agent_version_id_agent_versions')),
    sa.ForeignKeyConstraint(['supersedes_result_id'], ['assessment_results.id'], name=op.f('fk_assessment_results_supersedes_result_id_assessment_results'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_assessment_results_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_results')),
    sa.UniqueConstraint('attempt_id', 'seq', name='uq_assessment_results_attempt_id_seq')
    )
    with op.batch_alter_table('assessment_results', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assessment_results_user_id'), ['user_id'], unique=False)
        batch_op.create_index('uq_assessment_results_final', ['attempt_id'], unique=True, sqlite_where=sa.text("result_kind = 'final' AND supersedes_result_id IS NULL"), postgresql_where=sa.text("result_kind = 'final' AND supersedes_result_id IS NULL"))
        batch_op.create_index('uq_assessment_results_stage', ['attempt_id', 'result_kind', 'round'], unique=True, sqlite_where=sa.text("result_kind IN ('deterministic', 'grader')"), postgresql_where=sa.text("result_kind IN ('deterministic', 'grader')"))
        batch_op.create_index('uq_assessment_results_supersedes', ['supersedes_result_id'], unique=True, sqlite_where=sa.text('supersedes_result_id IS NOT NULL'), postgresql_where=sa.text('supersedes_result_id IS NOT NULL'))

    op.create_table('assessment_reviews',
    sa.Column('attempt_id', sa.String(length=36), nullable=False),
    sa.Column('result_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('trigger', sa.Enum('learner_dispute', 'low_confidence', 'model_disagreement', 'capstone_exception', 'manual_correction', name='assessmentreviewtrigger', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('status', sa.Enum('open', 'resolved', 'withdrawn', name='assessmentreviewstatus', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('reason_text', sa.Text(), nullable=True),
    sa.Column('consent_shared_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reviewer_user_id', sa.String(length=36), nullable=True),
    sa.Column('decision', sa.Enum('confirm', 'override_pass', 'override_needs_work', 'new_assessment', name='assessmentreviewdecision', native_enum=False, create_constraint=True), nullable=True),
    sa.Column('decision_rationale', sa.Text(), nullable=True),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resulting_result_id', sa.String(length=36), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['attempt_id'], ['assessment_attempts.id'], name=op.f('fk_assessment_reviews_attempt_id_assessment_attempts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['result_id'], ['assessment_results.id'], name=op.f('fk_assessment_reviews_result_id_assessment_results'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['resulting_result_id'], ['assessment_results.id'], name=op.f('fk_assessment_reviews_resulting_result_id_assessment_results'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reviewer_user_id'], ['users.id'], name=op.f('fk_assessment_reviews_reviewer_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_assessment_reviews_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_reviews'))
    )
    with op.batch_alter_table('assessment_reviews', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assessment_reviews_user_id'), ['user_id'], unique=False)
        batch_op.create_index('uq_assessment_reviews_one_open', ['attempt_id'], unique=True, sqlite_where=sa.text("status = 'open'"), postgresql_where=sa.text("status = 'open'"))



def downgrade() -> None:
    bind = op.get_bind()
    remaining = bind.exec_driver_sql("SELECT COUNT(*) FROM assessment_attempts").scalar()
    if remaining:
        raise RuntimeError(
            f"cannot downgrade: {remaining} assessment attempt(s) exist; "
            "assessment history is learner data and is never deleted by a downgrade"
        )

    with op.batch_alter_table('assessment_reviews', schema=None) as batch_op:
        batch_op.drop_index('uq_assessment_reviews_one_open', sqlite_where=sa.text("status = 'open'"), postgresql_where=sa.text("status = 'open'"))
        batch_op.drop_index(batch_op.f('ix_assessment_reviews_user_id'))

    op.drop_table('assessment_reviews')
    with op.batch_alter_table('assessment_results', schema=None) as batch_op:
        batch_op.drop_index('uq_assessment_results_supersedes', sqlite_where=sa.text('supersedes_result_id IS NOT NULL'), postgresql_where=sa.text('supersedes_result_id IS NOT NULL'))
        batch_op.drop_index('uq_assessment_results_stage', sqlite_where=sa.text("result_kind IN ('deterministic', 'grader')"), postgresql_where=sa.text("result_kind IN ('deterministic', 'grader')"))
        batch_op.drop_index('uq_assessment_results_final', sqlite_where=sa.text("result_kind = 'final' AND supersedes_result_id IS NULL"), postgresql_where=sa.text("result_kind = 'final' AND supersedes_result_id IS NULL"))
        batch_op.drop_index(batch_op.f('ix_assessment_results_user_id'))

    op.drop_table('assessment_results')
    with op.batch_alter_table('assessment_attempts', schema=None) as batch_op:
        batch_op.drop_index('uq_assessment_attempts_one_active', sqlite_where=sa.text("status IN ('draft', 'submitted', 'checking', 'awaiting_grading', 'grading')"), postgresql_where=sa.text("status IN ('draft', 'submitted', 'checking', 'awaiting_grading', 'grading')"))
        batch_op.drop_index('uq_assessment_attempts_idempotency', sqlite_where=sa.text('idempotency_key IS NOT NULL'), postgresql_where=sa.text('idempotency_key IS NOT NULL'))
        batch_op.drop_index(batch_op.f('ix_assessment_attempts_user_id'))

    op.drop_table('assessment_attempts')
    op.drop_table('assessment_definition_concepts')
    with op.batch_alter_table('assessment_definitions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_assessment_definitions_definition_key'))

    op.drop_table('assessment_definitions')
