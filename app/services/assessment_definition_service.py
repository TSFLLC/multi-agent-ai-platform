"""AIL.5C assessment definitions: versioned, immutable once published.

A definition is the rubric + challenge unit an attempt is graded against.
Rows are one-per-version. A published row is frozen by an ORM guard AND by a
``content_hash`` computed at publish; a "change" is always a NEW version, and
existing attempts stay pinned to the version they ran against.
"""

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assessment_contract import (
    DEFAULT_GRADING_POLICY,
    DEFAULT_INDEPENDENCE_POLICY,
    KIND_TO_EVIDENCE,
    DefinitionInvalid,
    sha256_hex,
    validate_definition,
)
from app.db.enums import (
    AssessmentDefinitionStatus,
    AssessmentKind,
    VersionStatus,
)
from app.errors import ConflictError, NotFoundError
from app.models.academy import ProjectTemplate
from app.models.assessment import AssessmentDefinition, AssessmentDefinitionConcept
from app.models.concepts import ConceptVersion
from app.services.assessment_checks import CHECKS
from app.services.concept_graph_service import ConceptGraphService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def content_payload(defn: AssessmentDefinition, links: Iterable[AssessmentDefinitionConcept]) -> dict:
    """The canonical, hash-frozen content of one definition version."""
    return {
        "definition_key": defn.definition_key,
        "version": defn.version,
        "assessment_kind": defn.assessment_kind.value,
        "title": defn.title,
        "instructions_md": defn.instructions_md,
        "produces_evidence_type": defn.produces_evidence_type.value,
        "project_template_id": defn.project_template_id,
        "criteria": defn.criteria,
        "challenge_spec": defn.challenge_spec,
        "independence_policy": defn.independence_policy,
        "grading_policy": defn.grading_policy,
        "allowed_resources": defn.allowed_resources,
        "requires_platform_capability": defn.requires_platform_capability,
        "concepts": sorted(
            (
                {
                    "concept_id": link.concept_id,
                    "concept_version_id": link.concept_version_id,
                    "role": link.role,
                    "criterion_keys": list(link.criterion_keys),
                }
                for link in links
            ),
            key=lambda c: c["concept_id"],
        ),
    }


class AssessmentDefinitionService:
    def __init__(self, db: Session):
        self.db = db

    # -- reads ----------------------------------------------------------------------

    def get(self, definition_id: str) -> AssessmentDefinition:
        defn = self.db.get(AssessmentDefinition, definition_id)
        if defn is None:
            raise NotFoundError("Assessment definition not found.")
        return defn

    def links(self, definition_id: str) -> List[AssessmentDefinitionConcept]:
        return list(
            self.db.execute(
                select(AssessmentDefinitionConcept)
                .where(AssessmentDefinitionConcept.definition_id == definition_id)
                .order_by(AssessmentDefinitionConcept.concept_id)
            ).scalars()
        )

    def current(self, definition_key: str) -> Optional[AssessmentDefinition]:
        """The newest PUBLISHED version of a key (retired versions are skipped)."""
        return (
            self.db.execute(
                select(AssessmentDefinition)
                .where(
                    AssessmentDefinition.definition_key == definition_key,
                    AssessmentDefinition.status == AssessmentDefinitionStatus.PUBLISHED,
                )
                .order_by(AssessmentDefinition.version.desc())
            )
            .scalars()
            .first()
        )

    def list_current(self) -> List[AssessmentDefinition]:
        keys = (
            self.db.execute(
                select(AssessmentDefinition.definition_key)
                .where(AssessmentDefinition.status == AssessmentDefinitionStatus.PUBLISHED)
                .distinct()
            )
            .scalars()
            .all()
        )
        found = [self.current(key) for key in sorted(keys)]
        return [d for d in found if d is not None]

    def verify_hash(self, defn: AssessmentDefinition) -> bool:
        """True when a published definition still matches its frozen hash."""
        return defn.content_hash is not None and defn.content_hash == sha256_hex(
            content_payload(defn, self.links(defn.id))
        )

    # -- authoring ------------------------------------------------------------------------

    def create_draft(
        self,
        *,
        author_user_id: str,
        definition_key: str,
        kind: AssessmentKind,
        title: str,
        instructions_md: str,
        criteria: List[dict],
        concept_links: List[dict],
        challenge_spec: Optional[dict] = None,
        independence_policy: Optional[dict] = None,
        grading_policy: Optional[dict] = None,
        allowed_resources: Optional[List[str]] = None,
        project_template_id: Optional[str] = None,
        requires_platform_capability: Optional[str] = None,
    ) -> AssessmentDefinition:
        """``concept_links``: ``{"concept_id", "criterion_keys", "role"?,
        "concept_version_id"?}``. An omitted version is resolved NOW to the
        Concept's current version and pinned — never left as "latest"."""
        latest = self.db.execute(
            select(func.max(AssessmentDefinition.version)).where(
                AssessmentDefinition.definition_key == definition_key
            )
        ).scalar()
        defn = AssessmentDefinition(
            definition_key=definition_key,
            version=(latest or 0) + 1,
            status=AssessmentDefinitionStatus.DRAFT,
            assessment_kind=kind,
            title=title,
            instructions_md=instructions_md,
            produces_evidence_type=KIND_TO_EVIDENCE[kind],
            project_template_id=project_template_id,
            criteria=criteria,
            challenge_spec=challenge_spec,
            independence_policy={**DEFAULT_INDEPENDENCE_POLICY, **(independence_policy or {})},
            grading_policy={**DEFAULT_GRADING_POLICY, **(grading_policy or {})},
            allowed_resources=allowed_resources or [],
            requires_platform_capability=requires_platform_capability,
            author_user_id=author_user_id,
        )
        if kind == AssessmentKind.CAPSTONE:
            defn.independence_policy = {**defn.independence_policy, "fresh_required": "always"}
        self.db.add(defn)
        self.db.flush()
        graph = ConceptGraphService(self.db)
        for link in concept_links:
            version_id = link.get("concept_version_id")
            if version_id is None:
                current = graph.get_current_version(link["concept_id"])
                if current is None:
                    raise ConflictError("A linked Concept has no version to pin.")
                version_id = current.id
            self.db.add(
                AssessmentDefinitionConcept(
                    definition_id=defn.id,
                    concept_id=link["concept_id"],
                    concept_version_id=version_id,
                    role=link.get("role", "assessed"),
                    criterion_keys=list(link.get("criterion_keys") or []),
                )
            )
        self.db.commit()
        self.db.refresh(defn)
        return defn

    def publish(self, definition_id: str) -> AssessmentDefinition:
        defn = self.get(definition_id)
        if defn.status != AssessmentDefinitionStatus.DRAFT:
            raise ConflictError(f"Assessment definition is {defn.status.value}, not draft.")
        links = self.links(defn.id)
        try:
            validate_definition(
                kind=defn.assessment_kind,
                produces_evidence_type=defn.produces_evidence_type,
                criteria=defn.criteria,
                challenge_spec=defn.challenge_spec,
                grading_policy=defn.grading_policy,
                independence_policy=defn.independence_policy,
                concept_links=[{"criterion_keys": l.criterion_keys} for l in links],
                known_check_types=CHECKS.keys(),
            )
        except DefinitionInvalid as exc:
            raise ConflictError(str(exc))
        for link in links:
            version = self.db.get(ConceptVersion, link.concept_version_id)
            if (
                version is None
                or version.concept_id != link.concept_id
                or version.status == VersionStatus.DRAFT
            ):
                raise ConflictError(
                    "Every linked Concept Version must be a published version of that Concept."
                )
        if defn.project_template_id:
            template = self.db.get(ProjectTemplate, defn.project_template_id)
            if template is None or template.status != "published":
                raise ConflictError("The linked project template must be published.")
        defn.status = AssessmentDefinitionStatus.PUBLISHED
        defn.published_at = _utcnow()
        defn.content_hash = sha256_hex(content_payload(defn, links))
        self.db.commit()
        self.db.refresh(defn)
        return defn

    def retire(self, definition_id: str) -> AssessmentDefinition:
        defn = self.get(definition_id)
        if defn.status != AssessmentDefinitionStatus.PUBLISHED:
            raise ConflictError("Only a published definition can be retired.")
        defn.status = AssessmentDefinitionStatus.RETIRED
        self.db.commit()
        return defn

    # -- learner-facing view ------------------------------------------------------------------------

    @staticmethod
    def response_fields(defn: AssessmentDefinition) -> List[Dict]:
        """The inputs this assessment expects, derived from its own checks so the
        UI never hard-codes a form per definition. Challenge items (questions,
        prompts, variants) are rendered from the issued challenge itself."""
        fields: Dict[str, Dict] = {}

        def add(path: str, label: str, kind: str, **extra) -> None:
            fields.setdefault(path, {"path": path, "label": label, "input": kind, **extra})

        for c in defn.criteria:
            if c["method"] != "deterministic":
                continue
            check, kind = c["check"], c["check"]["type"]
            if kind in ("length_bounds", "section_present"):
                add(
                    check["field"],
                    c["label"],
                    "long_text",
                    sections=check.get("sections"),
                    min_chars=check.get("min_chars"),
                )
            elif kind in ("schema_valid", "numeric_match"):
                add(check["field"], c["label"], "text")
            elif kind in (
                "run_exists_owned_terminal",
                "run_after_challenge_start",
                "inputs_match_challenge",
                "artifact_present",
                "cost_within",
            ):
                add(check.get("field", "run_ids"), "The runs you made (paste their ids)", "ids")
            elif kind == "evaluation_run_findings":
                add(
                    check.get("field", "evaluation_run_ids"),
                    "Your app's evaluation runs (paste their ids)",
                    "ids",
                )
            elif kind in ("experiment_concluded", "experiment_claim"):
                add(check.get("field", "experiment_id"), "Your experiment (paste its id)", "id")
                if check.get("claim_field"):
                    add(
                        check["claim_field"],
                        "Which variant did better, according to the recorded results?",
                        "text",
                    )
        entry_kind = (defn.challenge_spec or {}).get("entry_kind")
        if (
            any(c["method"] == "grader" for c in defn.criteria)
            and entry_kind != "prompt"
            and not any(f["input"] == "long_text" for f in fields.values())
        ):
            add("fields.explanation", "Your explanation, in your own words", "long_text")
        return list(fields.values())

    @staticmethod
    def learner_view(defn: AssessmentDefinition, links: List[AssessmentDefinitionConcept]) -> Dict:
        """Everything a learner may know BEFORE they start: what is assessed,
        in plain language, and how each part is decided. Never reference
        answers, check specs or the challenge pool."""
        return {
            "definition_key": defn.definition_key,
            "version": defn.version,
            "kind": defn.assessment_kind.value,
            "title": defn.title,
            "instructions_md": defn.instructions_md,
            "produces_evidence_type": defn.produces_evidence_type.value,
            "allowed_resources": list(defn.allowed_resources or []),
            "mentor_locked_during_attempt": True,
            "time_limit_hours": (defn.grading_policy or {}).get("expiry_hours", 24),
            "retake_cooldown_hours": (defn.grading_policy or {}).get("cooldown_hours", 12),
            "concept_ids": [l.concept_id for l in links],
            "criteria": [
                {
                    "key": c["key"],
                    "label": c["label"],
                    "description": c.get("description", ""),
                    "required": c.get("required", True),
                    "decided_by": "the platform" if c["method"] == "deterministic" else "the Academy Grader",
                    "anchors": c.get("anchors") or {},
                }
                for c in defn.criteria
            ],
            "response_fields": AssessmentDefinitionService.response_fields(defn),
            "evidence_collected": [
                "your responses and any runs, experiments or artifacts you cite",
                "the help level (H0-H5) recorded on your earlier project work",
                "whether you declared using outside help",
            ],
            "requires_platform_capability": defn.requires_platform_capability,
        }
