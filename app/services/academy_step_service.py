"""AIL.5D.1 — learner step progress over structured Learning Items.

Authored steps live in an immutable, versioned ``LearningItem`` (``spec_json.steps``, see ``app.academy_steps``).
This service owns the *mutable* half: one ``AcademyStepProgress`` row per (learner, exact item version, step).

Rules it enforces
-----------------
* Every operation is scoped to the authenticated ``user_id`` passed in by the caller. There is no way to name
  another learner, and the item is looked up by id, never trusted from a request body.
* Only the CURRENT version of an item can be acted on. A stale id gets a 409 that names the current version.
* ``open`` records "the learner viewed this step" and can never produce completion.
* ``complete`` succeeds for a self-completable step (teach, example) on the learner's explicit Continue. An
  interactive step completes only when the SERVER verifies its own interaction from persisted records (AIL.5D.3): a
  valid saved response (reflect, reflection, explain_back outline), a committed answer (think), or a PASSED canonical
  knowledge-check evidence row (check; AIL.5D.4: an unsuccessful attempt is recorded and can be retried, it never
  completes the step). The client never states that a step is complete; a ``lab`` step cannot complete
  until the Lab slice. A ``VerifiedCompletion`` can still be supplied by trusted server code, never by a request.
* Learner responses (AIL.5D.3) are append-only, owned by the learner, bound to the exact item version, and private.
  A ``think`` answer is committed once and the authored reveal is only served after that commit.
* Required steps cannot be skipped. Optional steps can.
* Completion is never evidence, with ONE deliberate exception (AIL.5D.2): when the learner's last REQUIRED step
  actually completes, a single ``lesson_completed`` evidence row is written (idempotent per learner and lineage). It
  is the moral equivalent of the legacy "open" write, but earned: opening, viewing and skipping never write it.
  Nothing is ever written to AIL.5C, Personal Lab or the Professor. "Learning complete" is derived and labelled as
  not demonstrated knowledge.
* Progress carries to a newer item version only for a step whose learner-visible fingerprint is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.academy_step_responses import ResponseError, accepts_response, validate_response
from app.academy_steps import SELF_COMPLETABLE, STEP_SCHEMA_VERSION, StepType, is_structured, public_steps, validate_structured_spec
from app.db.enums import AcademyStepResponseKind, AcademyStepStatus, EvidenceType, GradingMode
from app.db.mixins import utcnow
from app.errors import ConflictError, InvalidResponseError, NotFoundError
from app.models.academy import AcademyProfessorHelp, AcademyStepProgress, AcademyStepResponse
from app.models.concepts import LearningItem
from app.models.learner import LearningEvidence
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_state_service import LearnerStateService
from app.services.learning_evidence_service import LearningEvidenceService

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


@dataclass(frozen=True)
class _Part:
    kind: AcademyStepResponseKind
    key: str
    content: Dict[str, Any]


_NoteViewed = _Part(AcademyStepResponseKind.NOTE, "reveal-viewed", {"viewed": True})


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
        pending = [s["key"] for s in out_steps if s["status"] in ("not_started", "opened")]
        return {
            "item_id": item.id, "lineage_id": item.lineage_id, "version": item.version, "steps": out_steps,
            "current_step_key": pending[0] if pending else None, "next_step_key": pending[1] if len(pending) > 1 else None,
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
        else:
            verified = verified or self._verify(user_id, item, step)
            if verified is None:
                raise ConflictError(
                    "This step is completed by doing it, not by asking for completion.",
                    detail={"code": "interaction_required", "step_type": step_type.value},
                )
            basis = {"kind": verified.kind, "reference": verified.reference}
        row, _ = self._row(user_id, item, step)
        if row.status != AcademyStepStatus.COMPLETED:
            now = self._clock()
            row.status = AcademyStepStatus.COMPLETED
            row.completed_at = now
            row.skipped_at = None
            row.completion_basis = basis
            row.step_fingerprint = step["fingerprint"]
        self.db.commit()
        if step["required"] and self.progress(user_id, item.id)["learning_complete"]:
            self._record_lesson_completed_once(user_id, item)
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

    # -- learner responses (AIL.5D.3) --------------------------------------------------------------------------------

    def _raw_step(self, item: LearningItem, step_key: str) -> dict:
        """The authored step INCLUDING ``private`` -- for server-side use only, never returned to a client."""
        return next(s for s in validate_structured_spec(item.spec) if s["key"] == step_key)

    def _latest_responses(self, user_id: str, item: LearningItem, step: dict) -> Dict[tuple, AcademyStepResponse]:
        """This learner's latest response per (kind, key) for the step, across versions of the lineage but only where the
        step's learner-visible definition is unchanged."""
        rows = self.db.execute(select(AcademyStepResponse).where(
            AcademyStepResponse.user_id == user_id, AcademyStepResponse.lineage_id == item.lineage_id,
            AcademyStepResponse.step_key == step["key"], AcademyStepResponse.step_fingerprint == step["fingerprint"],
        ).order_by(AcademyStepResponse.created_at, AcademyStepResponse.revision)).scalars().all()
        latest: Dict[tuple, AcademyStepResponse] = {}
        for row in rows:
            latest[(row.kind, row.response_key)] = row
        return latest

    def _append(self, user_id: str, item: LearningItem, step: dict, parts, *, ref_type: Optional[str] = None, ref_id: Optional[str] = None) -> List[AcademyStepResponse]:
        now = self._clock()
        rows = []
        for part in parts:
            top = self.db.execute(select(func.max(AcademyStepResponse.revision)).where(
                AcademyStepResponse.user_id == user_id, AcademyStepResponse.learning_item_id == item.id,
                AcademyStepResponse.step_key == step["key"], AcademyStepResponse.kind == part.kind, AcademyStepResponse.response_key == part.key,
            )).scalar()
            row = AcademyStepResponse(
                user_id=user_id, learning_item_id=item.id, lineage_id=item.lineage_id, step_key=step["key"], step_fingerprint=step["fingerprint"],
                kind=part.kind, response_key=part.key, revision=(top or 0) + 1, content=part.content, ref_type=ref_type, ref_id=ref_id, created_at=now,
            )
            self.db.add(row)
            rows.append(row)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ConflictError("Your response was saved by another request at the same moment; reload and try again.", detail={"code": "concurrent_response"})
        return rows

    @staticmethod
    def _response_view(step: dict, latest: Dict[tuple, AcademyStepResponse]) -> Optional[dict]:
        """The learner's own latest response, shaped per step type. None when there is none."""
        t = StepType(step["type"])
        if t in (StepType.REFLECT, StepType.REFLECTION):
            row = latest.get((AcademyStepResponseKind.REFLECTION, ""))
            return {"text": row.content["text"]} if row else None
        if t == StepType.THINK:
            if "statements" in step["content"]:
                chosen = {s["id"]: latest.get((AcademyStepResponseKind.ANSWER, s["id"])) for s in step["content"]["statements"]}
                return {"statements": {k: dict(v.content) for k, v in chosen.items()}} if all(chosen.values()) else None
            row = latest.get((AcademyStepResponseKind.ANSWER, ""))
            return {"text": row.content["text"]} if row else None
        if t == StepType.EXPLAIN_BACK:
            rows = {p["key"]: latest.get((AcademyStepResponseKind.OUTLINE, p["key"])) for p in step["content"]["points"]}
            return {"points": {k: v.content["text"] for k, v in rows.items()}} if all(rows.values()) else None
        return None

    def respond(self, user_id: str, item_id: str, step_key: str, payload: Optional[dict]) -> Dict[str, Any]:
        """Persist the learner's response to a step. Validates against the authored step; never grades, never writes
        evidence, never completes the step (Continue does, after the server verifies the response)."""
        item = self._item(item_id)
        step = self._step(item, step_key)
        if not accepts_response(step):
            raise ConflictError("This step does not take a direct response.", detail={"code": "no_response_accepted", "step_type": step["type"]})
        try:
            parts = validate_response(step, payload)
        except ResponseError as exc:
            raise InvalidResponseError(str(exc))
        latest = self._latest_responses(user_id, item, step)
        if StepType(step["type"]) == StepType.THINK and self._response_view(step, latest) is not None:
            raise ConflictError(
                "Your answer is already committed and cannot be changed after the reveal is available.", detail={"code": "already_committed"},
            )
        self._append(user_id, item, step, parts)
        latest = self._latest_responses(user_id, item, step)
        return {"step_key": step_key, "response": self._response_view(step, latest), "saved": True}

    def responses(self, user_id: str, item_id: str, step_key: str) -> Dict[str, Any]:
        item = self._item(item_id)
        step = self._step(item, step_key)
        return {"step_key": step_key, "response": self._response_view(step, self._latest_responses(user_id, item, step))}

    @staticmethod
    def _reveal_payload(step: dict, private: dict, mine: dict) -> Dict[str, Any]:
        if "statements" in step["content"]:
            return {"claims": {
                sid: {"answer": c["answer"], "why_md": c["why_md"], "matched": mine["statements"][sid]["choice"] == c["answer"]}
                for sid, c in private["claims"].items()
            }}
        return {"reveal_md": private["reveal_md"]}

    def reveal(self, user_id: str, item_id: str, step_key: str) -> Dict[str, Any]:
        """The authored reveal of a ``think`` step, served only after THIS learner committed an answer. The only path
        by which ``private`` content is ever returned, and it is explicitly scoped to the committed learner and step."""
        item = self._item(item_id)
        step = self._step(item, step_key)
        if StepType(step["type"]) != StepType.THINK:
            raise ConflictError("Only a think step has a reveal.", detail={"code": "no_reveal"})
        latest = self._latest_responses(user_id, item, step)
        mine = self._response_view(step, latest)
        if mine is None:
            raise ConflictError("Commit your answer first; the explanation is shown after you commit.", detail={"code": "commit_required"})
        private = self._raw_step(item, step_key)["private"]
        if (AcademyStepResponseKind.NOTE, "reveal-viewed") not in latest:
            self._append(user_id, item, step, [_NoteViewed])
        return {"step_key": step_key, **self._reveal_payload(step, private, mine)}

    # -- knowledge check (the existing canonical mechanism) --------------------------------------------------------------

    def _kc_evidence(self, user_id: str, item: LearningItem) -> List[LearningEvidence]:
        return list(self.db.execute(select(LearningEvidence).where(
            LearningEvidence.user_id == user_id, LearningEvidence.learning_item_id == item.id,
            LearningEvidence.evidence_type == EvidenceType.KNOWLEDGE_CHECK,
        ).order_by(LearningEvidence.created_at)).scalars())

    def record_check_submission(self, user_id: str, item: LearningItem, answers: dict, evidence: LearningEvidence) -> None:
        """Keep the learner's own submitted answers with the structured step. The canonical result stays the evidence row."""
        if not is_structured(item.spec):
            return
        step = next((s for s in self._steps(item) if s["type"] == StepType.CHECK.value), None)
        if step is None:
            return
        part = _Part(AcademyStepResponseKind.ANSWER, "", {"answers": answers, "passed": bool(evidence.passed)})
        self._append(user_id, item, step, [part], ref_type="learning_evidence", ref_id=evidence.id)

    def check_feedback(self, user_id: str, item: LearningItem, passed: bool, results: List[dict]) -> Optional[Dict[str, Any]]:
        """Learning feedback after an unsuccessful structured knowledge check. It says how far the learner is and that they
        can retry, and deliberately withholds the answers and the authored rationale (released once every item is right)
        so that retrying remains meaningful. None for a pass and for any non-structured item."""
        if passed or not is_structured(item.spec):
            return None
        incorrect = [r["id"] for r in results if not r["passed"]]
        return {
            "message": "Not quite. Review the scenarios marked incorrect, think about what makes each one AI or ordinary software, "
                       "and try again. The explanations unlock when every item is right.",
            "incorrect_item_ids": incorrect, "incorrect": len(incorrect), "total": len(results),
            "attempt": len(self._kc_evidence(user_id, item)), "retry_allowed": True,
        }

    def _check_view(self, user_id: str, item: LearningItem) -> Dict[str, Any]:
        public = []
        for q in (item.spec or {}).get("knowledge_check", []):
            shown = {k: v for k, v in q.items() if k not in ("answer", "explanation", "pass_criteria", "tolerance")}
            if "choices" not in q and isinstance(q.get("answer"), dict):
                shown["fields"] = list(q["answer"])          # only the NAMES of the numeric answers, never a value or the tolerance
            public.append(shown)
        rows = self._kc_evidence(user_id, item)
        result = None
        review = None
        if rows:
            last = rows[-1]
            score = last.score or {}
            result = {"submitted": True, "attempts": len(rows), "passed": bool(last.passed), "ever_passed": any(r.passed for r in rows), "score": {"raw": score.get("raw"), "max": score.get("max")}, "items": [{"id": i.get("id"), "passed": i.get("passed")} for i in score.get("items", [])]}
            if any(r.passed for r in rows):
                # The authored rationale is revealed only once the check has been passed, so it cannot be used to copy answers.
                review = [{"id": q["id"], "explanation": q.get("explanation")} for q in (item.spec or {}).get("knowledge_check", [])]
        return {"questions": public, "result": result, "review": review}

    # -- server verification of interactive completion ---------------------------------------------------------------------

    def _verify(self, user_id: str, item: LearningItem, step: dict) -> Optional[VerifiedCompletion]:
        t = StepType(step["type"])
        if t == StepType.CHECK:
            # AIL.5D.4: a knowledge check is practice, not assessment, but it is still where a misunderstanding is
            # corrected. Only the authored success condition (every item right, recorded by the existing checker as
            # passed evidence) satisfies the step; failed attempts stay in the history and the learner may retry.
            passed = [r for r in self._kc_evidence(user_id, item) if r.passed]
            return VerifiedCompletion("knowledge_check_passed", passed[-1].id) if passed else None
        if t in (StepType.REFLECT, StepType.REFLECTION, StepType.THINK, StepType.EXPLAIN_BACK):
            latest = self._latest_responses(user_id, item, step)
            if self._response_view(step, latest) is None:
                return None
            kind = {StepType.THINK: "committed_answer", StepType.EXPLAIN_BACK: "outline_saved"}.get(t, "response_saved")
            newest = max((r for k, r in latest.items() if k[0] != AcademyStepResponseKind.NOTE), key=lambda r: r.created_at)
            return VerifiedCompletion(kind, newest.id)
        if t in (StepType.LAB, StepType.PRACTICE):
            # AIL.5D.5: a lab / practice step is completed by a COMPLETED Practice Instance the server itself finished
            # (never by a request). A lab step without a Lab Kit has no instance and still cannot be completed.
            from app.services.academy_practice_service import AcademyPracticeService

            return AcademyPracticeService(self.db, clock=self._clock).verified_completion(user_id, item, step)
        return None

    # -- step-scoped Professor (AIL.5D.4) ------------------------------------------------------------------------------------

    def help_history(self, user_id: str, item: LearningItem, step: dict) -> List[AcademyProfessorHelp]:
        """This learner's delivered Professor help on this step (same definition), oldest first."""
        return list(self.db.execute(select(AcademyProfessorHelp).where(
            AcademyProfessorHelp.user_id == user_id, AcademyProfessorHelp.lineage_id == item.lineage_id,
            AcademyProfessorHelp.step_key == step["key"], AcademyProfessorHelp.step_fingerprint == step["fingerprint"],
        ).order_by(AcademyProfessorHelp.created_at)).scalars())

    def professor_view(self, user_id: str, item_id: str, step_key: str) -> Dict[str, Any]:
        """Everything the step-scoped Professor may know, and nothing else. This is the single place where authored
        private material is read for the Professor, and it is only included once the learner is eligible to see it:

        * the think reveal   - only after THIS learner committed an answer AND was served the reveal;
        * the check rationale - only after THIS learner passed the check;
        * never an answer key, never the AIL.5C rubric, never another learner's data, never a response from an unrelated
          step (the one exception is the earlier step a reflection is explicitly asked to compare to).

        Raises the same stale-version conflict as every other step operation, so a stale item yields no context."""
        item = self._item(item_id)
        step = self._step(item, step_key)
        steps = self._steps(item)
        progress = self.progress(user_id, item.id)
        status = {s["key"]: s["status"] for s in progress["steps"]}
        latest = self._latest_responses(user_id, item, step)
        mine = self._response_view(step, latest)
        t = StepType(step["type"])
        state: Dict[str, Any] = {"committed": mine is not None if t == StepType.THINK else False, "passed": False}
        public = {k: step[k] for k in ("key", "position", "type", "title", "required", "estimated_minutes", "content", "binding") if k in step}
        learner: Dict[str, Any] = {"status": status[step_key]}
        reveal = rationales = compare = None
        if t in (StepType.REFLECT, StepType.REFLECTION, StepType.EXPLAIN_BACK):
            learner["response"] = mine
            if t == StepType.REFLECTION and step["content"].get("compare_to"):
                target = next(s for s in steps if s["key"] == step["content"]["compare_to"])
                compare = {"key": target["key"], "title": target["title"], "response": self._response_view(target, self._latest_responses(user_id, item, target))}
        elif t == StepType.THINK:
            learner["response"] = mine
            learner["committed"] = mine is not None
            served = (AcademyStepResponseKind.NOTE, "reveal-viewed") in latest
            learner["reveal_served"] = served
            if mine is not None and served:
                reveal = self._reveal_payload(step, self._raw_step(item, step_key)["private"], mine)
        elif t == StepType.CHECK:
            check = self._check_view(user_id, item)
            public["questions"] = check["questions"]
            result = check["result"]
            learner["attempts"] = result["attempts"] if result else 0
            learner["passed"] = bool(result and result["ever_passed"])
            learner["last_attempt_items"] = result["items"] if result else []
            state["passed"] = learner["passed"]
            if learner["passed"]:
                rationales = check["review"]
        elif t in (StepType.LAB, StepType.PRACTICE):
            # AIL.5D.5: the learner's own practice on this step (predictions, runs, notes) so the Professor can coach it. The
            # authored lab answer is never part of it, and ``solution_eligible`` keeps the ladder below "explain".
            from app.services.academy_practice_service import AcademyPracticeService

            learner["practice"] = AcademyPracticeService(self.db, clock=self._clock).professor_state(user_id, item, step_key)
        vocabulary = []
        for s in steps:
            for block in (s["content"].get("blocks") or []):
                if block.get("kind") == "compare" and block["head"][0].lower() == "term":
                    vocabulary.extend({"term": r[0], "definition": r[1]} for r in block["rows"])
        return {
            "item": {"id": item.id, "lineage_id": item.lineage_id, "version": item.version, "title": item.title, "day": item.spec.get("day"),
                     "week": item.spec.get("week"), "curriculum": item.spec.get("curriculum"), "concept_id": item.concept_id},
            "step": public, "step_fingerprint": step["fingerprint"], "learner": learner, "state": state,
            "progress": {"required_completed": progress["required_completed"], "required_total": progress["required_total"],
                         "learning_complete": progress["learning_complete"], "current_step_key": progress["current_step_key"],
                         "completed_keys": [k for k, v in status.items() if v == "completed"], "skipped_keys": [k for k, v in status.items() if v == "skipped"]},
            "outline": [{"position": s["position"], "key": s["key"], "type": s["type"], "title": s["title"], "required": s["required"], "status": status[s["key"]]} for s in steps],
            "vocabulary": vocabulary[:40], "reveal": reveal, "rationales": rationales, "compare_to": compare,
            "prior_hints": len([h for h in self.help_history(user_id, item, step) if h.request_kind.value == "hint"]),
        }

    def _record_lesson_completed_once(self, user_id: str, item: LearningItem) -> None:
        """Earned, not opened: written only when every required step has completed, once per learner and lineage."""
        version_ids = [
            row for row in self.db.execute(select(LearningItem.id).where(LearningItem.lineage_id == item.lineage_id)).scalars()
        ]
        already = self.db.execute(select(LearningEvidence.id).where(
            LearningEvidence.user_id == user_id, LearningEvidence.evidence_type == EvidenceType.LESSON_COMPLETED,
            LearningEvidence.learning_item_id.in_(version_ids),
        )).first()
        concept_version = self._graph.get_current_version(item.concept_id)
        if already is not None or concept_version is None:
            return
        LearningEvidenceService(self.db).record_evidence(
            user_id=user_id, concept_id=item.concept_id, concept_version_id=concept_version.id, learning_item_id=item.id,
            evidence_type=EvidenceType.LESSON_COMPLETED, grader=GradingMode.SELF, passed=True,
        )

    def learning_view(self, user_id: str, item_id: str) -> Dict[str, Any]:
        """The learner-facing read model of one structured Day: ordered PUBLIC steps merged with THIS learner's
        status, current/next derivation, learning completion, and demonstrated. Read-only; never writes; never
        contains ``private``; nothing in it can be supplied by a client."""
        item = self._item(item_id)
        progress = self.progress(user_id, item_id)
        status = {s["key"]: s for s in progress["steps"]}
        steps = []
        all_steps = self._steps(item)
        for step in all_steps:
            s = status[step["key"]]
            view = {k: step[k] for k in ("key", "position", "type", "title", "required", "estimated_minutes", "content", "binding") if k in step}
            view.update(
                status=s["status"], carried_from_version=s["carried_from_version"], open_count=s["open_count"], completed_at=s["completed_at"],
                is_current=step["key"] == progress["current_step_key"], is_next=step["key"] == progress["next_step_key"],
            )
            self._add_learner_view(user_id, item, step, view, all_steps)
            steps.append(view)
        ladder = LearnerStateService(self.db).state(user_id, item.concept_id).ladder
        return {
            "assessment": self.assessment_status(user_id, item), "capstone": self.capstone_progress(user_id, item),
            "structured": True, "item_id": item.id, "lineage_id": item.lineage_id, "version": item.version, "title": item.title,
            "day": item.spec.get("day"), "week": item.spec.get("week"), "kind": item.spec.get("kind"),
            "step_schema_version": STEP_SCHEMA_VERSION, "steps": steps,
            "current_step_key": progress["current_step_key"], "next_step_key": progress["next_step_key"],
            "required_total": progress["required_total"], "required_completed": progress["required_completed"],
            "optional_total": progress["optional_total"], "optional_completed": progress["optional_completed"],
            "learning_complete": progress["learning_complete"], "demonstrated": ladder == "demonstrated", "concept_state": ladder,
            "note": NOT_EVIDENCE,
        }

    def capstone_progress(self, user_id: str, item: LearningItem) -> Optional[Dict[str, Any]]:
        """AIL.5D.6: the learner's OWN Capstone artifacts from the Days before this one, so the project visibly builds across the ten
        Days and survives navigation and sessions (they are ordinary saved step responses, owned by this learner). None off the Capstone."""
        from app.academy_capstone_structure import STAGES, artifact_key
        from app.services.academy_level1_service import AcademyLevel1Service

        spec = item.spec or {}
        day = spec.get("day")
        if not spec.get("capstone_stage") or not isinstance(day, int):
            return None
        by_day = {row.spec["day"]: row for row in AcademyLevel1Service(self.db)._current_academy_items()}
        artifacts = []
        for earlier in range(21, day):
            row = by_day.get(earlier)
            if row is None or not is_structured(row.spec):
                continue
            step = next((s for s in self._steps(row) if s["key"] == artifact_key(earlier)), None)
            if step is None:
                continue
            mine = self._response_view(step, self._latest_responses(user_id, row, step))
            artifacts.append({"day": earlier, "stage": STAGES[earlier].title(), "title": step["title"], "text": mine["text"] if mine else None})
        return {"day": day, "stage": STAGES.get(day), "artifacts": artifacts, "artifact_key": artifact_key(day) if day < 29 else None}

    def assessment_status(self, user_id: str, item: LearningItem) -> Optional[Dict[str, Any]]:
        """AIL.5D.6: where this learner is with the Day's AIL.5C assessment, DERIVED from canonical attempt and result rows
        (never stored, never client-supplied). It is a presentation of AIL.5C, not a second status: ``demonstrated`` stays the
        Concept-level state. ``None`` when the Day is not bound to an assessment."""
        from app.db.enums import AssessmentAttemptStatus, AssessmentOutcome, AssessmentResultKind
        from app.models.assessment import AssessmentAttempt, AssessmentDefinition, AssessmentResult

        spec = item.spec or {}
        key = spec.get("assessment_definition_key") or (spec.get("explain_back") or {}).get("assessment_definition_key")
        if not key or (spec.get("capstone_stage") and spec.get("day") != 29):     # Day 29 is the Capstone's demonstration: its assessment is AIL.5C's
            return None
        attempt = self.db.execute(
            select(AssessmentAttempt).join(AssessmentDefinition, AssessmentDefinition.id == AssessmentAttempt.definition_id)
            .where(AssessmentAttempt.user_id == user_id, AssessmentDefinition.definition_key == key)
            .order_by(AssessmentAttempt.started_at.desc())
        ).scalars().first()
        out: Dict[str, Any] = {"definition_key": key, "status": "not_started", "attempt_id": None, "attempts_note": "Graded separately in the Assessment Center."}
        if attempt is None:
            return out
        out["attempt_id"] = attempt.id
        if attempt.status == AssessmentAttemptStatus.DRAFT:
            out["status"] = "in_progress"
        elif attempt.status == AssessmentAttemptStatus.ABANDONED:
            out["status"] = "not_started"
        elif attempt.status != AssessmentAttemptStatus.FINALIZED:
            out["status"] = "submitted"
        else:
            final = self.db.execute(
                select(AssessmentResult).where(AssessmentResult.attempt_id == attempt.id, AssessmentResult.result_kind.in_([AssessmentResultKind.FINAL, AssessmentResultKind.HUMAN]))
                .order_by(AssessmentResult.seq.desc())
            ).scalars().first()
            outcome = final.outcome if final is not None else None
            out["status"] = {AssessmentOutcome.PASSED: "passed", AssessmentOutcome.NEEDS_WORK: "needs_work"}.get(outcome, "in_review")
        return out

    def _add_learner_view(self, user_id: str, item: LearningItem, step: dict, view: dict, all_steps: List[dict]) -> None:
        """This learner's OWN data for an interactive step. Never the reveal, never an answer key."""
        t = StepType(step["type"])
        if t in (StepType.REFLECT, StepType.REFLECTION, StepType.THINK, StepType.EXPLAIN_BACK):
            mine = self._response_view(step, self._latest_responses(user_id, item, step))
            view["response"] = mine
            if t == StepType.THINK:
                view["committed"] = mine is not None
                view["reveal_available"] = mine is not None
            if t == StepType.REFLECTION and step["content"].get("compare_to"):
                target = next(s for s in all_steps if s["key"] == step["content"]["compare_to"])
                earlier = self._response_view(target, self._latest_responses(user_id, item, target))
                view["compare_to"] = {"key": target["key"], "title": target["title"], "response": earlier}
        elif t == StepType.CHECK:
            check = self._check_view(user_id, item)
            view["questions"], view["check_result"], view["review"] = check["questions"], check["result"], check["review"]
        elif t in (StepType.LAB, StepType.PRACTICE):
            from app.services.academy_practice_service import AcademyPracticeService   # AIL.5D.5: this learner's practice instances (summary only)

            view["practice"] = AcademyPracticeService(self.db, clock=self._clock).for_step(user_id, item.id, step["key"])

    def _one(self, user_id: str, item: LearningItem, step_key: str) -> Dict[str, Any]:
        summary = self.progress(user_id, item.id)
        step = next(s for s in summary["steps"] if s["key"] == step_key)
        return {"step": step, "day": {k: summary[k] for k in ("item_id", "version", "current_step_key", "next_step_key", "required_total", "required_completed", "optional_total", "optional_completed", "learning_complete", "note")}}
