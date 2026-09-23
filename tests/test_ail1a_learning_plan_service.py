from app.db.enums import (
    ConceptKind,
    ConceptLevel,
    ConceptRelationType,
    EvidenceType,
    GradingMode,
    LearnerDepth,
    PlanItemState,
    QuestionOrigin,
)
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_profile_service import LearnerProfileService
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.learning_plan_service import LearningPlanService
from tests.ail1a_factories import make_concept, make_user


def _demonstrate(db, user, concept, version):
    evidence = LearningEvidenceService(db)
    common = {
        "user_id": user.id,
        "concept_id": concept.id,
        "concept_version_id": version.id,
        "evidence_type": EvidenceType.KNOWLEDGE_CHECK,
        "grader": GradingMode.DETERMINISTIC,
        "passed": True,
        "question_origin": QuestionOrigin.REVIEWED,
    }
    evidence.record_evidence(**common)
    evidence.record_evidence(**common)


def test_plan_orders_prerequisites_before_dependents(db):
    graph = ConceptGraphService(db)
    tokens = make_concept(db, slug="tokens", name="Tokens", level=ConceptLevel.FOUNDATIONAL)
    context = make_concept(
        db, slug="context-windows", name="Context Windows", level=ConceptLevel.FOUNDATIONAL
    )
    graph.add_relation(
        from_concept_id=tokens.id, to_concept_id=context.id, relation_type=ConceptRelationType.PREREQUISITE
    )

    user = make_user(db)
    LearnerProfileService(db).set_interest(user.id, concept_id=context.id)

    plan = LearningPlanService(db).generate_plan(user.id)
    ids_in_order = [p.concept_id for p in plan]
    assert ids_in_order.index(tokens.id) < ids_in_order.index(context.id)


def test_plan_generation_is_deterministic(db):
    graph = ConceptGraphService(db)
    a = make_concept(db, slug="a", name="A", level=ConceptLevel.FOUNDATIONAL)
    b = make_concept(db, slug="b", name="B", level=ConceptLevel.FOUNDATIONAL)
    graph.add_relation(
        from_concept_id=a.id, to_concept_id=b.id, relation_type=ConceptRelationType.PREREQUISITE
    )

    user = make_user(db)
    LearnerProfileService(db).set_interest(user.id, concept_id=b.id)

    plan_service = LearningPlanService(db)
    first = [p.concept_id for p in plan_service.generate_plan(user.id)]
    second = [p.concept_id for p in plan_service.generate_plan(user.id)]
    assert first == second


def test_demonstrated_concepts_are_excluded_from_the_plan(db):
    graph = ConceptGraphService(db)
    concept = make_concept(db, slug="tokens", kind=ConceptKind.DEFINITIONAL, level=ConceptLevel.FOUNDATIONAL)
    version = graph.create_draft_version(concept_id=concept.id, plain_definition="v1")
    graph.publish_version(version.id)

    user = make_user(db)
    LearnerProfileService(db).set_interest(user.id, concept_id=concept.id)
    _demonstrate(db, user, concept, version)

    plan = LearningPlanService(db).generate_plan(user.id)
    assert concept.id not in {p.concept_id for p in plan}


def test_survey_depth_excludes_advanced_concepts(db):
    advanced = make_concept(db, slug="quantization", name="Quantization", level=ConceptLevel.ADVANCED)
    user = make_user(db)
    LearnerProfileService(db).update_profile(user.id, depth=LearnerDepth.SURVEY)
    LearnerProfileService(db).set_interest(user.id, concept_id=advanced.id)

    plan = LearningPlanService(db).generate_plan(user.id)
    assert advanced.id not in {p.concept_id for p in plan}


def test_deep_depth_includes_advanced_concepts(db):
    advanced = make_concept(db, slug="quantization", name="Quantization", level=ConceptLevel.ADVANCED)
    user = make_user(db)
    LearnerProfileService(db).update_profile(user.id, depth=LearnerDepth.DEEP)
    LearnerProfileService(db).set_interest(user.id, concept_id=advanced.id)

    plan = LearningPlanService(db).generate_plan(user.id)
    assert advanced.id in {p.concept_id for p in plan}


def test_replan_writes_proposed_rows_not_planned(db):
    concept = make_concept(db, slug="tokens", level=ConceptLevel.FOUNDATIONAL)
    user = make_user(db)
    LearnerProfileService(db).set_interest(user.id, concept_id=concept.id)

    plan_service = LearningPlanService(db)
    diff = plan_service.replan(user.id)
    assert concept.id in diff.added
    assert len(diff.new_items) == 1
    assert diff.new_items[0].state == PlanItemState.PROPOSED

    # A proposal is not yet "the plan" until accepted.
    assert plan_service.get_plan(user.id) == []


def test_accepted_plan_items_survive_replan(db):
    """spec Sec 17.4: the planner never overwrites user edits."""
    concept = make_concept(db, slug="tokens", level=ConceptLevel.FOUNDATIONAL)
    user = make_user(db)
    LearnerProfileService(db).set_interest(user.id, concept_id=concept.id)

    plan_service = LearningPlanService(db)
    diff = plan_service.replan(user.id)
    accepted = plan_service.accept_item(user.id, diff.new_items[0].id)
    assert accepted.state == PlanItemState.PLANNED

    second_diff = plan_service.replan(user.id)
    assert concept.id not in second_diff.added
    assert concept.id in second_diff.already_planned

    plan = plan_service.get_plan(user.id)
    assert [item.id for item in plan] == [accepted.id]


def test_replan_never_touches_an_accepted_items_state(db):
    concept = make_concept(db, slug="tokens", level=ConceptLevel.FOUNDATIONAL)
    user = make_user(db)
    LearnerProfileService(db).set_interest(user.id, concept_id=concept.id)

    plan_service = LearningPlanService(db)
    diff = plan_service.replan(user.id)
    accepted = plan_service.accept_item(user.id, diff.new_items[0].id)

    plan_service.replan(user.id)
    db.refresh(accepted)
    assert accepted.state == PlanItemState.PLANNED


def test_reject_item_removes_it_from_future_proposals_state(db):
    concept = make_concept(db, slug="tokens", level=ConceptLevel.FOUNDATIONAL)
    user = make_user(db)
    LearnerProfileService(db).set_interest(user.id, concept_id=concept.id)

    plan_service = LearningPlanService(db)
    diff = plan_service.replan(user.id)
    rejected = plan_service.reject_item(user.id, diff.new_items[0].id)
    assert rejected.state == PlanItemState.REJECTED
