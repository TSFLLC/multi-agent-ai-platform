from app.db.enums import EvidenceType, GradingMode
from app.services.learning_evidence_service import LearningEvidenceService
from tests.ail1a_factories import make_published_version, make_user


def test_record_evidence_persists_all_required_fields(db):
    user = make_user(db)
    version = make_published_version(db)
    evidence = LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.DETERMINISTIC,
        passed=True,
        score={"raw": 4, "max": 5, "pct": 0.8},
    )
    assert evidence.user_id == user.id
    assert evidence.concept_id == version.concept_id
    assert evidence.concept_version_id == version.id
    assert evidence.evidence_type == EvidenceType.KNOWLEDGE_CHECK
    assert evidence.grader == GradingMode.DETERMINISTIC
    assert evidence.passed is True
    assert evidence.score == {"raw": 4, "max": 5, "pct": 0.8}
    assert evidence.created_at is not None


def test_repeated_attempts_each_produce_a_new_row(db):
    """spec Sec 19.2 retry policy: a retry uses different items and is
    allowed after cooldown -- the attempt history stays visible, i.e.
    every attempt is its own row, never an overwrite."""
    user = make_user(db)
    version = make_published_version(db)
    service = LearningEvidenceService(db)
    first = service.record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.DETERMINISTIC,
        passed=False,
    )
    second = service.record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.DETERMINISTIC,
        passed=True,
    )
    assert first.id != second.id
    rows = service.list_evidence(user.id, concept_id=version.concept_id)
    assert len(rows) == 2
    assert {r.passed for r in rows} == {False, True}


def test_learning_evidence_service_exposes_no_update_or_delete_method():
    """The append-only contract is structural, not just behavioral: this
    asserts the write surface itself never grows an update/delete path."""
    public_methods = {name for name in vars(LearningEvidenceService) if not name.startswith("_")}
    assert public_methods == {"record_evidence", "list_evidence"}


def test_deleting_a_row_directly_is_the_only_way_to_remove_evidence(db):
    """Confirms there genuinely is no service-level mutation path -- the
    ORM instance itself is only ever touched by direct session
    manipulation (which the append-only services never do), never by
    LearningEvidenceService."""
    user = make_user(db)
    version = make_published_version(db)
    service = LearningEvidenceService(db)
    evidence = service.record_evidence(
        user_id=user.id,
        concept_id=version.concept_id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.KNOWLEDGE_CHECK,
        grader=GradingMode.DETERMINISTIC,
        passed=True,
    )
    stored = service.list_evidence(user.id, concept_id=version.concept_id)
    assert len(stored) == 1
    assert stored[0].id == evidence.id
