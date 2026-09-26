"""Focused Level 1 Academy executable-layer checks.

These tests use the disposable ``db`` fixture and never touch the persistent
development database.
"""

from app.db.enums import AssistanceLevel, ConceptKind, ConceptLevel, EvidenceType, GradingMode, LearningItemType
from app.models.academy import ProjectMilestone, ProjectTemplate
from app.models.assessment import AssessmentDefinition
from app.models.concepts import LearningItem
from app.services.academy_level1_service import (
    AcademyLevel1Service, CANONICAL_SLUGS, EXISTING_ASSESSMENT_BINDINGS,
    LEVEL1_CAPSTONE_TEMPLATE_KEY, _authored_knowledge_checks, _day_body,
)
from app.services.concept_graph_service import ConceptGraphService
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.learner_state_service import LearnerStateService, EXPOSED, PRACTICED
from app.services.academy_level1_fixtures import ensure_day10, ensure_day14, ensure_day15, ensure_day19, ensure_day20
from app.services.task_service import TaskService
from app.services.workflow_execution_service import WorkflowExecutionService
from tests.ail1a_factories import make_published_version, make_user


def test_authored_knowledge_bank_is_complete_and_does_not_leak_answers():
    expected = {1: 8, 2: 3, 3: 4, 6: 2, 7: 2, 8: 2, 11: 2, 12: 3, 13: 2, 16: 2, 17: 2, 18: 2}
    assert {day: len(_authored_knowledge_checks(day)) for day in expected} == expected
    assert "*Correct:" not in _day_body(2)
    day1 = _authored_knowledge_checks(1)
    assert day1[-8]["prompt"] == "This app uses AI to remind you to drink water every two hours."
    assert day1[-8]["answer"] == {"system": "traditional_software", "claim": "overclaimed"}
    assert "→ (a)" not in _day_body(1)


def test_provision_binds_exactly_28_concepts_and_is_idempotent(db):
    graph = ConceptGraphService(db)
    for slug in CANONICAL_SLUGS:
        concept = graph.create_concept(slug=slug, name=slug, level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept=concept)
    service = AcademyLevel1Service(db)
    user = make_user(db)
    first = service.provision(user)
    second = service.provision(user)
    assert len(first) == 30
    assert second == []
    assert db.query(LearningItem).filter(LearningItem.version == 2).count() == 30
    assert next(row for row in db.query(LearningItem).all() if (row.spec or {}).get("academy_key") == "level1-v2-day-21").concept.slug == "problem-framing-requirements"


def test_opening_lesson_is_exposed_only(db):
    user = make_user(db)
    version = make_published_version(db)
    item = ConceptGraphService(db).create_learning_item(concept_id=version.concept_id, item_type=LearningItemType.RESOURCE, title="lesson", spec={"academy_key": "level1-v2-day-test"})
    evidence = AcademyLevel1Service(db).expose(user.id, item.id)
    assert evidence.evidence_type == EvidenceType.LESSON_COMPLETED
    assert LearnerStateService(db).state(user.id, version.concept_id).ladder == EXPOSED


def test_h5_is_never_practice_and_unknown_is_not_a_qualifying_default(db):
    user = make_user(db)
    version = make_published_version(db)
    service = LearningEvidenceService(db)
    service.record_evidence(user_id=user.id, concept_id=version.concept_id, concept_version_id=version.id, evidence_type=EvidenceType.LAB, grader=GradingMode.DETERMINISTIC, passed=True, assistance_level=AssistanceLevel.H5)
    assert LearnerStateService(db).state(user.id, version.concept_id).ladder != PRACTICED
    assert AssistanceLevel("h5") == AssistanceLevel.H5


def test_lesson_exposure_cannot_satisfy_capstone_graduation(db):
    user = make_user(db)
    graph = ConceptGraphService(db)
    for slug in CANONICAL_SLUGS:
        concept = graph.create_concept(slug=slug, name=slug, level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept=concept)
    service = AcademyLevel1Service(db)
    service.provision(user)
    for row in service._current_academy_items():
        if row.spec["day"] in range(21, 30):
            service.expose(user.id, row.id)
    result = service.graduation(user.id)
    assert result["capstone_complete"] is False
    assert result["eligible"] is False


def test_graduation_requires_canonical_qualified_provenance_and_known_assistance(db):
    user = make_user(db)
    graph = ConceptGraphService(db)
    for slug in CANONICAL_SLUGS:
        concept = graph.create_concept(slug=slug, name=slug, level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept=concept)
    service = AcademyLevel1Service(db)
    service.provision(user)
    items = {row.spec["day"]: row for row in service._current_academy_items()}
    evidence = LearningEvidenceService(db)
    lab_concept = items[4].concept_id
    lab_version = graph.get_current_version(lab_concept)
    evidence.record_evidence(
        user_id=user.id, concept_id=lab_concept, concept_version_id=lab_version.id,
        learning_item_id=items[4].id, evidence_type=EvidenceType.LAB,
        grader=GradingMode.DETERMINISTIC, passed=True,
    )
    assert service.graduation(user.id)["capstone_complete"] is False
    assert 4 in service.graduation(user.id)["missing_days"]


def test_level1_explain_backs_and_capstone_template_are_versioned_and_idempotent(db):
    user = make_user(db)
    graph = ConceptGraphService(db)
    for slug in CANONICAL_SLUGS:
        concept = graph.create_concept(slug=slug, name=slug, level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept=concept)
    service = AcademyLevel1Service(db)
    service.provision(user)
    service.provision(user)
    definitions = db.query(AssessmentDefinition).all()
    assert {d.definition_key for d in definitions} >= set(EXISTING_ASSESSMENT_BINDINGS.values()) - {"eb-structured-output", "capstone-foundations"}
    assert all(d.status.value == "published" for d in definitions if d.definition_key.startswith("level1-day-"))
    template = db.query(ProjectTemplate).filter_by(template_key=LEVEL1_CAPSTONE_TEMPLATE_KEY, version=1).one()
    assert template.capstone_eligible is True
    milestones = db.query(ProjectMilestone).filter_by(project_template_id=template.id).order_by(ProjectMilestone.position).all()
    assert [m.check_spec["academy_day"] for m in milestones] == list(range(21, 31))
    assert len(db.query(ProjectTemplate).filter_by(template_key=LEVEL1_CAPSTONE_TEMPLATE_KEY, version=1).all()) == 1


def test_governed_level1_fixtures_are_server_bound_and_idempotent(db):
    user = make_user(db)
    graph = ConceptGraphService(db)
    for slug in CANONICAL_SLUGS:
        concept = graph.create_concept(slug=slug, name=slug, level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept=concept)
    service = AcademyLevel1Service(db)
    service.provision(user)
    items = {row.spec["day"]: row for row in service._current_academy_items()}
    day10 = ensure_day10(db, user, items[10])
    day14 = ensure_day14(db, user, items[14])
    day15 = ensure_day15(db, user, items[15])
    day19 = ensure_day19(db, user, items[19])
    day20 = ensure_day20(db, user, items[20])
    assert day10["agent_version"].status.value == "active"
    assert day10["task"].requirements["academy_fixture"] == "level1-day10-structured-extraction"
    assert day14["template"].template_key == "level1-day14-small-ai-application"
    assert day15["task"].requirements["not_vector_rag"] is True
    assert day19["workflow_version"].status.value == "active"
    assert day20["task"].requirements["requires_baseline_and_improved"] is True
    assert ensure_day10(db, user, items[10])["agent_version"].id == day10["agent_version"].id
    assert ensure_day19(db, user, items[19])["workflow_version"].id == day19["workflow_version"].id


def test_governed_fixtures_start_real_existing_execution_paths(db):
    user = make_user(db)
    graph = ConceptGraphService(db)
    for slug in CANONICAL_SLUGS:
        concept = graph.create_concept(slug=slug, name=slug, level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept=concept)
    service = AcademyLevel1Service(db)
    service.provision(user)
    items = {row.spec["day"]: row for row in service._current_academy_items()}
    day10 = ensure_day10(db, user, items[10])
    run10 = TaskService(db).start_task_run(task_id=day10["task"].id, agent_version_id=day10["agent_version"].id, enqueue=False)
    assert run10.agent_runs[0].agent_version_id == day10["agent_version"].id
    day14 = ensure_day14(db, user, items[14])
    run14 = TaskService(db).start_task_run(task_id=day14["task"].id, agent_version_id=day14["agent_version"].id, enqueue=False)
    assert run14.agent_runs[0].agent_version_id == day14["agent_version"].id
    day19 = ensure_day19(db, user, items[19])
    workflow_run = WorkflowExecutionService(db).start_workflow_run_from_task(day19["workflow_version"].id, day19["task"].id)
    assert workflow_run.workflow_version_id == day19["workflow_version"].id
