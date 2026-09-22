"""ma8.2 evidence routing policy and indexes

Revision ID: 8c3f6b2e9d14
Revises: 5e1d8a4c7b30
Create Date: 2026-09-22 18:00:00.000000

Additive only. No new table or column.

* Two indexes for the bounded routing-evidence query
  (app.routing_evidence.load_evidence): ``model_calls(provider_model_id,
  started_at)`` and ``evaluation_runs(subject_agent_run_id)``. SQLite does not
  index foreign keys by itself.
* One ``router_policy_versions`` row (version 1, status ``inactive``) holding
  the ``ma8.2-evidence-v1`` configuration. The MA0 table had no rows and no
  reader before MA8.2. The migration installs the capability but does not
  change routing: with no ACTIVE row, AUTO routing is exactly MA8.1. Evidence
  keeps accumulating in model_calls/evaluation_runs regardless; an operator
  activates the policy explicitly later (MA8.3/MA8.4). The config is written
  here as a literal so the recorded version never changes if code defaults
  change later.

The downgrade refuses, changing nothing, while any routing decision references
the version 1 row -- deleting it would erase what those decisions were based
on. Otherwise it removes the row and both indexes.
"""

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c3f6b2e9d14"
down_revision: Union[str, None] = "5e1d8a4c7b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_V1_CONFIG = {
    "strategy": "ma8.2-evidence-v1",
    "history_window_days": 30,
    "max_history_rows": 2000,
    "min_observations": 5,
    "max_provider_failure_rate": 0.5,
    "min_evaluated_runs": 3,
    "positive_met_share": 0.7,
    "negative_not_met_share": 0.3,
}

_policy_versions = sa.table(
    "router_policy_versions",
    sa.column("id", sa.String(36)),
    sa.column("version", sa.Integer()),
    sa.column("scoring_config_json", sa.JSON()),
    sa.column("free_policy", sa.String(20)),
    sa.column("status", sa.String(20)),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.create_index(
        "ix_model_calls_provider_model_id_started_at", "model_calls", ["provider_model_id", "started_at"]
    )
    op.create_index("ix_evaluation_runs_subject_agent_run_id", "evaluation_runs", ["subject_agent_run_id"])

    bind = op.get_bind()
    exists = bind.execute(sa.text("SELECT 1 FROM router_policy_versions WHERE version = 1")).first()
    if exists is None:
        op.bulk_insert(
            _policy_versions,
            [
                {
                    "id": str(uuid.uuid4()),
                    "version": 1,
                    "scoring_config_json": _V1_CONFIG,
                    "free_policy": None,
                    "status": "inactive",
                    "created_at": datetime.now(timezone.utc),
                }
            ],
        )


def downgrade() -> None:
    bind = op.get_bind()
    referenced = bind.execute(
        sa.text(
            "SELECT count(*) FROM model_routing_decisions d JOIN router_policy_versions v "
            "ON v.id = d.router_policy_version_id WHERE v.version = 1"
        )
    ).scalar()
    if referenced:
        raise RuntimeError(
            f"Refusing to downgrade 8c3f6b2e9d14: {referenced} routing decision(s) reference router policy "
            "version 1; removing it would erase what those decisions were based on."
        )
    bind.execute(sa.text("DELETE FROM router_policy_versions WHERE version = 1"))
    op.drop_index("ix_evaluation_runs_subject_agent_run_id", table_name="evaluation_runs")
    op.drop_index("ix_model_calls_provider_model_id_started_at", table_name="model_calls")
