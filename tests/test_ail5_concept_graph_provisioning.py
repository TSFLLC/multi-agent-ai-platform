import pytest
from sqlalchemy import select

from app.academy_curriculum import FOUNDATIONS_PROGRAM
from app.assessment_curriculum import seed_foundation_assessments
from app.db.enums import ConceptRelationType, ContentOrigin, VersionStatus
from app.errors import ConflictError
from app.models.concepts import Concept, ConceptRelation, ConceptVersion, LearningItem
from app.practical_ai_foundations_manifest import (
    CANONICAL_FOUNDATIONS_SLUGS,
    FOUNDATIONS_CONCEPT_MANIFEST,
    validate_practical_ai_foundations_manifest,
)
from app.services.academy_service import AcademyService
from app.services.build_with_me_service import ProjectTemplateService
from app.services.concept_graph_service import ConceptGraphService
from app.services.practical_ai_foundations_provisioning import provision_practical_ai_foundations
from app.services.professor_context_service import ProfessorContextAssembler
from app.schemas.professor import ProfessorContextRequest, ProfessorIntent, ProfessorTarget, ProfessorTargetType
from tests.ail1a_factories import make_concept


def test_manifest_has_exact_approved_graph_and_passes_static_gates():
    validate_practical_ai_foundations_manifest()
    assert len(FOUNDATIONS_CONCEPT_MANIFEST) == 28
    assert len(set(CANONICAL_FOUNDATIONS_SLUGS)) == 28
    assert {slug for _module, _title, slugs in FOUNDATIONS_PROGRAM["modules"] for slug in slugs} == set(CANONICAL_FOUNDATIONS_SLUGS)


def test_provisioning_is_published_additive_and_idempotent(db):
    first = provision_practical_ai_foundations(db)
    assert len(first.concepts_created) == 28
    assert len(first.versions_published) == 28
    assert first.relations_created
    assert first.learning_items_created

    concept_count = db.execute(select(Concept)).scalars().all()
    version_count = db.execute(select(ConceptVersion)).scalars().all()
    relation_count = db.execute(select(ConceptRelation)).scalars().all()
    item_count = db.execute(select(LearningItem)).scalars().all()
    before = (len(concept_count), len(version_count), len(relation_count), len(item_count))

    second = provision_practical_ai_foundations(db)
    after = (
        len(db.execute(select(Concept)).scalars().all()),
        len(db.execute(select(ConceptVersion)).scalars().all()),
        len(db.execute(select(ConceptRelation)).scalars().all()),
        len(db.execute(select(LearningItem)).scalars().all()),
    )
    assert before == after
    assert second.concepts_created == []
    assert second.versions_published == []

    versions = db.execute(select(ConceptVersion)).scalars().all()
    assert all(version.status == VersionStatus.ACTIVE for version in versions)
    assert all(version.content_origin == ContentOrigin.HUMAN for version in versions)


def test_existing_published_version_is_never_overwritten(db):
    concept = make_concept(db, slug="what-ai-is-and-isnt", name="Existing authored identity")
    graph = ConceptGraphService(db)
    version = graph.create_draft_version(concept_id=concept.id, plain_definition="Existing definition")
    graph.publish_version(version.id)

    result = provision_practical_ai_foundations(db)
    assert "what-ai-is-and-isnt" in result.skipped_existing_concepts
    assert "what-ai-is-and-isnt" in result.skipped_existing_versions
    preserved = db.get(ConceptVersion, version.id)
    assert preserved.plain_definition == "Existing definition"
    assert preserved.version == 1


def test_prerequisite_edges_are_dag_and_evaluation_is_related_to_model_choice(db):
    provision_practical_ai_foundations(db)
    choosing = db.execute(select(Concept).where(Concept.slug == "choosing-a-model")).scalar_one()
    evaluation = db.execute(select(Concept).where(Concept.slug == "evaluation")).scalar_one()
    related = db.execute(select(ConceptRelation).where(ConceptRelation.from_concept_id == choosing.id, ConceptRelation.to_concept_id == evaluation.id, ConceptRelation.relation_type == ConceptRelationType.RELATED)).scalar_one()
    assert related.label == "associated_with"
    assert db.execute(select(ConceptRelation).where(ConceptRelation.from_concept_id == evaluation.id, ConceptRelation.to_concept_id == choosing.id, ConceptRelation.relation_type == ConceptRelationType.PREREQUISITE)).scalar_one_or_none() is None

    graph = ConceptGraphService(db)
    with pytest.raises(ConflictError):
        graph.add_relation(from_concept_id=choosing.id, to_concept_id=choosing.id, relation_type=ConceptRelationType.PREREQUISITE)


def test_agents_and_workflows_are_co_taught_not_a_schedule_prerequisite(db):
    provision_practical_ai_foundations(db)
    agents = db.execute(select(Concept).where(Concept.slug == "agents")).scalar_one()
    workflows = db.execute(select(Concept).where(Concept.slug == "workflows")).scalar_one()
    assert db.execute(select(ConceptRelation).where(
        ConceptRelation.from_concept_id == agents.id,
        ConceptRelation.to_concept_id == workflows.id,
        ConceptRelation.relation_type == ConceptRelationType.PREREQUISITE,
    )).scalar_one_or_none() is None
    related = db.execute(select(ConceptRelation).where(
        ConceptRelation.from_concept_id == workflows.id,
        ConceptRelation.to_concept_id == agents.id,
        ConceptRelation.relation_type == ConceptRelationType.RELATED,
    )).scalar_one()
    assert related.label == "co_taught_with"


def test_foundation_seed_consumers_and_professor_can_read_provisioned_concept(db, bootstrap):
    provision_practical_ai_foundations(db)
    program_version = AcademyService(db).seed_foundations_program(bootstrap.user)
    assert program_version.duration_days == 30

    # These are the existing additive seed consumers; they resolve graph rows
    # by canonical slug and must not manufacture concepts.
    ProjectTemplateService.seed_foundation_projects(db, bootstrap.user.id)
    seed_foundation_assessments(db, bootstrap.user.id)

    concept = db.execute(select(Concept).where(Concept.slug == "rag")).scalar_one()
    context = ProfessorContextAssembler(db).assemble(
        bootstrap.user.id,
        ProfessorContextRequest(
            intent=ProfessorIntent.EXPLAIN_THIS,
            target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=concept.id),
        ),
    )
    assert any(record.ref_type == "concept_version" for record in context.records)
    assert any(record.ref_type == "learning_item" for record in context.records)
