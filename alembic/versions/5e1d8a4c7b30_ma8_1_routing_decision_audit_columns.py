"""ma8.1 routing decision audit columns

Revision ID: 5e1d8a4c7b30
Revises: c7f4a2d9e815
Create Date: 2026-09-22 12:00:00.000000

Additive only: three nullable columns on ``model_routing_decisions`` (the
MA0 routing-reproducibility table, which had no writer before MA8.1 and so
holds no rows on any existing database). No CHECK/enum change, no index, no
data rewrite.

* ``requested_policy`` -- FREE_ONLY/PREFER_FREE/ANY value an AUTO decision
  applied (NULL for MANUAL).
* ``routing_strategy`` -- identifier of the deterministic algorithm that made
  the decision (``app.model_resolution.ROUTING_STRATEGY``).
* ``decision_json`` -- outcome, failure code, exclusion reasons/counts and the
  PREFER_FREE fallback flags.

Both directions are native SQLite ``ALTER TABLE ... ADD/DROP COLUMN`` (no
table rebuild). That matters for the downgrade: ``model_routing_decisions`` is
a parent of ``model_calls.model_routing_decision_id``, and a batch rebuild
under ``PRAGMA foreign_keys=ON`` would have to drop the referenced table.
DROP COLUMN needs SQLite >= 3.35, which the supported runtimes ship.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e1d8a4c7b30"
down_revision: Union[str, None] = "c7f4a2d9e815"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "model_routing_decisions"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("requested_policy", sa.String(length=20), nullable=True))
    op.add_column(_TABLE, sa.Column("routing_strategy", sa.String(length=60), nullable=True))
    op.add_column(_TABLE, sa.Column("decision_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "decision_json")
    op.drop_column(_TABLE, "routing_strategy")
    op.drop_column(_TABLE, "requested_policy")
