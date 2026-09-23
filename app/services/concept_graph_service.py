"""Concept Graph Service — AIL.1A, docs/ail-learning-spec-v1.md Sec 15.

Publishing discipline mirrors ``app.services.agent_registry_service``
exactly: a concept version's content columns are set once, at creation,
and ``publish_version`` only ever flips ``status``/``published_at`` —
nothing here ever mutates a published version's
plain_definition/technical_explanation/examples_md/evidence_requirements
in place. "Editing" a concept means calling ``create_draft_version``
again, which allocates ``version + 1``.

Prerequisite acyclicity is enforced here, not at the DB layer (SQLite
cannot express graph acyclicity declaratively) — ``add_relation`` runs a
reachability check before insert and raises ``ConflictError`` rather than
let a cycle land.

Relation directionality: ``from_concept_id`` is the prerequisite,
``to_concept_id`` is the dependent — ``add_relation(A, B, PREREQUISITE)``
means "A must be at least UNDERSTOOD before B is planned" (spec Sec
15.2), matching the graph fragment's left-to-right arrows (Sec 15.4).
"""

from datetime import datetime, timezone
from typing import List, Optional, Set

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import ConceptRelationType, ContentOrigin, VersionStatus
from app.errors import ConflictError, NotFoundError
from app.models.concepts import Concept, ConceptRelation, ConceptTerm, ConceptVersion, LearningItem


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConceptGraphService:
    def __init__(self, db: Session):
        self.db = db

    # -- Concepts -----------------------------------------------------------

    def create_concept(
        self,
        *,
        slug: str,
        name: str,
        level,
        kind,
        is_core: bool = False,
        freshness_days: Optional[int] = None,
        aliases: Optional[list] = None,
    ) -> Concept:
        concept = Concept(
            slug=slug,
            name=name,
            level=level,
            kind=kind,
            is_core=is_core,
            freshness_days=freshness_days,
            aliases=aliases,
        )
        self.db.add(concept)
        self.db.commit()
        self.db.refresh(concept)
        return concept

    def get_concept(self, concept_id: str) -> Optional[Concept]:
        return self.db.get(Concept, concept_id)

    def get_concept_by_slug(self, slug: str) -> Optional[Concept]:
        stmt = select(Concept).where(Concept.slug == slug)
        return self.db.execute(stmt).scalars().first()

    def list_concepts(self) -> List[Concept]:
        return list(self.db.execute(select(Concept)).scalars().all())

    # -- Concept Versions -----------------------------------------------------

    def create_draft_version(
        self,
        *,
        concept_id: str,
        plain_definition: str,
        technical_explanation: Optional[str] = None,
        examples_md: Optional[str] = None,
        evidence_requirements: Optional[dict] = None,
        content_origin=None,
        change_note: Optional[str] = None,
        change_severity=None,
    ) -> ConceptVersion:
        concept = self.get_concept(concept_id)
        if concept is None:
            raise NotFoundError(f"Concept {concept_id} not found")

        next_version = self._next_version_number(concept_id)
        version = ConceptVersion(
            concept_id=concept_id,
            version=next_version,
            plain_definition=plain_definition,
            technical_explanation=technical_explanation,
            examples_md=examples_md,
            evidence_requirements=evidence_requirements,
            content_origin=content_origin or ContentOrigin.AI_DRAFTED_UNREVIEWED,
            change_note=change_note,
            change_severity=change_severity,
            status=VersionStatus.DRAFT,
        )
        self.db.add(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def publish_version(self, version_id: str) -> ConceptVersion:
        version = self.db.get(ConceptVersion, version_id)
        if version is None:
            raise NotFoundError(f"ConceptVersion {version_id} not found")
        if version.status != VersionStatus.DRAFT:
            raise ConflictError(f"ConceptVersion {version_id} is {version.status.value}, not draft")

        # Immutability discipline: only status/published_at ever change on
        # a version row, here or anywhere else in this service.
        now = _utcnow()
        previously_active = self._get_active_version(version.concept_id)
        if previously_active is not None:
            previously_active.status = VersionStatus.DEPRECATED

        version.status = VersionStatus.ACTIVE
        version.published_at = now

        concept = self.get_concept(version.concept_id)
        concept.status = VersionStatus.ACTIVE

        self.db.commit()
        self.db.refresh(version)
        return version

    def get_current_version(self, concept_id: str) -> Optional[ConceptVersion]:
        """The ACTIVE version if one exists, else the latest by version
        number (spec Sec 32.1 "derive, don't cache" — never a stored FK)."""
        active = self._get_active_version(concept_id)
        if active is not None:
            return active
        stmt = (
            select(ConceptVersion)
            .where(ConceptVersion.concept_id == concept_id)
            .order_by(ConceptVersion.version.desc())
        )
        return self.db.execute(stmt).scalars().first()

    def list_versions(self, concept_id: str) -> List[ConceptVersion]:
        stmt = (
            select(ConceptVersion)
            .where(ConceptVersion.concept_id == concept_id)
            .order_by(ConceptVersion.version)
        )
        return list(self.db.execute(stmt).scalars().all())

    def _get_active_version(self, concept_id: str) -> Optional[ConceptVersion]:
        stmt = select(ConceptVersion).where(
            ConceptVersion.concept_id == concept_id, ConceptVersion.status == VersionStatus.ACTIVE
        )
        return self.db.execute(stmt).scalars().first()

    def _next_version_number(self, concept_id: str) -> int:
        existing = self.list_versions(concept_id)
        return (existing[-1].version + 1) if existing else 1

    # -- Relations (prerequisite / part_of / related) ------------------------

    def add_relation(
        self,
        *,
        from_concept_id: str,
        to_concept_id: str,
        relation_type: ConceptRelationType,
        label: Optional[str] = None,
    ) -> ConceptRelation:
        if from_concept_id == to_concept_id:
            raise ConflictError("A concept cannot relate to itself")

        if relation_type == ConceptRelationType.PREREQUISITE and self._reaches(
            from_=to_concept_id, to_=from_concept_id, relation_type=relation_type
        ):
            raise ConflictError(
                f"Adding {from_concept_id} -> {to_concept_id} as a prerequisite edge would create a cycle"
            )

        relation = ConceptRelation(
            from_concept_id=from_concept_id,
            to_concept_id=to_concept_id,
            relation_type=relation_type,
            label=label,
        )
        self.db.add(relation)
        self.db.commit()
        self.db.refresh(relation)
        return relation

    def _reaches(self, *, from_: str, to_: str, relation_type: ConceptRelationType) -> bool:
        """True if ``to_`` is reachable from ``from_`` following existing
        edges of ``relation_type`` in the from->to direction — used to
        detect that a proposed new edge would close a cycle before it is
        written."""
        visited: Set[str] = set()
        frontier = [from_]
        while frontier:
            current = frontier.pop()
            if current == to_:
                return True
            if current in visited:
                continue
            visited.add(current)
            stmt = select(ConceptRelation.to_concept_id).where(
                ConceptRelation.from_concept_id == current, ConceptRelation.relation_type == relation_type
            )
            frontier.extend(self.db.execute(stmt).scalars().all())
        return False

    def get_prerequisites(self, concept_id: str) -> List[str]:
        """Direct prerequisites only (not transitive)."""
        stmt = select(ConceptRelation.from_concept_id).where(
            ConceptRelation.to_concept_id == concept_id,
            ConceptRelation.relation_type == ConceptRelationType.PREREQUISITE,
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_prerequisite_closure(self, concept_id: str) -> Set[str]:
        """Every concept that must be at least UNDERSTOOD before
        ``concept_id`` can be planned, transitively."""
        closure: Set[str] = set()
        frontier = list(self.get_prerequisites(concept_id))
        while frontier:
            current = frontier.pop()
            if current in closure:
                continue
            closure.add(current)
            frontier.extend(self.get_prerequisites(current))
        return closure

    # -- Concept <-> taxonomy term membership --------------------------------

    def add_term(self, *, concept_id: str, term_id: str) -> ConceptTerm:
        link = ConceptTerm(concept_id=concept_id, term_id=term_id)
        self.db.add(link)
        self.db.commit()
        self.db.refresh(link)
        return link

    def list_term_ids(self, concept_id: str) -> List[str]:
        stmt = select(ConceptTerm.term_id).where(ConceptTerm.concept_id == concept_id)
        return list(self.db.execute(stmt).scalars().all())

    # -- Learning Items -------------------------------------------------------

    def create_learning_item(
        self,
        *,
        concept_id: str,
        item_type,
        title: str,
        body_md: Optional[str] = None,
        spec: Optional[dict] = None,
        grading_mode=None,
        reviewed: bool = False,
        est_minutes: Optional[int] = None,
        requires_capability_term_id: Optional[str] = None,
    ) -> LearningItem:
        item = LearningItem(
            concept_id=concept_id,
            item_type=item_type,
            title=title,
            body_md=body_md,
            spec=spec,
            grading_mode=grading_mode,
            reviewed=reviewed,
            est_minutes=est_minutes,
            requires_capability_term_id=requires_capability_term_id,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def list_learning_items(self, concept_id: str) -> List[LearningItem]:
        stmt = select(LearningItem).where(LearningItem.concept_id == concept_id)
        return list(self.db.execute(stmt).scalars().all())
