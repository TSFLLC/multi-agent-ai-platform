"""Additive provisioning for the approved Practical AI Foundations graph."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import ConceptKind, ConceptLevel, ConceptRelationType, ContentOrigin, GradingMode, LearningItemType
from app.models.concepts import Concept, ConceptRelation, ConceptVersion, LearningItem
from app.practical_ai_foundations_manifest import FOUNDATIONS_CONCEPT_MANIFEST, validate_practical_ai_foundations_manifest
from app.services.concept_graph_service import ConceptGraphService


@dataclass
class ProvisioningResult:
    concepts_created: list[str] = field(default_factory=list)
    versions_published: list[str] = field(default_factory=list)
    relations_created: list[tuple[str, str, str]] = field(default_factory=list)
    learning_items_created: list[tuple[str, str]] = field(default_factory=list)
    skipped_existing_concepts: list[str] = field(default_factory=list)
    skipped_existing_versions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "concepts_created": self.concepts_created,
            "versions_published": self.versions_published,
            "relations_created": [list(edge) for edge in self.relations_created],
            "learning_items_created": [list(item) for item in self.learning_items_created],
            "skipped_existing_concepts": self.skipped_existing_concepts,
            "skipped_existing_versions": self.skipped_existing_versions,
            "manifest_slugs": [entry["slug"] for entry in FOUNDATIONS_CONCEPT_MANIFEST],
        }


def provision_practical_ai_foundations(db: Session) -> ProvisioningResult:
    """Provision only absent graph rows; never edit existing content.

    All manifest validation happens before the first call to
    ``ConceptGraphService``.  Existing Concepts, versions, relations, and
    Learning Items are treated as immutable inputs for this operation.
    """
    validate_practical_ai_foundations_manifest()
    graph = ConceptGraphService(db)
    result = ProvisioningResult()

    concepts = {
        concept.slug: concept
        for concept in db.execute(
            select(Concept).where(Concept.slug.in_([entry["slug"] for entry in FOUNDATIONS_CONCEPT_MANIFEST]))
        ).scalars().all()
    }

    reconcile_curriculum_relation_corrections(db, concepts)

    # Resolve identities first.  No authored content is written over an
    # existing Concept row, even if its metadata differs.
    for entry in FOUNDATIONS_CONCEPT_MANIFEST:
        slug = entry["slug"]
        concept = concepts.get(slug)
        if concept is None:
            concept = graph.create_concept(
                slug=slug,
                name=entry["name"],
                level=ConceptLevel(entry["level"]),
                kind=ConceptKind(entry["kind"]),
                is_core=entry["is_core"],
            )
            concepts[slug] = concept
            result.concepts_created.append(slug)
        else:
            result.skipped_existing_concepts.append(slug)

    # Version 1 is created/published only for a Concept with no versions.
    # A pre-existing draft is not silently published or replaced.
    for entry in FOUNDATIONS_CONCEPT_MANIFEST:
        concept = concepts[entry["slug"]]
        versions = graph.list_versions(concept.id)
        if versions:
            result.skipped_existing_versions.append(entry["slug"])
            continue
        version = graph.create_draft_version(
            concept_id=concept.id,
            plain_definition=entry["plain_definition"],
            technical_explanation=entry["technical_explanation"],
            examples_md=entry["examples_md"],
            evidence_requirements=entry["evidence_requirements"],
            content_origin=ContentOrigin.HUMAN,
            change_note="Approved Practical AI Foundations Concept Graph V1",
        )
        graph.publish_version(version.id)
        result.versions_published.append(entry["slug"])

    # Relation insertion remains delegated to ConceptGraphService, including
    # its authoritative prerequisite cycle check.
    for entry in FOUNDATIONS_CONCEPT_MANIFEST:
        dependent = concepts[entry["slug"]]
        for prerequisite_slug in entry["prerequisites"]:
            prerequisite = concepts[prerequisite_slug]
            _add_relation_if_absent(db, graph, prerequisite.id, dependent.id, ConceptRelationType.PREREQUISITE, result, prerequisite_slug, entry["slug"])
        for related in entry["related"]:
            target = concepts[related["slug"]]
            _add_relation_if_absent(db, graph, dependent.id, target.id, ConceptRelationType.RELATED, result, entry["slug"], related["slug"], related.get("label"))

    # LearningItem has no natural-key column.  The stable authored title plus
    # concept and version is the idempotency key; existing rows are never
    # updated.
    for entry in FOUNDATIONS_CONCEPT_MANIFEST:
        concept = concepts[entry["slug"]]
        existing = {(item.title, item.version) for item in graph.list_learning_items(concept.id)}
        for item in entry["learning_items"]:
            key = (item["title"], 1)
            if key in existing:
                continue
            graph.create_learning_item(
                concept_id=concept.id,
                item_type=LearningItemType(item["item_type"]),
                title=item["title"],
                body_md=item.get("body_md"),
                spec=item.get("spec"),
                grading_mode=GradingMode(item["grading_mode"]) if item.get("grading_mode") else None,
                reviewed=item.get("reviewed", False),
                est_minutes=item.get("est_minutes"),
            )
            existing.add(key)
            result.learning_items_created.append((entry["slug"], item["title"]))

    return result


def reconcile_curriculum_relation_corrections(db: Session, concepts: dict[str, Concept] | None = None) -> None:
    """Repair only the previously provisioned relation superseded by V2.

    The original graph seed incorrectly made ``agents`` a hard prerequisite of
    ``workflows``.  The approved curriculum teaches both in the Day 13 lecture
    and revisits agent design on Day 16, so that edge conflicts with the frozen
    Academy schedule.  This narrowly-scoped correction is safe for existing
    staging graphs and leaves all other graph relations untouched.
    """
    if concepts is None:
        concepts = {
            concept.slug: concept
            for concept in db.execute(
                select(Concept).where(Concept.slug.in_(["agents", "workflows"]))
            ).scalars().all()
        }
    agents = concepts.get("agents")
    workflows = concepts.get("workflows")
    if agents is None or workflows is None:
        return
    obsolete = db.execute(
        select(ConceptRelation).where(
            ConceptRelation.from_concept_id == agents.id,
            ConceptRelation.to_concept_id == workflows.id,
            ConceptRelation.relation_type == ConceptRelationType.PREREQUISITE,
        )
    ).scalar_one_or_none()
    if obsolete is not None:
        db.delete(obsolete)
        db.commit()


def _add_relation_if_absent(db, graph, from_id, to_id, relation_type, result, from_slug, to_slug, label=None):
    existing = db.execute(
        select(ConceptRelation).where(
            ConceptRelation.from_concept_id == from_id,
            ConceptRelation.to_concept_id == to_id,
            ConceptRelation.relation_type == relation_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    graph.add_relation(from_concept_id=from_id, to_concept_id=to_id, relation_type=relation_type, label=label)
    result.relations_created.append((from_slug, to_slug, relation_type.value))
