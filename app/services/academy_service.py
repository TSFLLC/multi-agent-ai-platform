"""Deterministic Academy Foundation service.

This service owns curriculum and enrollment lifecycle only.  It delegates
learning state to LearnerStateService and never writes LearningEvidence.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import (
    AcademyEnrollmentStatus,
    AcademyPace,
    AcademyProgramItemKind,
    AcademyProgramVersionStatus,
    ConceptRelationType,
    PlanItemOrigin,
    PlanItemState,
    VersionStatus,
)
from app.academy_curriculum import FOUNDATIONS_PROGRAM
from app.errors import ConflictError, NotFoundError
from app.models.academy import AcademyEnrollment, AcademyProgram, AcademyProgramItem, AcademyProgramVersion
from app.models.concepts import Concept, ConceptRelation, ConceptVersion, LearningItem
from app.models.identity import User
from app.models.learner import LearningEvidence, LearningPlanItem
from app.services.audit_service import AuditService
from app.services.learner_state_service import DEMONSTRATED, EXPOSED, PRACTICED, UNDERSTOOD, LearnerStateService

_LADDER_ORDER = {"not_started": 0, EXPOSED: 1, UNDERSTOOD: 2, PRACTICED: 3, DEMONSTRATED: 4}


@dataclass
class AcademyProgress:
    required_items: int
    completed_items: int
    required_concepts: int
    demonstrated_concepts: int
    practiced_or_better: int
    understood_or_better: int
    exposed: int
    review_due: int
    eligible_next_item_id: Optional[str]
    complete: bool


class AcademyService:
    def __init__(self, db: Session):
        self.db = db
        self.state = LearnerStateService(db)

    # -- Program lifecycle -------------------------------------------------

    def seed_foundations_program(self, user: User) -> AcademyProgramVersion:
        """Create and publish the initial program from the Concept Graph.

        Missing graph concepts are reported instead of being invented.  This
        keeps curriculum seeding honest when content migrations have not yet
        populated a deployment.
        """
        existing = self.db.execute(select(AcademyProgram).where(AcademyProgram.slug == FOUNDATIONS_PROGRAM["slug"])).scalar_one_or_none()
        if existing:
            published = self.db.execute(select(AcademyProgramVersion).where(AcademyProgramVersion.program_id == existing.id, AcademyProgramVersion.status == AcademyProgramVersionStatus.PUBLISHED)).scalar_one_or_none()
            if published:
                return published
            program = existing
        else:
            program = AcademyProgram(slug=FOUNDATIONS_PROGRAM["slug"], title=FOUNDATIONS_PROGRAM["title"], description=FOUNDATIONS_PROGRAM["description"], author_user_id=user.id, status="active")
            self.db.add(program)
            self.db.flush()
        self._can_author(user, program)
        version = AcademyProgramVersion(program_id=program.id, version=max((v.version for v in program.versions), default=0) + 1, status=AcademyProgramVersionStatus.DRAFT, duration_days=FOUNDATIONS_PROGRAM["duration_days"], completion_rules={"required_threshold": PRACTICED})
        self.db.add(version)
        self.db.flush()
        missing = []
        day = 1
        position = 0
        for module_key, _title, slugs in FOUNDATIONS_PROGRAM["modules"]:
            week = int(module_key.split("-")[1])
            for slug in slugs:
                concept = self.db.execute(select(Concept).where(Concept.slug == slug)).scalar_one_or_none()
                if concept is None:
                    missing.append(slug)
                    continue
                self.db.add(AcademyProgramItem(program_version_id=version.id, week=week, day=day, module_key=module_key, position=position, item_kind=AcademyProgramItemKind.CONCEPT, concept_id=concept.id, required=True, estimated_minutes=20, purpose_text=f"Build the foundation needed for the {FOUNDATIONS_PROGRAM['modules'][week - 1][1]} module."))
                position += 1
                day = min(30, day + 1)
        if missing:
            self.db.rollback()
            raise ConflictError("Cannot seed Practical AI Foundations; missing Concept Graph slugs: " + ", ".join(sorted(set(missing))))
        self.db.commit()
        return self.publish_version(user, version.id)

    def create_program(self, user: User, *, slug: str, title: str, description: Optional[str] = None) -> AcademyProgram:
        if self.db.execute(select(AcademyProgram).where(AcademyProgram.slug == slug)).scalar_one_or_none():
            raise ConflictError(f"Program slug {slug!r} already exists")
        program = AcademyProgram(slug=slug, title=title, description=description, author_user_id=user.id, status="active")
        self.db.add(program)
        self.db.flush()
        self._audit(user, "academy.program.created", program.id, {"slug": slug})
        self.db.commit()
        self.db.refresh(program)
        return program

    def create_version(self, user: User, program_id: str, *, duration_days: int, completion_rules: Optional[dict] = None) -> AcademyProgramVersion:
        program = self._program(program_id)
        self._can_author(user, program)
        if duration_days < 1:
            raise ConflictError("Program duration_days must be positive")
        next_version = max((v.version for v in program.versions), default=0) + 1
        version = AcademyProgramVersion(
            program_id=program.id,
            version=next_version,
            status=AcademyProgramVersionStatus.DRAFT,
            duration_days=duration_days,
            completion_rules=completion_rules or {"required_threshold": PRACTICED},
        )
        self.db.add(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def add_item(
        self,
        user: User,
        version_id: str,
        *,
        week: int,
        day: int,
        module_key: str,
        position: int,
        item_kind: AcademyProgramItemKind,
        concept_id: Optional[str] = None,
        learning_item_id: Optional[str] = None,
        required: bool = True,
        estimated_minutes: Optional[int] = None,
        purpose_text: Optional[str] = None,
        completion_requirement: Optional[dict] = None,
    ) -> AcademyProgramItem:
        version = self._version(version_id)
        self._can_author(user, version.program)
        self._draft_only(version)
        concept_id = self._validate_target(item_kind, concept_id, learning_item_id)
        item = AcademyProgramItem(
            program_version_id=version.id,
            week=week,
            day=day,
            module_key=module_key,
            position=position,
            item_kind=item_kind,
            concept_id=concept_id,
            learning_item_id=learning_item_id,
            required=required,
            estimated_minutes=estimated_minutes,
            purpose_text=purpose_text,
            completion_requirement=completion_requirement,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def publish_version(self, user: User, version_id: str) -> AcademyProgramVersion:
        version = self._version(version_id)
        self._can_author(user, version.program)
        self._draft_only(version)
        items = list(self.db.execute(select(AcademyProgramItem).where(AcademyProgramItem.program_version_id == version.id)).scalars())
        if not items:
            raise ConflictError("A Program Version must contain at least one Program Item")
        self._validate_items(version, items)
        now = datetime.now(timezone.utc)
        active = self.db.execute(
            select(AcademyProgramVersion).where(
                AcademyProgramVersion.program_id == version.program_id,
                AcademyProgramVersion.status == AcademyProgramVersionStatus.PUBLISHED,
            )
        ).scalars().all()
        for old in active:
            old.status = AcademyProgramVersionStatus.RETIRED
        version.status = AcademyProgramVersionStatus.PUBLISHED
        version.published_at = now
        self._audit(user, "academy.program_version.published", version.id, {"version": version.version})
        self.db.commit()
        self.db.refresh(version)
        return version

    # -- Enrollment --------------------------------------------------------

    def enroll(self, user: User, version_id: str, *, pace: AcademyPace = AcademyPace.SCHEDULED) -> AcademyEnrollment:
        version = self._version(version_id)
        if version.status != AcademyProgramVersionStatus.PUBLISHED:
            raise ConflictError("Only a published Program Version can be enrolled in")
        existing = self.db.execute(
            select(AcademyEnrollment).where(AcademyEnrollment.user_id == user.id, AcademyEnrollment.program_version_id == version.id)
        ).scalar_one_or_none()
        if existing:
            return existing
        enrollment = AcademyEnrollment(user_id=user.id, program_version_id=version.id, pace=pace, status=AcademyEnrollmentStatus.ACTIVE)
        self.db.add(enrollment)
        self.db.flush()
        self._project_plan(user.id, version)
        self._audit(user, "academy.enrollment.created", enrollment.id, {"program_version_id": version.id})
        self.db.commit()
        self.db.refresh(enrollment)
        return enrollment

    def change_status(self, user: User, enrollment_id: str, status: AcademyEnrollmentStatus) -> AcademyEnrollment:
        enrollment = self._own_enrollment(user.id, enrollment_id)
        if enrollment.status in (AcademyEnrollmentStatus.COMPLETED, AcademyEnrollmentStatus.WITHDRAWN):
            raise ConflictError(f"Enrollment is already {enrollment.status.value}")
        if status == AcademyEnrollmentStatus.COMPLETED:
            progress = self.progress(user.id, enrollment)
            if not progress.complete:
                raise ConflictError("Enrollment completion requirements are not satisfied")
            enrollment.completed_at = datetime.now(timezone.utc)
        enrollment.status = status
        self._audit(user, f"academy.enrollment.{status.value}", enrollment.id, {})
        self.db.commit()
        self.db.refresh(enrollment)
        return enrollment

    # -- Read models and deterministic progression ------------------------

    def progress(self, user_id: str, enrollment: AcademyEnrollment) -> AcademyProgress:
        version = self._version(enrollment.program_version_id)
        items = self._items(version.id)
        required = [i for i in items if i.required]
        concept_ids = sorted({i.concept_id for i in required if i.concept_id})
        states = self.state.states_for_concepts(user_id, concept_ids)
        threshold = version.completion_rules.get("required_threshold", PRACTICED)
        completed_items = sum(1 for i in required if self._item_complete(user_id, i, states, threshold))
        demonstrated = sum(1 for s in states.values() if s.ladder == DEMONSTRATED)
        practiced = sum(1 for s in states.values() if _LADDER_ORDER.get(s.ladder, 0) >= _LADDER_ORDER[PRACTICED])
        understood = sum(1 for s in states.values() if _LADDER_ORDER.get(s.ladder, 0) >= _LADDER_ORDER[UNDERSTOOD])
        exposed = sum(1 for s in states.values() if _LADDER_ORDER.get(s.ladder, 0) >= _LADDER_ORDER[EXPOSED])
        review_due = sum(1 for s in states.values() if "REVIEW_DUE" in s.overlays or "REVIEW_FAILED" in s.overlays)
        next_item = self._next_item(items, states, threshold)
        required_concepts = len(concept_ids)
        complete = completed_items == len(required) and bool(required)
        return AcademyProgress(
            required_items=len(required), completed_items=completed_items,
            required_concepts=required_concepts, demonstrated_concepts=demonstrated,
            practiced_or_better=practiced, understood_or_better=understood,
            exposed=exposed, review_due=review_due,
            eligible_next_item_id=next_item.id if next_item else None, complete=complete,
        )

    def today(self, user_id: str, enrollment: AcademyEnrollment, *, day: Optional[int] = None) -> List[AcademyProgramItem]:
        version = self._version(enrollment.program_version_id)
        if day is None:
            elapsed = (datetime.now(timezone.utc).date() - enrollment.started_at.date()).days
            day = max(1, min(version.duration_days, elapsed + 1))
        return list(self.db.execute(
            select(AcademyProgramItem).where(AcademyProgramItem.program_version_id == version.id, AcademyProgramItem.day == day)
            .order_by(AcademyProgramItem.position)
        ).scalars())

    def _project_plan(self, user_id: str, version: AcademyProgramVersion) -> None:
        items = self._items(version.id)
        existing = self.db.execute(select(LearningPlanItem).where(LearningPlanItem.user_id == user_id)).scalars().all()
        existing_ids = {i.concept_id for i in existing if i.state in (PlanItemState.PROPOSED, PlanItemState.PLANNED)}
        position = max((i.position for i in existing), default=-1) + 1
        for item in items:
            if item.concept_id is None or item.concept_id in existing_ids:
                continue
            self.db.add(LearningPlanItem(user_id=user_id, concept_id=item.concept_id, position=position, state=PlanItemState.PLANNED, origin=PlanItemOrigin.PROGRAM))
            existing_ids.add(item.concept_id)
            position += 1

    def _item_complete(self, user_id: str, item: AcademyProgramItem, states: dict, default_threshold: str) -> bool:
        if item.concept_id:
            threshold = (item.completion_requirement or {}).get("state", default_threshold)
            return _LADDER_ORDER.get(states[item.concept_id].ladder, 0) >= _LADDER_ORDER.get(threshold, _LADDER_ORDER[default_threshold])
        return self.db.execute(select(LearningEvidence.id).where(
            LearningEvidence.user_id == user_id,
            LearningEvidence.learning_item_id == item.learning_item_id,
            LearningEvidence.passed.is_(True),
        )).first() is not None

    def _next_item(self, items, states, threshold):
        for item in items:
            if not item.required:
                continue
            if not self._item_concept_ready(item, states):
                continue
            if item.concept_id:
                current = _LADDER_ORDER.get(states.get(item.concept_id).ladder, 0) if states.get(item.concept_id) else 0
                required = _LADDER_ORDER.get((item.completion_requirement or {}).get("state", threshold), _LADDER_ORDER[threshold])
                if current < required:
                    return item
            else:
                return item
        return None

    def _item_concept_ready(self, item, states):
        if not item.concept_id:
            return True
        prereqs = self.db.execute(select(ConceptRelation.from_concept_id).where(
            ConceptRelation.to_concept_id == item.concept_id,
            ConceptRelation.relation_type == ConceptRelationType.PREREQUISITE,
        )).scalars().all()
        return all(_LADDER_ORDER.get(states.get(p).ladder, 0) >= _LADDER_ORDER[UNDERSTOOD] for p in prereqs)

    # -- Validation / access helpers --------------------------------------

    def _validate_items(self, version, items):
        concept_ids = {i.concept_id for i in items if i.concept_id}
        # A frozen curriculum may intentionally revisit a concept later.  A
        # prerequisite is satisfied by the earliest occurrence, not replaced
        # by the later review occurrence.
        positions = {}
        for item in items:
            if item.concept_id:
                position = (item.day, item.position)
                positions[item.concept_id] = min(positions.get(item.concept_id, position), position)
        for item in items:
            if item.concept_id:
                current = self.db.get(Concept, item.concept_id)
                active = self.db.execute(select(ConceptVersion).where(ConceptVersion.concept_id == item.concept_id, ConceptVersion.status == VersionStatus.ACTIVE)).first()
                if current is None or active is None:
                    raise ConflictError(f"Program Item {item.id} references a Concept without an active Concept Version")
                for prereq in self.db.execute(select(ConceptRelation.from_concept_id).where(ConceptRelation.to_concept_id == item.concept_id, ConceptRelation.relation_type == ConceptRelationType.PREREQUISITE)).scalars():
                    if prereq in positions and positions[prereq] > positions[item.concept_id]:
                        raise ConflictError(f"Prerequisite {prereq} is scheduled after {item.concept_id}")
            if item.learning_item_id:
                learning_item = self.db.get(LearningItem, item.learning_item_id)
                if learning_item is None or learning_item.concept_id != item.concept_id:
                    raise ConflictError(f"Program Item {item.id} has an invalid Learning Item reference")

    def _validate_target(self, item_kind, concept_id, learning_item_id):
        if concept_id is None and learning_item_id is None:
            raise ConflictError("Program Item must reference a Concept or Learning Item")
        learning_item = self.db.get(LearningItem, learning_item_id) if learning_item_id else None
        if learning_item_id and learning_item is None:
            raise NotFoundError(f"LearningItem {learning_item_id} not found")
        if concept_id and self.db.get(Concept, concept_id) is None:
            raise NotFoundError(f"Concept {concept_id} not found")
        if learning_item and concept_id and learning_item.concept_id != concept_id:
            raise ConflictError("Learning Item and Concept references must agree")
        if learning_item and concept_id is None:
            concept_id = learning_item.concept_id
        return concept_id

    def _project(self, project_id):
        program = self.db.get(AcademyProgram, project_id)
        if program is None:
            raise NotFoundError(f"Program {project_id} not found")
        return program

    def _program(self, program_id):
        return self._project(program_id)

    def _version(self, version_id):
        version = self.db.get(AcademyProgramVersion, version_id)
        if version is None:
            raise NotFoundError(f"Program Version {version_id} not found")
        return version

    def _own_enrollment(self, user_id, enrollment_id):
        enrollment = self.db.get(AcademyEnrollment, enrollment_id)
        if enrollment is None or enrollment.user_id != user_id:
            raise NotFoundError(f"Enrollment {enrollment_id} not found")
        return enrollment

    def _items(self, version_id):
        return list(self.db.execute(select(AcademyProgramItem).where(AcademyProgramItem.program_version_id == version_id).order_by(AcademyProgramItem.day, AcademyProgramItem.position)).scalars())

    @staticmethod
    def _draft_only(version):
        if version.status != AcademyProgramVersionStatus.DRAFT:
            raise ConflictError("Published Program Versions are immutable; create a new version")

    @staticmethod
    def _can_author(user, program):
        if program.author_user_id != user.id and user.role.value not in ("owner", "admin"):
            raise NotFoundError(f"Program {program.id} not found")

    def _audit(self, user, event_type, target_ref, detail):
        AuditService(self.db).record(org_id=user.org_id, actor_user_id=user.id, event_type=event_type, target_ref=target_ref, detail=detail, commit=False)


def _value(value):
    return value.value if hasattr(value, "value") else value
