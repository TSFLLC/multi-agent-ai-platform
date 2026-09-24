"""ail3c_experiment_conclusion_and_evidence

Revision ID: ail3c_experiment_conclusion
Revises: ail3b_experiment_execution
Create Date: 2026-09-23 16:00:00

AIL.3C: human conclusion, frozen Concept version, and experiment-backed
Learning Evidence.

- ``experiments.concept_version_id`` (nullable FK -> concept_versions.id): the
  ConceptVersion frozen when a Concept is bound to the Experiment.
- ``experiments.conclusion_type/conclusion_text/concluded_at``: the owner's own
  interpretation, stored separately from any MA6 evidence.
- ``learning_evidence.ref_type`` CHECK widened to allow ``'experiment'``
  (app.db.enums.EvidenceRefType.EXPERIMENT). SQLite cannot ALTER a CHECK in
  place, so this uses the same batch rebuild with foreign-key enforcement
  paused as c7f4a2d9e815.
- Partial UNIQUE index on ``learning_evidence(user_id, ref_id)`` where
  ``ref_type = 'experiment'``: exactly one experiment-backed evidence row per
  user and experiment, enforced by the database.

The downgrade refuses, changing nothing, while any experiment-backed evidence
exists, rather than deleting learner evidence to satisfy the narrower CHECK.
"""

from typing import Callable, Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ail3c_experiment_conclusion"
down_revision: Union[str, None] = "ail3b_experiment_execution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_REF_TYPES = (
    "agent_run",
    "evaluation_run",
    "model_routing_decision",
    "tool_call",
    "workflow_run",
    "human",
    "none",
)
_NEW_REF_TYPES = (
    "agent_run",
    "evaluation_run",
    "model_routing_decision",
    "tool_call",
    "workflow_run",
    "experiment",
    "human",
    "none",
)
_INDEX = "uq_learning_evidence_experiment_ref"


def _with_foreign_keys_paused(rebuild: Callable[[], None]) -> None:
    """Run a batch table rebuild with FK enforcement off, then prove no child
    row was orphaned. The pragma is a no-op inside a transaction."""
    bind = op.get_bind()
    raw = bind.connection.dbapi_connection
    if raw.in_transaction:
        raw.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        rebuild()
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"AIL.3C table rebuild left {len(violations)} foreign-key violation(s); refusing to continue"
            )
    finally:
        if not raw.in_transaction:
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _rebuild_ref_type(values: Sequence[str], existing_values: Sequence[str]) -> None:
    def rebuild() -> None:
        with op.batch_alter_table("learning_evidence", schema=None) as batch_op:
            batch_op.alter_column(
                "ref_type",
                existing_type=sa.Enum(
                    *existing_values, name="evidencereftype", native_enum=False, create_constraint=True
                ),
                type_=sa.Enum(*values, name="evidencereftype", native_enum=False, create_constraint=True),
                existing_nullable=False,
            )

    _with_foreign_keys_paused(rebuild)


def upgrade() -> None:
    op.execute(
        'ALTER TABLE "experiments" ADD COLUMN concept_version_id VARCHAR(36) '
        "REFERENCES concept_versions(id)"
    )
    op.add_column("experiments", sa.Column("conclusion_type", sa.String(50), nullable=True))
    op.add_column("experiments", sa.Column("conclusion_text", sa.Text(), nullable=True))
    op.add_column("experiments", sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True))

    _rebuild_ref_type(_NEW_REF_TYPES, _OLD_REF_TYPES)

    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX} ON learning_evidence (user_id, ref_id) WHERE ref_type = 'experiment'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    remaining = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM learning_evidence WHERE ref_type = 'experiment'"
    ).scalar()
    if remaining:
        raise RuntimeError(
            f"cannot downgrade: {remaining} experiment-backed Learning Evidence row(s) exist; "
            "learner evidence is append-only and is never deleted by a downgrade"
        )

    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    _rebuild_ref_type(_OLD_REF_TYPES, _NEW_REF_TYPES)

    def drop_columns() -> None:
        with op.batch_alter_table("experiments", schema=None) as batch_op:
            batch_op.drop_column("concluded_at")
            batch_op.drop_column("conclusion_text")
            batch_op.drop_column("conclusion_type")
            batch_op.drop_column("concept_version_id")

    _with_foreign_keys_paused(drop_columns)
