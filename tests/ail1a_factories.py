"""Minimal entity factories for AIL.1A tests only.

Deliberately not added to ``tests/conftest.py`` — that file is shared
infrastructure other simultaneous AIL slices (Model/Provider Explorer,
Radar) also touch; keeping AIL.1A's own factories in a separate module
avoids an unnecessary collision surface (AIL architecture reconciliation
collision rules).
"""

from app.db.enums import ConceptKind, ConceptLevel, OrgRole
from app.models.identity import User


def make_user(db, org=None, email="learner@example.com"):
    from tests.conftest import make_org

    org = org or make_org(db)
    user = User(org_id=org.id, email=email, role=OrgRole.MEMBER)
    db.add(user)
    db.flush()
    return user


def make_concept(
    db,
    *,
    slug="tokens",
    name="Tokens & Tokenization",
    level=ConceptLevel.FOUNDATIONAL,
    kind=ConceptKind.DEFINITIONAL,
    is_core=False,
):
    from app.services.concept_graph_service import ConceptGraphService

    return ConceptGraphService(db).create_concept(
        slug=slug, name=name, level=level, kind=kind, is_core=is_core
    )


def make_published_version(
    db,
    concept=None,
    *,
    plain_definition="A definition.",
    kind=ConceptKind.DEFINITIONAL,
    level=ConceptLevel.FOUNDATIONAL,
    is_core=False,
    **version_kwargs,
):
    """``kind``/``level``/``is_core`` are Concept-level attributes; every
    other keyword is forwarded to ``create_draft_version`` (e.g.
    ``evidence_requirements``, ``change_severity``)."""
    from app.services.concept_graph_service import ConceptGraphService

    concept = concept or make_concept(db, kind=kind, level=level, is_core=is_core)
    service = ConceptGraphService(db)
    version = service.create_draft_version(
        concept_id=concept.id, plain_definition=plain_definition, **version_kwargs
    )
    return service.publish_version(version.id)
