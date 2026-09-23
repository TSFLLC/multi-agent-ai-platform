import pytest

from app.db.enums import ConceptRelationType
from app.errors import ConflictError
from app.services.concept_graph_service import ConceptGraphService
from tests.ail1a_factories import make_concept


def test_add_prerequisite_edge(db):
    service = ConceptGraphService(db)
    tokens = make_concept(db, slug="tokens", name="Tokens")
    context_windows = make_concept(db, slug="context-windows", name="Context Windows")

    service.add_relation(
        from_concept_id=tokens.id,
        to_concept_id=context_windows.id,
        relation_type=ConceptRelationType.PREREQUISITE,
    )
    assert service.get_prerequisites(context_windows.id) == [tokens.id]


def test_direct_cycle_rejected(db):
    service = ConceptGraphService(db)
    a = make_concept(db, slug="a", name="A")
    b = make_concept(db, slug="b", name="B")
    service.add_relation(
        from_concept_id=a.id, to_concept_id=b.id, relation_type=ConceptRelationType.PREREQUISITE
    )

    with pytest.raises(ConflictError):
        service.add_relation(
            from_concept_id=b.id, to_concept_id=a.id, relation_type=ConceptRelationType.PREREQUISITE
        )


def test_transitive_cycle_rejected(db):
    """A -> B -> C, then C -> A must be rejected (spec acceptance
    criterion: "Adding a prerequisite edge that creates a cycle is
    rejected.")."""
    service = ConceptGraphService(db)
    a = make_concept(db, slug="a", name="A")
    b = make_concept(db, slug="b", name="B")
    c = make_concept(db, slug="c", name="C")
    service.add_relation(
        from_concept_id=a.id, to_concept_id=b.id, relation_type=ConceptRelationType.PREREQUISITE
    )
    service.add_relation(
        from_concept_id=b.id, to_concept_id=c.id, relation_type=ConceptRelationType.PREREQUISITE
    )

    with pytest.raises(ConflictError):
        service.add_relation(
            from_concept_id=c.id, to_concept_id=a.id, relation_type=ConceptRelationType.PREREQUISITE
        )


def test_self_loop_rejected(db):
    service = ConceptGraphService(db)
    a = make_concept(db, slug="a", name="A")
    with pytest.raises(ConflictError):
        service.add_relation(
            from_concept_id=a.id, to_concept_id=a.id, relation_type=ConceptRelationType.PREREQUISITE
        )


def test_non_prerequisite_relations_do_not_trigger_cycle_check(db):
    """`related`/`part_of` edges are not subject to acyclicity — only
    `prerequisite` is a DAG (spec Sec 15.2)."""
    service = ConceptGraphService(db)
    a = make_concept(db, slug="rag", name="RAG")
    b = make_concept(db, slug="fine-tuning", name="Fine-Tuning")
    service.add_relation(
        from_concept_id=a.id,
        to_concept_id=b.id,
        relation_type=ConceptRelationType.RELATED,
        label="alternative_to",
    )
    # The reverse relation of the same soft type is fine -- no acyclicity
    # rule applies to `related`.
    service.add_relation(
        from_concept_id=b.id,
        to_concept_id=a.id,
        relation_type=ConceptRelationType.RELATED,
        label="alternative_to",
    )


def test_prerequisite_closure_is_transitive(db):
    service = ConceptGraphService(db)
    tokens = make_concept(db, slug="tokens", name="Tokens")
    context = make_concept(db, slug="context", name="Context Windows")
    long_context = make_concept(db, slug="long-context", name="Long Context vs RAG")

    service.add_relation(
        from_concept_id=tokens.id, to_concept_id=context.id, relation_type=ConceptRelationType.PREREQUISITE
    )
    service.add_relation(
        from_concept_id=context.id,
        to_concept_id=long_context.id,
        relation_type=ConceptRelationType.PREREQUISITE,
    )

    closure = service.get_prerequisite_closure(long_context.id)
    assert closure == {tokens.id, context.id}
