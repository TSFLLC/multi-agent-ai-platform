"""Agent-to-Agent Review contract — MA4.

A structured, machine-validated review of one exact, immutable candidate
Artifact — never inferred from casual prose (``app.review_contract`` parses
the reviewer's raw response into this shape, or records why it could not).

``candidate_artifact_hash`` is a defensive, redundant copy of
``artifacts.content_hash`` taken at review-creation time — the same
freeze-at-decision-time pattern already used by
``provider_model_snapshots``/``app.model_resolution.freeze_snapshot`` — so a
stale review can never be mistaken for approving a different artifact even
if the immutable-artifact discipline were ever violated elsewhere.

Deliberately separate from ``app.models.artifacts_eval.Evaluation``: MA6
owns the real Evaluation Engine (objective metrics, judge scoring); this is
review/repair-loop quality-control infrastructure only — no scoring or
ranking lives here.
"""

from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import ReviewDecision
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import sa_enum


class AgentReview(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Section: MA4 review contract — decision/summary/issues/repair
    instructions, reviewer Agent Version + model snapshot, and the exact
    artifact/hash reviewed, with timestamps (``created_at`` via
    ``CreatedAtMixin``)."""

    __tablename__ = "agent_reviews"

    task_run_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)

    candidate_agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    candidate_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    # Redundant copy of artifacts.content_hash at review time — see module
    # docstring. Nullable only because Artifact.content_hash itself is
    # nullable (Section 24.4 #18); never populated with a guess.
    candidate_artifact_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    reviewer_agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_agent_version_id: Mapped[str] = mapped_column(ForeignKey("agent_versions.id"), nullable=False)
    reviewer_provider_model_snapshot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("provider_model_snapshots.id"), nullable=True
    )

    # 0 = review of the initial candidate; 1 = review after repair #1; etc.
    iteration_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    decision: Mapped[ReviewDecision] = mapped_column(sa_enum(ReviewDecision), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    issues: Mapped[Optional[list]] = mapped_column("issues_json", nullable=True)
    repair_instructions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Populated only when decision=INVALID — a short diagnostic, never the
    # raw reviewer text duplicated wholesale (that stays in the reviewer's
    # own Artifact row) and never a secret.
    parse_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
