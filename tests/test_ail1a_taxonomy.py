import pytest
from sqlalchemy.exc import IntegrityError

from app.models.taxonomy import TaxonomyTerm
from app.services.taxonomy_service import TaxonomyService


def test_get_or_create_term_is_idempotent(db):
    service = TaxonomyService(db)
    first = service.get_or_create_term(vocabulary="track", key="agentic_ai", label="Agentic AI")
    second = service.get_or_create_term(vocabulary="track", key="agentic_ai", label="Agentic AI (again)")
    assert first.id == second.id
    assert first.label == "Agentic AI"


def test_vocabulary_key_uniqueness_enforced(db):
    service = TaxonomyService(db)
    service.get_or_create_term(vocabulary="track", key="model_intelligence", label="Model Intelligence")

    db.add(TaxonomyTerm(vocabulary="track", key="model_intelligence", label="Duplicate"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_different_vocabularies_may_share_a_key(db):
    service = TaxonomyService(db)
    track = service.get_or_create_term(vocabulary="track", key="quality", label="AI Quality & Evaluation")
    topic = service.get_or_create_term(vocabulary="topic", key="quality", label="Quality (topic)")
    assert track.id != topic.id


def test_list_terms_filters_by_vocabulary_and_active(db):
    service = TaxonomyService(db)
    service.get_or_create_term(vocabulary="role", key="code_reviewer", label="Code Reviewer")
    service.get_or_create_term(vocabulary="track", key="security", label="AI Security")

    roles = service.list_terms(vocabulary="role")
    assert {t.key for t in roles} == {"code_reviewer"}

    inactive = service.get_or_create_term(vocabulary="role", key="planner", label="Planner")
    inactive.active = False
    db.commit()

    active_roles = service.list_terms(vocabulary="role")
    assert "planner" not in {t.key for t in active_roles}


def test_taxonomy_vocabulary_is_not_a_closed_enum(db):
    """Frozen contract #3: any slice can introduce a new vocabulary value
    without an AIL.1A schema/code change — proven by using one nobody
    seeded here."""
    service = TaxonomyService(db)
    term = service.get_or_create_term(vocabulary="capability", key="cheap_reasoning", label="Cheap Reasoning")
    assert term.vocabulary == "capability"
