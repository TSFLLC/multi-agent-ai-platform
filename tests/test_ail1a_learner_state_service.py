from app.db.enums import (
    ConceptKind,
    EvidenceRefType,
    EvidenceType,
    GradingMode,
    QuestionOrigin,
)
from app.services.learner_state_service import (
    CHANGED,
    DEMONSTRATED,
    EXPOSED,
    NOT_STARTED,
    PRACTICED,
    SELF_REPORTED,
    UNDERSTOOD,
    LearnerStateService,
)
from app.services.learning_evidence_service import LearningEvidenceService
from tests.ail1a_factories import make_concept, make_published_version, make_user


def test_no_evidence_is_not_started(db):
    user = make_user(db)
    version = make_published_version(db)
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == NOT_STARTED


def test_lesson_completed_reaches_exposed_only(db):
    user = make_user(db)
    version = make_published_version(db)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.LESSON_COMPLETED,
        grader=GradingMode.DETERMINISTIC,
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == EXPOSED, "exposure must never read as knowledge"


def test_passed_knowledge_check_reaches_understood(db):
    user = make_user(db)
    version = make_published_version(db)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.DETERMINISTIC,
        passed=True,
        question_origin=QuestionOrigin.REVIEWED,
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == UNDERSTOOD


def test_failed_knowledge_check_does_not_advance(db):
    user = make_user(db)
    version = make_published_version(db)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.DETERMINISTIC,
        passed=False,
        question_origin=QuestionOrigin.REVIEWED,
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == NOT_STARTED


def test_observation_reaches_practiced_without_understood(db):
    """PRACTICED does not require UNDERSTOOD first (spec Sec 18.2)."""
    user = make_user(db)
    version = make_published_version(db)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.OBSERVATION,
        grader=GradingMode.DETERMINISTIC,
        passed=True,
        ref_type=EvidenceRefType.MODEL_ROUTING_DECISION,
        ref_id="fake-decision-id",
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == PRACTICED


def test_generated_question_alone_cannot_reach_demonstrated(db):
    """Spec Sec 18.4 rule 2: generated/unreviewed questions establish
    UNDERSTOOD only, never DEMONSTRATED."""
    user = make_user(db)
    version = make_published_version(db, kind=ConceptKind.DEFINITIONAL)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.DETERMINISTIC,
        passed=True,
        question_origin=QuestionOrigin.GENERATED,
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == UNDERSTOOD


def test_definitional_concept_demonstrated_from_reviewed_check(db):
    """Definitional's rule is check + scenario question -- two passed,
    reviewed knowledge_check rows (see
    app.services.learner_state_service.default_evidence_requirements)."""
    user = make_user(db)
    version = make_published_version(db, kind=ConceptKind.DEFINITIONAL)
    evidence = LearningEvidenceService(db)
    common = {
        "user_id": user.id,
        "concept_id": version.concept_id,
        "concept_version_id": version.id,
        "evidence_type": EvidenceType.KNOWLEDGE_CHECK,
        "grader": GradingMode.DETERMINISTIC,
        "passed": True,
        "question_origin": QuestionOrigin.REVIEWED,
    }
    evidence.record_evidence(**common)
    state_after_one = LearnerStateService(db).state(user.id, version.concept_id)
    assert state_after_one.ladder == UNDERSTOOD

    evidence.record_evidence(**common)
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == DEMONSTRATED


def test_ai_only_evidence_cannot_reach_demonstrated(db):
    """Spec Sec 18.4 rule 1: DEMONSTRATED is impossible on AI-graded
    evidence alone, even when every requirement entry is individually
    satisfied."""
    user = make_user(db)
    version = make_published_version(db, kind=ConceptKind.MECHANISM)
    evidence = LearningEvidenceService(db)
    evidence.record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.AI_RUBRIC,
        passed=True,
        question_origin=QuestionOrigin.REVIEWED,
    )
    evidence.record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.INTERPRETATION,
        grader=GradingMode.AI_RUBRIC,
        passed=True,
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder != DEMONSTRATED
    assert state.ladder == UNDERSTOOD


def test_mechanism_concept_demonstrated_with_one_deterministic_leg(db):
    """Mechanism requires knowledge_check AND (lab OR interpretation) --
    with the knowledge check itself deterministic, rule 1 is satisfied
    even though the second leg is AI-graded."""
    user = make_user(db)
    version = make_published_version(db, kind=ConceptKind.MECHANISM)
    evidence = LearningEvidenceService(db)
    evidence.record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.DETERMINISTIC,
        passed=True,
        question_origin=QuestionOrigin.REVIEWED,
    )
    evidence.record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.INTERPRETATION,
        grader=GradingMode.AI_RUBRIC,
        passed=True,
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == DEMONSTRATED


def test_operational_concept_requires_all_four_legs(db):
    user = make_user(db)
    version = make_published_version(db, kind=ConceptKind.OPERATIONAL)
    evidence = LearningEvidenceService(db)
    common = {
        "user_id": user.id,
        "concept_id": version.concept_id,
        "concept_version_id": version.id,
        "passed": True,
    }
    evidence.record_evidence(
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.DETERMINISTIC,
        question_origin=QuestionOrigin.REVIEWED,
        **common,
    )
    evidence.record_evidence(
        evidence_type=EvidenceType.OBSERVATION, grader=GradingMode.DETERMINISTIC, **common
    )
    state_before_lab = LearnerStateService(db).state(user.id, version.concept_id)
    assert state_before_lab.ladder == PRACTICED

    evidence.record_evidence(evidence_type=EvidenceType.LAB, grader=GradingMode.DETERMINISTIC, **common)
    evidence.record_evidence(
        evidence_type=EvidenceType.INTERPRETATION, grader=GradingMode.AI_RUBRIC, **common
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == DEMONSTRATED


def test_self_reported_never_advances_ladder(db):
    user = make_user(db)
    version = make_published_version(db)
    LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.SELF_REPORT,
        grader=GradingMode.SELF,
        passed=True,
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == NOT_STARTED
    assert SELF_REPORTED in state.overlays


def test_self_reported_alongside_real_evidence_is_informational_only(db):
    user = make_user(db)
    version = make_published_version(db, kind=ConceptKind.DEFINITIONAL)
    evidence = LearningEvidenceService(db)
    check = {
        "user_id": user.id,
        "concept_id": version.concept_id,
        "concept_version_id": version.id,
        "evidence_type": EvidenceType.KNOWLEDGE_CHECK,
        "grader": GradingMode.DETERMINISTIC,
        "passed": True,
        "question_origin": QuestionOrigin.REVIEWED,
    }
    evidence.record_evidence(**check)
    evidence.record_evidence(**check)
    evidence.record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.SELF_REPORT,
        grader=GradingMode.SELF,
        passed=True,
    )
    state = LearnerStateService(db).state(user.id, version.concept_id)
    assert state.ladder == DEMONSTRATED
    assert SELF_REPORTED in state.overlays


def test_changed_overlay_on_material_new_version(db):
    from app.services.concept_graph_service import ConceptGraphService

    user = make_user(db)
    concept = make_concept(db, kind=ConceptKind.DEFINITIONAL)
    graph = ConceptGraphService(db)
    v1 = graph.create_draft_version(concept_id=concept.id, plain_definition="v1")
    graph.publish_version(v1.id)

    check = {
        "user_id": user.id,
        "concept_id": concept.id,
        "concept_version_id": v1.id,
        "evidence_type": EvidenceType.KNOWLEDGE_CHECK,
        "grader": GradingMode.DETERMINISTIC,
        "passed": True,
        "question_origin": QuestionOrigin.REVIEWED,
    }
    evidence_service = LearningEvidenceService(db)
    evidence_service.record_evidence(**check)
    evidence_service.record_evidence(**check)
    state = LearnerStateService(db).state(user.id, concept.id)
    assert state.ladder == DEMONSTRATED
    assert CHANGED not in state.overlays

    v2 = graph.create_draft_version(
        concept_id=concept.id, plain_definition="v2, materially different", change_severity="material"
    )
    graph.publish_version(v2.id)

    state_after = LearnerStateService(db).state(user.id, concept.id)
    assert (
        state_after.ladder == DEMONSTRATED
    ), "DEMONSTRATED is not revoked by a material change (spec rule 5)"
    assert CHANGED in state_after.overlays


def test_minor_version_change_preserves_demonstrated_without_changed_overlay(db):
    from app.services.concept_graph_service import ConceptGraphService

    user = make_user(db)
    concept = make_concept(db, kind=ConceptKind.DEFINITIONAL)
    graph = ConceptGraphService(db)
    v1 = graph.create_draft_version(concept_id=concept.id, plain_definition="v1")
    graph.publish_version(v1.id)

    check = {
        "user_id": user.id,
        "concept_id": concept.id,
        "concept_version_id": v1.id,
        "evidence_type": EvidenceType.KNOWLEDGE_CHECK,
        "grader": GradingMode.DETERMINISTIC,
        "passed": True,
        "question_origin": QuestionOrigin.REVIEWED,
    }
    evidence_service = LearningEvidenceService(db)
    evidence_service.record_evidence(**check)
    evidence_service.record_evidence(**check)
    v2 = graph.create_draft_version(
        concept_id=concept.id, plain_definition="v2, typo fix", change_severity="minor"
    )
    graph.publish_version(v2.id)

    state = LearnerStateService(db).state(user.id, concept.id)
    assert state.ladder == DEMONSTRATED
    assert CHANGED not in state.overlays
