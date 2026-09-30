"""AIL.5D.1 — learner step progress over structured Learning Items.

Authored steps live in an immutable, versioned ``LearningItem`` (``spec_json.steps``, see ``app.academy_steps``).
This service owns the *mutable* half: one ``AcademyStepProgress`` row per (learner, exact item version, step).

Rules it enforces
-----------------
* Every operation is scoped to the authenticated ``user_id`` passed in by the caller. There is no way to name
  another learner, and the item is looked up by id, never trusted from a request body.
* Only the CURRENT version of an item can be acted on. A stale id gets a 409 that names the current version.
* ``open`` records "the learner viewed this step" and can never produce completion.
* ``complete`` succeeds for a self-completable step (teach, example) on the learner's explicit Continue. Every
  interactive type needs a ``VerifiedCompletion`` that only server code can construct; nothing reachable from the
  API builds one in this slice, so an interactive step cannot be completed by asking for it.
* Required steps cannot be skipped. Optional steps can.
* Completion is never evidence. This service writes nothing to LearningEvidence, AIL.5C, Personal Lab or the
  Professor. "Learning complete" is derived and labelled as not demonstrated knowledge.
* Progress carries to a newer item version only for a step whose learner-visible fingerprint is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.academy_steps import SELF_COMPLETABLE, STEP_SCHEMA_VERSION, StepType, is_structured, public_steps
from app.db.enums import AcademyStepStatus
from app.db.mixins import utcnow
from app.errors import ConflictError, NotFoundError
from app.models.academy import AcademyStepProgress
from app.models.concepts import LearningItem
from app.services.concept_graph_service import ConceptGraphService

NOT_EVIDENCE = (
    "Learning complete is not demonstrated knowledge. Evidence is recorded by the knowledge check, the Personal Lab "
    "and the AIL.5C assessment, never by finishing steps."
)


@dataclass(frozen=True)
class VerifiedCompletion:
    """Proof, produced by trusted server code, that a step's own interaction really happened (a graded check, a
    finished lab run, a committed answer, ...). Later slices construct this from records they verified; it is not
    part of any request schema and cannot be deserialised from a client."""

    kind: str
    reference: Optional[str] = None


class AcademyStepService:
    def __init__(self, db: Session, *, clock: Callable[[], datetime] = utcnow):
        self.db = db
        self._clock = clock
        self._graph = ConceptGraphService(db)

    # -- item + step resolution -------------------------------------------------------------------------------

    def _item(self, item_id: str) -> LearningItem:
        """The exact Academy item, which must be the CURRENT version of its lineage and structured."""
        item = self.db.get(LearningItem, item_id)
        if item is None or not (item.spec or {}).get("academy_key"):
            raise NotFoundError("Academy learning item not found")
        if not is_structured(item.spec):
            raise NotFoundError("This Academy item has no structured steps")
        current = self._graph.get_current_learning_item(item.lineage_id)
        if current is None or current.id != item.id:
            raise ConflictError(
                "This lesson has been updated. Reload to continue on the current version.",
                detail={"code": "stale_item_version", "current_item_id": current.id if current else None, "current_version": current.version if current else None},
            )
        return item

    @staticmethod
    def _steps(item: LearningItem) -> List[dict]:
        steps = public_steps(item.spec)
        assert steps is not None
        return steps

    def _step(self, item: LearningItem, step_key: str) -> dict:
        step = next((s for s in self._steps(item) if s["key"] == step_key), None)
        if step is None:
            raise NotFoundError("Step not found on this lesson")
        return step

    # -- reads ------------------------------------------------------------------------------------------------

    def structure(self, item_id: str) -> Dict[str, Any]:
        item = self._item(item_id)
        return {
            "item_id": item.id, "lineage_id": item.lineage_id, "version": item.version, "title": item.title,
            "step_schema_version": STEP_SCHEMA_VERSION, "steps": self._steps(item),
        }

    def _rows(self, user_id: str, item: LearningItem) -> List[AcademyStepProgress]:
        return list(self.db.execute(
            select(AcademyStepProgress).where(AcademyStepProgress.user_id == user_id, AcademyStepProgress.lineage_id == item.lineage_id)
        ).scalars().all())

    def progress(self, user_id: str, item_id: str) -> Dict[str, Any]:
        item = self._item(item_id)
        rows = self._rows(user_id, item)
        out_steps, req_total, req_done, opt_total, opt_done = [], 0, 0, 0, 0
        for step in self._steps(item):
            own = next((r for r in rows if r.learning_item_id == item.id and r.step_key == step["key"]), None)
            carried_from: Optional[int] = None
            row = own
            if row is None or row.status == AcademyStepStatus.OPENED:
                inherited = self._inherit(rows, item, step)
                if inherited is not None:
                    row, carried_from = inherited
            status = "not_started" if row is None else row.status.value
            if own is not None and own.status == AcademyStepStatus.OPENED and carried_from is None:
                status = "opened"
            counted = row is not None and row.status == AcademyStepStatus.COMPLETED
            if step["required"]:
                req_total += 1
                req_done += 1 if counted else 0
            else:
                opt_total += 1
                opt_done += 1 if counted else 0
            out_steps.append({
                "key": step["key"], "position": step["position"], "type": step["type"], "required": step["required"],
                "status": status, "carried_from_version": carried_from,
                "open_count": own.open_count if own else 0,
                "completed_at": row.completed_at if row is not None and row.status == AcademyStepStatus.COMPLETED else None,
            })
        return {
            "item_id": item.id, "lineage_id": item.lineage_id, "version": item.version, "steps": out_steps,
            "required_total": req_total, "required_completed": req_done, "optional_total": opt_total, "optional_completed": opt_done,
            "learning_complete": req_total > 0 and req_done == req_total, "note": NOT_EVIDENCE,
        }

    def _inherit(self, rows: List[AcademyStepProgress], item: LearningItem, step: dict):
        """A completed/skipped row from an OLDER version of the same lineage whose step is unchanged."""
        best = None
        for r in rows:
            if r.learning_item_id == item.id or r.step_key != step["key"] or r.step_fingerprint != step["fingerprint"]:
                continue
            if r.status not in (AcademyStepStatus.COMPLETED, AcademyStepStatus.SKIPPED):
                continue
            version = self.db.get(LearningItem, r.learning_item_id).version
            if version < item.version and (best is None or version > best[1]):
                best = (r, version)
        return best

    # -- writes -----------------------------------------------------------------------------------------------

    def _lookup(self, user_id: str, item: LearningItem, step_key: str) -> Optional[AcademyStepProgress]:
        return self.db.execute(select(AcademyStepProgress).where(
            AcademyStepProgress.user_id == user_id, AcademyStepProgress.learning_item_id == item.id, AcademyStepProgress.step_key == step_key,
        )).scalar_one_or_none()

    def _row(self, user_id: str, item: LearningItem, step: dict) -> tuple[AcademyStepProgress, bool]:
        """Get-or-create the learner's row for this step. Returns (row, created)."""
        row = self._lookup(user_id, item, step["key"])
        if row is not None:
            return row, False
        now = self._clock()
        row = AcademyStepProgress(
            user_id=user_id, learning_item_id=item.id, lineage_id=item.lineage_id, step_key=step["key"],
            step_fingerprint=step["fingerprint"], status=AcademyStepStatus.OPENED, open_count=1, first_opened_at=now, last_opened_at=now,
        )
        self.db.add(row)
        try:
            self.db.flush()
        except IntegrityError:  # a concurrent request created it first
            self.db.rollback()
            return self._lookup(user_id, item, step["key"]), False
        return row, True

    def open_step(self, user_id: str, item_id: str, step_key: str) -> Dict[str, Any]:
        """Record that the learner viewed the step. Never completes it; never downgrades a completed/skipped row."""
        item = self._item(item_id)
        step = self._step(item, step_key)
        row, created = self._row(user_id, item, step)
        if not created:
            row.open_count += 1
            row.last_opened_at = self._clock()
        self.db.commit()
        return self._one(user_id, item, step_key)

    def complete_step(self, user_id: str, item_id: str, step_key: str, *, verified: Optional[VerifiedCompletion] = None) -> Dict[str, Any]:
        item = self._item(item_id)
        step = self._step(item, step_key)
        step_type = StepType(step["type"])
        if step_type in SELF_COMPLETABLE:
            basis = {"kind": "continue"}
        elif verified is not None:
            basis = {"kind": verified.kind, "reference": verified.reference}
        else:
            raise ConflictError(
                "This step is completed by doing it, not by asking for completion.",
                detail={"code": "interaction_required", "step_type": step_type.value},
            )
        row, _ = self._row(user_id, item, step)
        if row.status != AcademyStepStatus.COMPLETED:
            now = self._clock()
            row.status = AcademyStepStatus.COMPLETED
            row.completed_at = now
            row.skipped_at = None
            row.completion_basis = basis
            row.step_fingerprint = step["fingerprint"]
        self.db.commit()
        return self._one(user_id, item, step_key)

    def skip_step(self, user_id: str, item_id: str, step_key: str) -> Dict[str, Any]:
        item = self._item(item_id)
        step = self._step(item, step_key)
        if step["required"]:
            raise ConflictError("A required step cannot be skipped.", detail={"code": "required_step"})
        row, _ = self._row(user_id, item, step)
        if row.status == AcademyStepStatus.OPENED:
            row.status = AcademyStepStatus.SKIPPED
            row.skipped_at = self._clock()
        self.db.commit()
        return self._one(user_id, item, step_key)

    def _one(self, user_id: str, item: LearningItem, step_key: str) -> Dict[str, Any]:
        summary = self.progress(user_id, item.id)
        step = next(s for s in summary["steps"] if s["key"] == step_key)
        return {"step": step, "day": {k: summary[k] for k in ("item_id", "version", "required_total", "required_completed", "optional_total", "optional_completed", "learning_complete", "note")}}
