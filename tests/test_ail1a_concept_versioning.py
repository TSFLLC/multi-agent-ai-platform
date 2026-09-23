import pytest

from app.db.enums import VersionStatus
from app.errors import ConflictError, NotFoundError
from app.services.concept_graph_service import ConceptGraphService
from tests.ail1a_factories import make_concept


def test_create_draft_version_starts_at_one(db):
    service = ConceptGraphService(db)
    concept = make_concept(db)
    version = service.create_draft_version(concept_id=concept.id, plain_definition="v1 definition")
    assert version.version == 1
    assert version.status == VersionStatus.DRAFT


def test_editing_a_concept_allocates_version_plus_one(db):
    service = ConceptGraphService(db)
    concept = make_concept(db)
    v1 = service.create_draft_version(concept_id=concept.id, plain_definition="v1")
    service.publish_version(v1.id)
    v2 = service.create_draft_version(concept_id=concept.id, plain_definition="v2")
    assert v2.version == 2


def test_publish_version_never_mutates_prior_published_content(db):
    service = ConceptGraphService(db)
    concept = make_concept(db)
    v1 = service.create_draft_version(concept_id=concept.id, plain_definition="original text")
    service.publish_version(v1.id)
    original_text = v1.plain_definition
    original_published_at = v1.published_at

    v2 = service.create_draft_version(
        concept_id=concept.id, plain_definition="revised text", change_note="clarified wording"
    )
    service.publish_version(v2.id)

    db.refresh(v1)
    assert v1.plain_definition == original_text
    assert v1.published_at == original_published_at
    assert v1.status == VersionStatus.DEPRECATED
    assert v2.status == VersionStatus.ACTIVE


def test_only_one_active_version_at_a_time(db):
    service = ConceptGraphService(db)
    concept = make_concept(db)
    v1 = service.create_draft_version(concept_id=concept.id, plain_definition="v1")
    service.publish_version(v1.id)
    v2 = service.create_draft_version(concept_id=concept.id, plain_definition="v2")
    service.publish_version(v2.id)

    versions = service.list_versions(concept.id)
    active = [v for v in versions if v.status == VersionStatus.ACTIVE]
    assert len(active) == 1
    assert active[0].id == v2.id


def test_get_current_version_returns_active_version(db):
    service = ConceptGraphService(db)
    concept = make_concept(db)
    v1 = service.create_draft_version(concept_id=concept.id, plain_definition="v1")
    service.publish_version(v1.id)
    assert service.get_current_version(concept.id).id == v1.id


def test_publish_requires_draft_status(db):
    service = ConceptGraphService(db)
    concept = make_concept(db)
    v1 = service.create_draft_version(concept_id=concept.id, plain_definition="v1")
    service.publish_version(v1.id)
    with pytest.raises(ConflictError):
        service.publish_version(v1.id)


def test_publish_unknown_version_raises_not_found(db):
    service = ConceptGraphService(db)
    with pytest.raises(NotFoundError):
        service.publish_version("00000000-0000-0000-0000-000000000000")


def test_concept_status_mirrors_latest_published_version(db):
    service = ConceptGraphService(db)
    concept = make_concept(db)
    assert concept.status is None
    v1 = service.create_draft_version(concept_id=concept.id, plain_definition="v1")
    service.publish_version(v1.id)
    db.refresh(concept)
    assert concept.status == VersionStatus.ACTIVE
