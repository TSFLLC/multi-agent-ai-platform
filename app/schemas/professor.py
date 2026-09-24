"""AIL.4C.1 Professor contract.

This module contains only the bounded, provider-independent contract used by
the deterministic context assembler and the future Professor generator.  It
does not execute a model, create evidence, or persist a conversation.
"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.radar import ClaimType


class ProfessorIntent(str, Enum):
    ASK_PROFESSOR = "ASK_PROFESSOR"
    EXPLAIN_THIS = "EXPLAIN_THIS"
    WHAT_SHOULD_I_LEARN_NEXT = "WHAT_SHOULD_I_LEARN_NEXT"
    UNDERSTAND_MY_EXPERIMENT = "UNDERSTAND_MY_EXPERIMENT"
    HELP_ME_REVIEW = "HELP_ME_REVIEW"
    WHY_DOES_THIS_MATTER = "WHY_DOES_THIS_MATTER"


class ProfessorTargetType(str, Enum):
    CONCEPT = "concept"
    DEVELOPMENT = "development"
    EXPERIMENT = "experiment"
    REVIEW_ATTEMPT = "review_attempt"


class ProfessorAttachmentType(str, Enum):
    EXPERIMENT = "experiment"
    REVIEW_ATTEMPT = "review_attempt"
    LEARNING_EVIDENCE = "learning_evidence"
    TASK_RUN = "task_run"
    AGENT_RUN = "agent_run"
    ARTIFACT = "artifact"
    MODEL_CALL = "model_call"
    ROUTING_DECISION = "routing_decision"


class ProfessorProvenanceKind(str, Enum):
    LEARNING_RECORD = "learning_record"
    EXTERNAL_KNOWLEDGE = "external_knowledge"
    PROVIDER_CLAIM = "provider_claim"
    PLATFORM_OBSERVATION = "platform_observation"
    USER_AUTHORED_CONCLUSION = "user_authored_conclusion"
    AI_EXPLANATION = "ai_explanation"


class ProfessorTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ProfessorTargetType
    id: str = Field(min_length=1, max_length=36)


class ProfessorAttachment(BaseModel):
    """An explicit, user-selected reference; never an arbitrary search query."""

    model_config = ConfigDict(extra="forbid")

    type: ProfessorAttachmentType
    id: str = Field(min_length=1, max_length=36)
    project_id: Optional[str] = Field(default=None, min_length=1, max_length=36)


class ProfessorContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: ProfessorIntent
    question: Optional[str] = Field(default=None, max_length=4000)
    target: Optional[ProfessorTarget] = None
    attachments: List[ProfessorAttachment] = Field(default_factory=list, max_length=8)

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("question cannot be blank")
        return value.strip() if value is not None else value


class ProfessorEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_type: str = Field(min_length=1, max_length=80)
    ref_id: str = Field(min_length=1, max_length=36)
    role: str = Field(min_length=1, max_length=120)
    provenance_kind: ProfessorProvenanceKind
    claim_type: Optional[ClaimType] = None
    conflict_group: Optional[str] = None


class ProfessorContextRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_type: str = Field(min_length=1, max_length=80)
    ref_id: str = Field(min_length=1, max_length=36)
    role: str = Field(min_length=1, max_length=120)
    provenance_kind: ProfessorProvenanceKind
    claim_type: Optional[ClaimType] = None
    conflict_group: Optional[str] = None
    data: Dict = Field(default_factory=dict)


class ProfessorContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: ProfessorIntent
    user_id: str = Field(min_length=1, max_length=36)
    question: Optional[str] = None
    target: Optional[ProfessorTarget] = None
    records: List[ProfessorContextRecord] = Field(default_factory=list)
    attachments: List[ProfessorAttachment] = Field(default_factory=list)
    deterministic_facts: Dict = Field(default_factory=dict)
    suggested_next_actions: List[Dict] = Field(default_factory=list)

    @property
    def permitted_references(self) -> Dict[str, ProfessorContextRecord]:
        return {f"{record.ref_type}:{record.ref_id}": record for record in self.records}

    @property
    def permitted_attachments(self) -> Dict[str, ProfessorAttachment]:
        return {f"{attachment.type.value}:{attachment.id}": attachment for attachment in self.attachments}


class ProfessorSuggestedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=80)
    target_id: Optional[str] = Field(default=None, max_length=36)
    reason: str = Field(min_length=1, max_length=1000)
    advisory: bool = True

    @field_validator("advisory")
    @classmethod
    def must_be_advisory(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Professor actions are advisory only")
        return value


class ProfessorResponse(BaseModel):
    """Bounded output contract for AIL.4C.2 generation."""

    model_config = ConfigDict(extra="forbid")

    intent: ProfessorIntent
    direct_answer: str = Field(min_length=1, max_length=12000)
    explanation: Optional[str] = Field(default=None, max_length=20000)
    evidence: List[ProfessorEvidenceReference] = Field(default_factory=list, max_length=100)
    uncertainties: List[str] = Field(default_factory=list, max_length=20)
    suggested_next_actions: List[ProfessorSuggestedAction] = Field(default_factory=list, max_length=5)
    attachment_references: List[ProfessorEvidenceReference] = Field(default_factory=list, max_length=8)

    @field_validator("direct_answer")
    @classmethod
    def direct_answer_not_blank(cls, value: str) -> str:
        return value.strip()
