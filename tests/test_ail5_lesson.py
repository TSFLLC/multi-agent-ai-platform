from app.db.enums import ConceptKind, ConceptLevel, LearningItemType
from app.services.concept_graph_service import ConceptGraphService


def test_lesson_returns_active_authored_content_and_learning_items(client, db, bootstrap, auth_headers):
    graph = ConceptGraphService(db)
    concept = graph.create_concept(
        slug="lesson-content",
        name="Lesson Content",
        level=ConceptLevel.FOUNDATIONAL,
        kind=ConceptKind.DEFINITIONAL,
        is_core=True,
    )
    version = graph.create_draft_version(
        concept_id=concept.id,
        plain_definition="Plain authored definition.",
        technical_explanation="Technical authored explanation.",
        examples_md="- Authored example",
    )
    graph.publish_version(version.id)
    graph.create_learning_item(
        concept_id=concept.id,
        item_type=LearningItemType.RESOURCE,
        title="Read the example",
        body_md="Authored instructional material.",
        reviewed=True,
    )

    before = client.get(f"/academy/concepts/{concept.id}/lesson", headers=auth_headers)
    assert before.status_code == 200, before.text
    payload = before.json()
    assert payload["concept_id"] == concept.id
    assert payload["concept_version_id"] == version.id
    assert payload["plain_definition"] == "Plain authored definition."
    assert payload["technical_explanation"] == "Technical authored explanation."
    assert payload["examples_md"] == "- Authored example"
    assert payload["learning_items"][0]["title"] == "Read the example"
    assert payload["learner_state"] == "not_started"
    assert payload["evidence_count"] == 0

    after = client.get(f"/academy/concepts/{concept.id}/lesson", headers=auth_headers)
    assert after.json()["learner_state"] == payload["learner_state"]
    assert after.json()["evidence_count"] == payload["evidence_count"] == 0


def test_lesson_without_learning_items_is_valid_and_requires_auth(client, db, bootstrap, auth_headers):
    graph = ConceptGraphService(db)
    concept = graph.create_concept(
        slug="lesson-without-items",
        name="Lesson Without Items",
        level=ConceptLevel.FOUNDATIONAL,
        kind=ConceptKind.DEFINITIONAL,
    )
    version = graph.create_draft_version(concept_id=concept.id, plain_definition="Definition")
    graph.publish_version(version.id)

    unauthenticated = client.get(f"/academy/concepts/{concept.id}/lesson")
    assert unauthenticated.status_code == 401
    response = client.get(f"/academy/concepts/{concept.id}/lesson", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["learning_items"] == []
