"""ail5c_evidence_enum_ext

Revision ID: ail5c_evidence_enum_ext
Revises: ail5c_assessment_tables
Create Date: 2026-09-25 09:00:00

AIL.5C R2 — the highest-risk revision, isolated on purpose.

* ``learning_evidence.evidence_type`` CHECK widened with ``explain_back``,
  ``modification``, ``reproduction``, ``debugging``, ``project_assessment``.
* ``learning_evidence.ref_type`` CHECK widened with ``assessment_result``.
* Partial UNIQUE index ``uq_learning_evidence_assessment_ref`` on
  ``(user_id, ref_id, concept_id)`` where ``ref_type = 'assessment_result'``:
  assessment-derived evidence is idempotent, enforced by the database.

SQLite cannot ALTER a CHECK in place, so this is the established batch table
rebuild (see ``ail4c_professor_agent_role`` / ``ail3c_experiment_conclusion``):
foreign keys are paused for the rebuild, the two stale CHECKs are dropped by
name first, the rebuild is followed by ``PRAGMA foreign_key_check``, and a
leftover ``_alembic_tmp_learning_evidence`` from a killed earlier attempt is
removed only when the canonical table still exists.

The downgrade refuses, changing nothing, while any assessment-derived evidence
exists: learner evidence is append-only and is never deleted by a downgrade.
"""

import re
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "ail5c_evidence_enum_ext"
down_revision: Union[str, None] = "ail5c_assessment_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_EVIDENCE_TYPES = (
    "lesson_completed",
    "knowledge_check",
    "observation",
    "lab",
    "interpretation",
    "self_report",
)
_NEW_EVIDENCE_TYPES = _OLD_EVIDENCE_TYPES + (
    "explain_back",
    "modification",
    "reproduction",
    "debugging",
    "project_assessment",
)
_OLD_REF_TYPES = (
    "agent_run",
    "evaluation_run",
    "model_routing_decision",
    "tool_call",
    "workflow_run",
    "experiment",
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
    "assessment_result",
    "none",
)
_INDEX = "uq_learning_evidence_assessment_ref"


def _enum(name: str, values: Sequence[str]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _in_check_names(bind, column: str):
    """Names of the CHECK constraints of the form ``<column> IN (...)``."""
    pattern = re.compile(rf"^\s*{re.escape(column)}\s+IN\b", re.IGNORECASE)
    return {
        c["name"]
        for c in sa.inspect(bind).get_check_constraints("learning_evidence")
        if c.get("name") and pattern.match(c["sqltext"])
    }


def _rebuild(evidence_types: Sequence[str], ref_types: Sequence[str], old_evidence, old_ref) -> None:
    bind = op.get_bind()
    raw = bind.connection.dbapi_connection
    if raw.in_transaction:
        raw.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("learning_evidence", schema=None) as batch_op:
            for column in ("evidence_type", "ref_type"):
                for name in _in_check_names(bind, column):
                    batch_op.drop_constraint(batch_op.f(name), type_="check")
            batch_op.alter_column(
                "evidence_type",
                existing_type=_enum("evidencetype", old_evidence),
                type_=_enum("evidencetype", evidence_types),
                existing_nullable=False,
            )
            batch_op.alter_column(
                "ref_type",
                existing_type=_enum("evidencereftype", old_ref),
                type_=_enum("evidencereftype", ref_types),
                existing_nullable=False,
            )
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"learning_evidence rebuild left {len(violations)} foreign-key violation(s); refusing to continue"
            )
    finally:
        if not raw.in_transaction:
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "_alembic_tmp_learning_evidence" in tables:
        if "learning_evidence" not in tables:
            raise RuntimeError(
                "Cannot recover learning_evidence migration: canonical learning_evidence table is missing."
            )
        op.drop_table("_alembic_tmp_learning_evidence")

    _rebuild(_NEW_EVIDENCE_TYPES, _NEW_REF_TYPES, _OLD_EVIDENCE_TYPES, _OLD_REF_TYPES)
    op.create_index(
        _INDEX,
        "learning_evidence",
        ["user_id", "ref_id", "concept_id"],
        unique=True,
        sqlite_where=sa.text("ref_type = 'assessment_result'"),
        postgresql_where=sa.text("ref_type = 'assessment_result'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    new_types = ", ".join(f"'{v}'" for v in _NEW_EVIDENCE_TYPES if v not in _OLD_EVIDENCE_TYPES)
    remaining = bind.exec_driver_sql(
        f"SELECT COUNT(*) FROM learning_evidence WHERE evidence_type IN ({new_types}) "
        "OR ref_type = 'assessment_result'"
    ).scalar()
    if remaining:
        raise RuntimeError(
            f"cannot downgrade: {remaining} assessment-derived Learning Evidence row(s) exist; "
            "learner evidence is append-only and is never deleted by a downgrade"
        )
    op.drop_index(_INDEX, table_name="learning_evidence")
    _rebuild(_OLD_EVIDENCE_TYPES, _OLD_REF_TYPES, _NEW_EVIDENCE_TYPES, _NEW_REF_TYPES)
