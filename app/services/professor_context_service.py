"""AIL.4C.1 deterministic Professor context assembly.

The assembler is intentionally read-only.  It resolves one fixed intent,
loads only the records required for that intent, and emits a bounded context
for the future Professor generator.  It never accepts a model-supplied query
or performs arbitrary project search.
"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFoundError
from app.models.artifacts_eval import Artifact
from app.models.execution import ModelCall, ModelRoutingDecision
from app.models.identity import Project, ProjectMembership
from app.models.learner import LearningEvidence
from app.models.radar import (
    Claim,
    ClaimCitation,
    ClaimOrigin,
    ClaimStatus,
    ClaimType,
    Development,
    DevelopmentConcept,
    DevelopmentConceptState,
)
from app.models.tasks import AgentRun, Task, TaskRun
from app.schemas.professor import (
    ProfessorAttachment,
    ProfessorAttachmentType,
    ProfessorClaimProvenance,
    ProfessorContext,
    ProfessorContextRecord,
    ProfessorContextRequest,
    ProfessorIntent,
    ProfessorProvenanceKind,
    ProfessorProvenanceReference,
    ProfessorTarget,
    ProfessorTargetType,
)
from app.services.concept_graph_service import ConceptGraphService
from app.services.experiment_execution_service import ExperimentExecutionService
from app.services.lab_service import LabService
from app.services.learner_profile_service import LearnerProfileService
from app.services.learner_state_service import LearnerStateService
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.learning_plan_service import LearningPlanService
from app.services.radar_intelligence_service import RadarIntelligenceService
from app.services.radar_service import derive_verification
from app.services.review_attempt_service import ReviewAttemptService
from app.services.stay_ahead_service import StayAheadService

MAX_EVIDENCE_PER_CONCEPT = 40
MAX_PLAN_ITEMS = 20
MAX_CLAIMS_PER_DEVELOPMENT = 50
MAX_RECORDS = 150


class ProfessorContextError(NotFoundError):
    """Fail-closed context error that does not disclose unauthorized records."""

    code = "professor_context_unavailable"


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_value(item) for item in value]
    return value


def _model_data(model: Any, fields: Iterable[str]) -> Dict[str, Any]:
    return {field: _value(getattr(model, field, None)) for field in fields if hasattr(model, field)}


class ProfessorContextAssembler:
    """Assemble one minimum-necessary, user-authorized Professor context."""

    def __init__(self, db: Session):
        self.db = db
        self._concepts = ConceptGraphService(db)
        self._profiles = LearnerProfileService(db)
        self._evidence = LearningEvidenceService(db)
        self._plans = LearningPlanService(db)
        self._states = LearnerStateService(db)

    def assemble(self, user_id: str, request: ProfessorContextRequest) -> ProfessorContext:
        self._validate_intent_target(request.intent, request.target)
        records: List[ProfessorContextRecord] = []
        attachments: List[ProfessorAttachment] = []
        facts: Dict[str, Any] = {}
        actions: List[Dict[str, Any]] = []

        profile = self._profiles.get_profile(user_id)
        if profile is not None:
            self._add(
                records,
                "learner_profile",
                profile.id,
                "learner_profile",
                ProfessorProvenanceKind.LEARNING_RECORD,
                data=_model_data(profile, ["level", "goal_text", "depth", "weekly_minutes", "updated_at"]),
            )
        interests = self._profiles.list_interests(user_id)
        for interest in sorted(interests, key=lambda row: row.id):
            self._add(
                records,
                "learner_interest",
                interest.id,
                "declared_interest",
                ProfessorProvenanceKind.LEARNING_RECORD,
                data=_model_data(interest, ["term_id", "concept_id", "watch"]),
            )

        if request.intent == ProfessorIntent.WHAT_SHOULD_I_LEARN_NEXT:
            next_action = self._assemble_next(user_id, records, facts)
            if next_action is not None:
                actions.append(next_action)
        elif request.intent == ProfessorIntent.ASK_PROFESSOR:
            # ASK_PROFESSOR deliberately has no implicit search.  The question
            # is passed to the future generator, while records remain empty
            # unless the caller explicitly supplied an attachment.
            pass
        elif request.target is not None:
            if request.target.type == ProfessorTargetType.CONCEPT:
                self._assemble_concept(user_id, request.target, records, facts)
            elif request.target.type == ProfessorTargetType.EXPERIMENT:
                self._assemble_experiment(user_id, request.target, records, facts)
            elif request.target.type == ProfessorTargetType.REVIEW_ATTEMPT:
                self._assemble_review(user_id, request.target, records, facts)
            elif request.target.type == ProfessorTargetType.DEVELOPMENT:
                self._assemble_development(user_id, request.target, records, facts)

        for attachment in request.attachments:
            record = self._authorize_attachment(user_id, attachment)
            attachments.append(attachment)
            records.append(record)

        if len(records) > MAX_RECORDS:
            records = records[:MAX_RECORDS]

        return ProfessorContext(
            intent=request.intent,
            user_id=user_id,
            question=request.question,
            target=request.target,
            records=records,
            attachments=attachments,
            deterministic_facts=facts,
            suggested_next_actions=actions,
        )

    @staticmethod
    def _validate_intent_target(intent: ProfessorIntent, target: Optional[ProfessorTarget]) -> None:
        required = {
            ProfessorIntent.EXPLAIN_THIS,
            ProfessorIntent.UNDERSTAND_MY_EXPERIMENT,
            ProfessorIntent.HELP_ME_REVIEW,
            ProfessorIntent.WHY_DOES_THIS_MATTER,
        }
        if intent in required and target is None:
            raise ProfessorContextError("This Professor intent requires an explicit target.")
        if intent == ProfessorIntent.UNDERSTAND_MY_EXPERIMENT and target and target.type != ProfessorTargetType.EXPERIMENT:
            raise ProfessorContextError("UNDERSTAND_MY_EXPERIMENT requires an experiment target.")
        if intent == ProfessorIntent.HELP_ME_REVIEW and target and target.type not in {
            ProfessorTargetType.REVIEW_ATTEMPT,
            ProfessorTargetType.CONCEPT,
        }:
            raise ProfessorContextError("HELP_ME_REVIEW requires a review attempt or Concept target.")
        if intent == ProfessorIntent.WHY_DOES_THIS_MATTER and target and target.type != ProfessorTargetType.DEVELOPMENT:
            raise ProfessorContextError("WHY_DOES_THIS_MATTER requires a Development target.")

    @staticmethod
    def _add(
        records: List[ProfessorContextRecord],
        ref_type: str,
        ref_id: str,
        role: str,
        provenance_kind: ProfessorProvenanceKind,
        *,
        claim_type: Optional[ClaimType] = None,
        conflict_group: Optional[str] = None,
        claim_provenance: Optional[ProfessorClaimProvenance] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> ProfessorContextRecord:
        record = ProfessorContextRecord(
            ref_type=ref_type,
            ref_id=ref_id,
            role=role,
            provenance_kind=provenance_kind,
            claim_type=claim_type,
            conflict_group=conflict_group,
            claim_provenance=claim_provenance,
            data=_value(data or {}),
        )
        records.append(record)
        return record

    def _assemble_concept(
        self, user_id: str, target: ProfessorTarget, records: List[ProfessorContextRecord], facts: Dict[str, Any]
    ) -> None:
        concept = self._concepts.get_concept(target.id)
        version = self._concepts.get_current_version(target.id)
        if concept is None or version is None:
            raise ProfessorContextError("Concept context is unavailable.")

        self._add(
            records,
            "concept",
            concept.id,
            "concept",
            ProfessorProvenanceKind.LEARNING_RECORD,
            data=_model_data(concept, ["slug", "name", "level", "kind", "is_core", "status"]),
        )
        self._add(
            records,
            "concept_version",
            version.id,
            "current_concept_version",
            ProfessorProvenanceKind.LEARNING_RECORD,
            data=_model_data(
                version,
                [
                    "concept_id",
                    "version",
                    "plain_definition",
                    "technical_explanation",
                    "examples_md",
                    "content_origin",
                    "reviewed_at",
                    "change_note",
                    "change_severity",
                ],
            ),
        )
        prerequisites = sorted(self._concepts.get_prerequisite_closure(concept.id))
        facts["prerequisite_concept_ids"] = prerequisites
        for prerequisite_id in prerequisites:
            prerequisite = self._concepts.get_concept(prerequisite_id)
            if prerequisite is not None:
                self._add(
                    records,
                    "concept",
                    prerequisite.id,
                    "prerequisite",
                    ProfessorProvenanceKind.LEARNING_RECORD,
                    data=_model_data(prerequisite, ["slug", "name", "level", "kind", "is_core", "status"]),
                )

        items = sorted(self._concepts.list_learning_items(concept.id), key=lambda item: item.id)
        for item in items[:20]:
            self._add(
                records,
                "learning_item",
                item.id,
                "learning_item",
                ProfessorProvenanceKind.LEARNING_RECORD,
                data=_model_data(item, ["concept_id", "item_type", "title", "body_md", "grading_mode", "reviewed", "version", "est_minutes"]),
            )

        state = self._states.state(user_id, concept.id)
        facts["learner_state"] = {"ladder": state.ladder, "overlays": sorted(state.overlays)}
        for evidence in state.evidence[:MAX_EVIDENCE_PER_CONCEPT]:
            self._add(
                records,
                "learning_evidence",
                evidence.id,
                "learning_evidence",
                ProfessorProvenanceKind.LEARNING_RECORD,
                data=_model_data(
                    evidence,
                    ["concept_id", "concept_version_id", "evidence_type", "learning_item_id", "score", "passed", "grader", "question_origin", "on_demo_data", "ref_type", "ref_id", "created_at"],
                ),
            )

        plan = [item for item in self._plans.get_plan(user_id) if item.concept_id == concept.id]
        for item in plan:
            self._add(
                records,
                "learning_plan_item",
                item.id,
                "learning_plan",
                ProfessorProvenanceKind.LEARNING_RECORD,
                data=_model_data(item, ["concept_id", "position", "state", "origin"]),
            )

    def _assemble_next(self, user_id: str, records: List[ProfessorContextRecord], facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        today = StayAheadService(self.db).today(user_id)
        today_data = today.model_dump(mode="json")
        self._add(
            records,
            "today",
            user_id,
            "today_signals",
            ProfessorProvenanceKind.LEARNING_RECORD,
            data=today_data,
        )
        facts["today_signal_count"] = sum(
            len(section["items"])
            for section in today.sections.model_dump(mode="python").values()
            if isinstance(section, dict) and "items" in section
        )
        plan = sorted(self._plans.get_plan(user_id), key=lambda item: (item.position, item.id))
        candidate = next((item for item in plan if _value(item.state) in {"planned", "proposed"}), None)
        if candidate is None:
            generated = self._plans.generate_plan(user_id)
            candidate_data = generated[0] if generated else None
            if candidate_data is None:
                facts["next_action_source"] = "learning_plan"
                return None
            concept_id = candidate_data.concept_id
            reason = candidate_data.reason
            position = candidate_data.position
            facts["next_action_source"] = "planner_preview"
        else:
            concept_id = candidate.concept_id
            reason = "This Concept is the next active item in your Learning Plan."
            position = candidate.position
            facts["next_action_source"] = "learning_plan"

        facts["next_concept_id"] = concept_id
        facts["next_position"] = position
        target = ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=concept_id)
        self._assemble_concept(user_id, target, records, facts)
        return {"type": "learn", "target_id": concept_id, "reason": reason, "advisory": True}

    def _assemble_experiment(
        self, user_id: str, target: ProfessorTarget, records: List[ProfessorContextRecord], facts: Dict[str, Any]
    ) -> None:
        experiment = LabService(self.db).get_experiment(user_id, target.id)
        execution = ExperimentExecutionService(self.db)
        data = LabService(self.db).experiment_read(experiment)
        data["execution_state"] = execution._state_summary(experiment)
        self._add(
            records,
            "experiment",
            experiment.id,
            "experiment",
            ProfessorProvenanceKind.PLATFORM_OBSERVATION,
            claim_type=ClaimType.PLATFORM_OBSERVATION,
            data=data,
        )
        if experiment.conclusion_type or experiment.conclusion_text:
            self._add(
                records,
                "experiment_conclusion",
                experiment.id,
                "user_authored_conclusion",
                ProfessorProvenanceKind.USER_AUTHORED_CONCLUSION,
                data={
                    "experiment_id": experiment.id,
                    "conclusion_type": experiment.conclusion_type,
                    "conclusion_text": experiment.conclusion_text,
                    "concluded_at": _value(experiment.concluded_at),
                },
            )
        facts["experiment_status"] = _value(experiment.status)

    def _assemble_review(
        self, user_id: str, target: ProfessorTarget, records: List[ProfessorContextRecord], facts: Dict[str, Any]
    ) -> None:
        attempt = None
        if target.type == ProfessorTargetType.REVIEW_ATTEMPT:
            attempt = ReviewAttemptService(self.db).get(user_id, target.id)
            concept_id = attempt.concept_id
        else:
            concept_id = target.id
            state = self._states.state(user_id, concept_id)
            if state.review and state.review.latest_completed:
                attempt = ReviewAttemptService(self.db).get(user_id, state.review.latest_completed.id)
        if attempt is None:
            raise ProfessorContextError("Review context is unavailable.")
        state = self._states.state(user_id, concept_id)
        self._add(
            records,
            "review_attempt",
            attempt.id,
            "review_attempt",
            ProfessorProvenanceKind.LEARNING_RECORD,
            data=_model_data(attempt, ["concept_id", "concept_version_id", "learning_item_id", "status", "started_at", "completed_at", "resulting_learning_evidence_id"]),
        )
        facts["learner_state"] = {"ladder": state.ladder, "overlays": sorted(state.overlays)}
        facts["review_due"] = bool(state.review and state.review.due)
        facts["review_failed"] = bool(state.review and state.review.failed)
        if attempt.resulting_learning_evidence_id:
            evidence = self.db.get(LearningEvidence, attempt.resulting_learning_evidence_id)
            if evidence is not None and evidence.user_id == user_id:
                self._add(
                    records,
                    "learning_evidence",
                    evidence.id,
                    "review_resulting_evidence",
                    ProfessorProvenanceKind.LEARNING_RECORD,
                    data=_model_data(evidence, ["concept_id", "concept_version_id", "evidence_type", "passed", "grader", "created_at"]),
                )

    def _assemble_development(
        self, user_id: str, target: ProfessorTarget, records: List[ProfessorContextRecord], facts: Dict[str, Any]
    ) -> None:
        development = self.db.get(Development, target.id)
        if development is None:
            raise ProfessorContextError("Development context is unavailable.")
        self._add(
            records,
            "development",
            development.id,
            "radar_development",
            ProfessorProvenanceKind.EXTERNAL_KNOWLEDGE,
            data=_model_data(development, ["title", "development_type", "announced_at", "effective_at", "first_seen_at", "status"]),
        )
        claims = list(
            self.db.execute(
                select(Claim)
                .where(Claim.development_id == development.id, Claim.status.in_([ClaimStatus.ACTIVE, ClaimStatus.DISPUTED]))
                .order_by(Claim.as_of, Claim.id)
                .limit(MAX_CLAIMS_PER_DEVELOPMENT)
            ).scalars()
        )
        claim_ids = {claim.id for claim in claims}
        conflict_keys: Dict[str, List[Claim]] = {}
        for claim in claims:
            key = (claim.conditions or {}).get("conflict_key")
            if key:
                conflict_keys.setdefault(str(key), []).append(claim)
        conflict_groups = {
            id(claim): f"claim_conflict:{key}"
            for key, rows in conflict_keys.items()
            if len({row.text.strip() for row in rows}) > 1 or any(row.status == ClaimStatus.DISPUTED for row in rows)
            for claim in rows
        }
        for claim in claims:
            if claim.claim_type == ClaimType.PROVIDER_CLAIM:
                provenance = ProfessorProvenanceKind.PROVIDER_CLAIM
            elif claim.claim_type == ClaimType.PLATFORM_OBSERVATION:
                provenance = ProfessorProvenanceKind.PLATFORM_OBSERVATION
            elif claim.claim_type == ClaimType.AI_EXPLANATION:
                provenance = ProfessorProvenanceKind.AI_EXPLANATION
            else:
                provenance = ProfessorProvenanceKind.EXTERNAL_KNOWLEDGE
            origin = self.db.execute(
                select(ClaimOrigin).where(ClaimOrigin.claim_id == claim.id)
            ).scalar_one_or_none()
            claim_provenance = None
            if origin is not None:
                if origin.source_item_id:
                    origin_ref = ProfessorProvenanceReference(ref_type="radar_item", ref_id=origin.source_item_id)
                elif origin.evaluation_id:
                    origin_ref = ProfessorProvenanceReference(ref_type="evaluation", ref_id=origin.evaluation_id)
                elif origin.agent_run_id:
                    origin_ref = ProfessorProvenanceReference(ref_type="agent_run", ref_id=origin.agent_run_id)
                else:
                    origin_ref = None
                if origin_ref is not None:
                    citations = list(
                        self.db.execute(
                            select(ClaimCitation.cited_claim_id)
                            .where(ClaimCitation.explanation_claim_id == claim.id)
                            .order_by(ClaimCitation.cited_claim_id)
                            .limit(20)
                        ).scalars()
                    )
                    citations = [claim_id for claim_id in citations if claim_id in claim_ids]
                    claim_provenance = ProfessorClaimProvenance(
                        origin_kind=_value(origin.origin_kind),
                        origin=origin_ref,
                        cited_claims=[
                            ProfessorProvenanceReference(ref_type="claim", ref_id=claim_id)
                            for claim_id in citations
                        ],
                    )
            self._add(
                records,
                "claim",
                claim.id,
                "development_claim",
                provenance,
                claim_type=claim.claim_type,
                conflict_group=conflict_groups.get(id(claim)),
                claim_provenance=claim_provenance,
                data=_model_data(claim, ["text", "quote_span", "conditions", "as_of", "created_by", "status", "model_id", "development_id"]),
            )
        concept_links = list(
            self.db.execute(
                select(DevelopmentConcept).where(
                    DevelopmentConcept.development_id == development.id,
                    DevelopmentConcept.state == DevelopmentConceptState.CONFIRMED,
                )
            ).scalars()
        )
        facts["verification_level"] = derive_verification(self.db, development.id)
        facts["relevance_reason_codes"] = [
            reason.value
            for reason in RadarIntelligenceService(self.db).relevance_reason_codes(user_id, development.id)
        ]
        facts["confirmed_concept_ids"] = sorted(link.concept_id for link in concept_links)
        triage = RadarIntelligenceService(self.db).current_triage(user_id, development_id=development.id)
        if triage is not None:
            facts["triage"] = _model_data(triage, ["decision", "rationale", "reason_codes", "revisit_at", "decided_at"])

    def _authorize_attachment(self, user_id: str, attachment: ProfessorAttachment) -> ProfessorContextRecord:
        if attachment.type == ProfessorAttachmentType.EXPERIMENT:
            experiment = LabService(self.db).get_experiment(user_id, attachment.id)
            return self._attachment_record(attachment, "owned_experiment", _model_data(experiment, ["status", "hypothesis", "conclusion_type", "conclusion_text"]))
        if attachment.type == ProfessorAttachmentType.REVIEW_ATTEMPT:
            attempt = ReviewAttemptService(self.db).get(user_id, attachment.id)
            return self._attachment_record(attachment, "owned_review_attempt", _model_data(attempt, ["concept_id", "status", "completed_at"]))
        if attachment.type == ProfessorAttachmentType.LEARNING_EVIDENCE:
            evidence = self.db.get(LearningEvidence, attachment.id)
            if evidence is None or evidence.user_id != user_id:
                raise ProfessorContextError("Learning Evidence attachment is unavailable.")
            return self._attachment_record(attachment, "owned_learning_evidence", _model_data(evidence, ["concept_id", "evidence_type", "passed", "created_at"]))

        project_id = attachment.project_id
        if not project_id or not self._project_is_opted_in_and_accessible(user_id, project_id):
            raise ProfessorContextError("Platform attachment is not authorized.")
        project = self.db.get(Project, project_id)
        entity = self._platform_entity(attachment, project_id)
        if entity is None:
            raise ProfessorContextError("Platform attachment is unavailable.")
        return self._attachment_record(
            attachment,
            "explicit_platform_attachment",
            {"project_id": project.id, "selected": True},
            claim_type=ClaimType.PLATFORM_OBSERVATION,
        )

    def _attachment_record(
        self,
        attachment: ProfessorAttachment,
        role: str,
        data: Dict[str, Any],
        *,
        claim_type: Optional[ClaimType] = None,
    ) -> ProfessorContextRecord:
        provenance = (
            ProfessorProvenanceKind.PLATFORM_OBSERVATION
            if claim_type == ClaimType.PLATFORM_OBSERVATION
            else ProfessorProvenanceKind.LEARNING_RECORD
        )
        return ProfessorContextRecord(
            ref_type=attachment.type.value,
            ref_id=attachment.id,
            role=role,
            provenance_kind=provenance,
            claim_type=claim_type,
            data=_value(data),
        )

    def _project_is_opted_in_and_accessible(self, user_id: str, project_id: str) -> bool:
        return self.db.execute(
            select(Project.id)
            .join(ProjectMembership, ProjectMembership.project_id == Project.id)
            .where(
                Project.id == project_id,
                ProjectMembership.user_id == user_id,
                Project.ail_evidence_opt_in.is_(True),
            )
        ).scalar_one_or_none() is not None

    def _platform_entity(self, attachment: ProfessorAttachment, project_id: str) -> Optional[Any]:
        if attachment.type == ProfessorAttachmentType.TASK_RUN:
            entity = self.db.get(TaskRun, attachment.id)
            if entity is None:
                return None
            task = self.db.get(Task, entity.task_id)
            return entity if task is not None and task.project_id == project_id else None
        if attachment.type == ProfessorAttachmentType.AGENT_RUN:
            entity = self.db.get(AgentRun, attachment.id)
            if entity is None:
                return None
            task_run = self.db.get(TaskRun, entity.task_run_id)
            task = self.db.get(Task, task_run.task_id) if task_run else None
            return entity if task is not None and task.project_id == project_id else None
        if attachment.type == ProfessorAttachmentType.ARTIFACT:
            entity = self.db.get(Artifact, attachment.id)
            if entity is None:
                return None
            agent_run = self.db.get(AgentRun, entity.agent_run_id)
            return self._platform_entity(
                ProfessorAttachment(type=ProfessorAttachmentType.AGENT_RUN, id=agent_run.id, project_id=project_id),
                project_id,
            ) if agent_run else None
        if attachment.type == ProfessorAttachmentType.MODEL_CALL:
            entity = self.db.get(ModelCall, attachment.id)
            if entity is None:
                return None
            agent_run = self.db.get(AgentRun, entity.agent_run_id)
            return self._platform_entity(
                ProfessorAttachment(type=ProfessorAttachmentType.AGENT_RUN, id=agent_run.id, project_id=project_id),
                project_id,
            ) if agent_run else None
        if attachment.type == ProfessorAttachmentType.ROUTING_DECISION:
            entity = self.db.get(ModelRoutingDecision, attachment.id)
            if entity is None:
                return None
            agent_run = self.db.get(AgentRun, entity.agent_run_id)
            return self._platform_entity(
                ProfessorAttachment(type=ProfessorAttachmentType.AGENT_RUN, id=agent_run.id, project_id=project_id),
                project_id,
            ) if agent_run else None
        return None
