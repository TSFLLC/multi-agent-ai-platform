"""AIL.5A Academy Foundation API."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.authz import require_platform_admin
from app.db.enums import AcademyEnrollmentStatus, ConceptRelationType
from app.models.academy import AcademyEnrollment, AcademyProgram, AcademyProgramItem, AcademyProgramVersion
from app.models.identity import User
from app.models.concepts import Concept, ConceptRelation, LearningItem
from app.schemas.academy import (
    AcademyEnrollmentCreate,
    AcademyEnrollmentDetailRead,
    AcademyEnrollmentRead,
    AcademyLessonItemRead,
    AcademyLessonRead,
    AcademyLessonReviewRead,
    AcademyItemCreate,
    AcademyProgramItemRead,
    AcademyProgramRead,
    AcademyProgressRead,
    AcademyProgramVersionRead,
    AcademyProgramCreate,
    AcademyTodayRead,
    AcademyVersionCreate,
)
from app.services.academy_service import AcademyProgress, AcademyService
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_state_service import LearnerStateService
from app.services.practical_ai_foundations_provisioning import provision_practical_ai_foundations

router = APIRouter(prefix="/academy", tags=["academy"])


def _enrollment_read(row):
    return AcademyEnrollmentRead.model_validate(row, from_attributes=True)


def _progress_read(progress: AcademyProgress):
    return AcademyProgressRead(
        required_items=progress.required_items,
        completed_items=progress.completed_items,
        required_concepts=progress.required_concepts,
        demonstrated_concepts=progress.demonstrated_concepts,
        practiced_or_better=progress.practiced_or_better,
        understood_or_better=progress.understood_or_better,
        exposed=progress.exposed,
        review_due=progress.review_due,
        next_item_id=progress.eligible_next_item_id,
        complete=progress.complete,
    )


def _item_read(item, *, user_id: Optional[str] = None, db: Optional[Session] = None):
    state = None
    overlays = []
    eligible = None
    if user_id and db and item.concept_id:
        state = LearnerStateService(db).state(user_id, item.concept_id)
        overlays = sorted(state.overlays)
        prerequisite_ids = db.execute(
            select(ConceptRelation.from_concept_id).where(
                ConceptRelation.to_concept_id == item.concept_id,
                ConceptRelation.relation_type == ConceptRelationType.PREREQUISITE,
            )
        ).scalars().all()
        prerequisite_states = LearnerStateService(db).states_for_concepts(user_id, prerequisite_ids)
        eligible = all(prerequisite_states.get(prerequisite_id) and prerequisite_states[prerequisite_id].ladder in ("understood", "practiced", "demonstrated") for prerequisite_id in prerequisite_ids)
    title = None
    if db and item.learning_item_id:
        learning_item = db.get(LearningItem, item.learning_item_id)
        title = learning_item.title if learning_item else None
    if db and title is None and item.concept_id:
        concept = db.get(Concept, item.concept_id)
        title = concept.name if concept else None
    return AcademyProgramItemRead(
        id=item.id,
        title=title,
        week=item.week,
        day=item.day,
        module_key=item.module_key,
        position=item.position,
        item_kind=item.item_kind,
        concept_id=item.concept_id,
        learning_item_id=item.learning_item_id,
        required=item.required,
        estimated_minutes=item.estimated_minutes,
        purpose_text=item.purpose_text,
        completion_requirement=item.completion_requirement,
        state=state.ladder if state else None,
        overlays=overlays,
        eligible=eligible,
    )


def _version_read(version, *, user_id=None, db=None):
    items = db.execute(select(AcademyProgramItem).where(AcademyProgramItem.program_version_id == version.id).order_by(AcademyProgramItem.day, AcademyProgramItem.position)).scalars().all() if db else []
    return AcademyProgramVersionRead(
        id=version.id,
        program_id=version.program_id,
        version=version.version,
        status=version.status,
        duration_days=version.duration_days,
        completion_rules=version.completion_rules,
        published_at=version.published_at,
        items=[_item_read(item, user_id=user_id, db=db) for item in items],
    )


def _program_read(program, *, user_id=None, db=None):
    versions = db.execute(select(AcademyProgramVersion).where(AcademyProgramVersion.program_id == program.id).order_by(AcademyProgramVersion.version)).scalars().all() if db else []
    return AcademyProgramRead(
        id=program.id,
        slug=program.slug,
        title=program.title,
        description=program.description,
        status=program.status,
        author_user_id=program.author_user_id,
        versions=[_version_read(v, user_id=user_id, db=db) for v in versions],
    )


def _own_enrollment(db, user_id, enrollment_id):
    return AcademyService(db)._own_enrollment(user_id, enrollment_id)


@router.get("/programs", response_model=List[AcademyProgramRead])
def list_programs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    programs = db.execute(select(AcademyProgram).where(AcademyProgram.status == "active").order_by(AcademyProgram.title)).scalars().all()
    return [_program_read(program, db=db) for program in programs]


@router.post("/programs", response_model=AcademyProgramRead, status_code=201)
def create_program(body: AcademyProgramCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _program_read(AcademyService(db).create_program(user, slug=body.slug, title=body.title, description=body.description), db=db)


@router.post("/programs/seed-foundations", response_model=AcademyProgramVersionRead, status_code=201)
def seed_foundations(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    version = AcademyService(db).seed_foundations_program(user)
    return _version_read(version, db=db)


@router.post("/concept-graph/seed-practical-ai-foundations")
def seed_practical_ai_foundations_concept_graph(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_platform_admin),
):
    """Owner/admin-only additive seed for the approved authored graph."""
    return provision_practical_ai_foundations(db).as_dict()


@router.get("/programs/{program_id}", response_model=AcademyProgramRead)
def get_program(program_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _program_read(AcademyService(db)._program(program_id), db=db)


@router.get("/concepts/{concept_id}/lesson", response_model=AcademyLessonRead)
def get_concept_lesson(concept_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Read the active authored lesson projection for one Concept.

    This route deliberately has no write path: resolving content and learner
    state must not create evidence, reviews, or progress transitions.
    """
    graph = ConceptGraphService(db)
    concept = graph.get_concept(concept_id)
    version = graph.get_current_version(concept_id)
    if concept is None or version is None:
        from app.errors import NotFoundError

        raise NotFoundError(f"Concept {concept_id} not found")

    state = LearnerStateService(db).state(user.id, concept.id)
    prerequisite_ids = graph.get_prerequisites(concept.id)
    prerequisite_states = LearnerStateService(db).states_for_concepts(user.id, prerequisite_ids)
    unmet = [
        prerequisite_id
        for prerequisite_id in prerequisite_ids
        if not prerequisite_states[prerequisite_id].is_at_least("understood")
    ]
    review = state.review
    items = sorted(graph.list_learning_items(concept.id), key=lambda item: item.id)

    return AcademyLessonRead(
        concept_id=concept.id,
        slug=concept.slug,
        name=concept.name,
        level=concept.level.value,
        kind=concept.kind.value,
        concept_version_id=version.id,
        concept_version=version.version,
        plain_definition=version.plain_definition,
        technical_explanation=version.technical_explanation,
        examples_md=version.examples_md,
        learning_items=[
            AcademyLessonItemRead(
                id=item.id,
                item_type=item.item_type.value,
                title=item.title,
                body_md=item.body_md,
                grading_mode=item.grading_mode.value if item.grading_mode else None,
                reviewed=item.reviewed,
                version=item.version,
                est_minutes=item.est_minutes,
            )
            for item in items
        ],
        learner_state=state.ladder,
        review_overlays=sorted(state.overlays),
        review=AcademyLessonReviewRead(
            eligible=bool(review and review.eligible),
            due=bool(review and review.due),
            failed=bool(review and review.failed),
            demonstrated=state.ladder == "demonstrated",
            due_reasons=list(review.due_reasons) if review else [],
        ),
        prerequisite_eligible=not unmet,
        unmet_prerequisite_count=len(unmet),
        evidence_count=len(state.evidence),
        passed_evidence_count=sum(1 for evidence in state.evidence if evidence.passed),
    )


@router.post("/programs/{program_id}/versions", response_model=AcademyProgramVersionRead, status_code=201)
def create_version(program_id: str, body: AcademyVersionCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    version = AcademyService(db).create_version(user, program_id, duration_days=body.duration_days, completion_rules=body.completion_rules)
    return _version_read(version, db=db)


@router.post("/program-versions/{version_id}/items", response_model=AcademyProgramItemRead, status_code=201)
def create_item(version_id: str, body: AcademyItemCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = AcademyService(db).add_item(user, version_id, **body.model_dump())
    return _item_read(item)


@router.post("/program-versions/{version_id}/publish", response_model=AcademyProgramVersionRead)
def publish_version(version_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    version = AcademyService(db).publish_version(user, version_id)
    return _version_read(version, db=db)


@router.post("/program-versions/{version_id}/enroll", response_model=AcademyEnrollmentDetailRead, status_code=201)
def enroll(version_id: str, body: AcademyEnrollmentCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AcademyService(db)
    enrollment = service.enroll(user, version_id, pace=body.pace)
    return _detail(service, user.id, enrollment)


@router.get("/enrollments", response_model=List[AcademyEnrollmentRead])
def list_enrollments(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.execute(select(AcademyEnrollment).where(AcademyEnrollment.user_id == user.id).order_by(AcademyEnrollment.created_at.desc())).scalars().all()
    return [_enrollment_read(row) for row in rows]


@router.get("/enrollments/{enrollment_id}", response_model=AcademyEnrollmentDetailRead)
def get_enrollment(enrollment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AcademyService(db)
    return _detail(service, user.id, service._own_enrollment(user.id, enrollment_id))


@router.get("/enrollments/{enrollment_id}/today", response_model=AcademyTodayRead)
def get_today(enrollment_id: str, day: Optional[int] = Query(None, ge=1), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AcademyService(db)
    enrollment = service._own_enrollment(user.id, enrollment_id)
    items = service.today(user.id, enrollment, day=day)
    actual_day = day if day is not None else max(1, min(enrollment.program_version_id and service._version(enrollment.program_version_id).duration_days, (items[0].day if items else 1)))
    return AcademyTodayRead(enrollment=_enrollment_read(enrollment), day=actual_day, items=[_item_read(i, user_id=user.id, db=db) for i in items], progress=_progress_read(service.progress(user.id, enrollment)))


@router.get("/enrollments/{enrollment_id}/progress", response_model=AcademyProgressRead)
def get_progress(enrollment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = AcademyService(db)
    enrollment = service._own_enrollment(user.id, enrollment_id)
    return _progress_read(service.progress(user.id, enrollment))


@router.post("/enrollments/{enrollment_id}/pause", response_model=AcademyEnrollmentRead)
def pause(enrollment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _enrollment_read(AcademyService(db).change_status(user, enrollment_id, AcademyEnrollmentStatus.PAUSED))


@router.post("/enrollments/{enrollment_id}/resume", response_model=AcademyEnrollmentRead)
def resume(enrollment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _enrollment_read(AcademyService(db).change_status(user, enrollment_id, AcademyEnrollmentStatus.ACTIVE))


@router.post("/enrollments/{enrollment_id}/withdraw", response_model=AcademyEnrollmentRead)
def withdraw(enrollment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _enrollment_read(AcademyService(db).change_status(user, enrollment_id, AcademyEnrollmentStatus.WITHDRAWN))


@router.post("/enrollments/{enrollment_id}/complete", response_model=AcademyEnrollmentRead)
def complete(enrollment_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _enrollment_read(AcademyService(db).change_status(user, enrollment_id, AcademyEnrollmentStatus.COMPLETED))


def _detail(service, user_id, enrollment):
    version = service._version(enrollment.program_version_id)
    program = service._program(version.program_id)
    return AcademyEnrollmentDetailRead(
        enrollment=_enrollment_read(enrollment),
        program=_program_read(program, user_id=user_id, db=service.db),
        progress=_progress_read(service.progress(user_id, enrollment)),
    )
