"""Deterministic validation for the AIL.4C Professor response contract."""

import re
from typing import Iterable, Set

from app.errors import ConflictError
from app.models.radar import ClaimType
from app.schemas.professor import (
    ProfessorAssertionKind,
    ProfessorContext,
    ProfessorEvidenceReference,
    ProfessorResponse,
)


class ProfessorResponseValidationError(ConflictError):
    """The generated response cannot be safely grounded in its context."""

    code = "professor_response_invalid"


_FORBIDDEN_LEARNING_ASSERTIONS = (
    r"\bnow\s+(?:understood|practiced|demonstrated)\b",
    r"\byou\s+(?:now\s+)?(?:understand|practice|demonstrate)\b",
    r"\b(counts?|counted)\s+toward\s+learning\b",
    r"\breview\s+(?:passed|failed)\b",
    r"\b(?:marked|set)\s+(?:as\s+)?(?:understood|practiced|demonstrated)\b",
)


def _key(ref_type: str, ref_id: str) -> str:
    return f"{ref_type}:{ref_id}"


def _all_text(response: ProfessorResponse) -> str:
    action_text = " ".join(action.reason for action in response.suggested_next_actions)
    return " ".join(filter(None, [response.direct_answer, response.explanation, action_text])).lower()


def _validate_reference(
    reference: ProfessorEvidenceReference,
    context: ProfessorContext,
    permitted: dict,
    *,
    attachment_only: bool = False,
) -> None:
    record = permitted.get(_key(reference.ref_type, reference.ref_id))
    if record is None:
        kind = "attachment" if attachment_only else "evidence"
        raise ProfessorResponseValidationError(
            f"Professor {kind} reference is outside the assembled context.",
            detail={"ref_type": reference.ref_type, "ref_id": reference.ref_id},
        )
    if reference.provenance_kind != record.provenance_kind:
        raise ProfessorResponseValidationError("Professor provenance kind does not match the context record.")
    if reference.claim_type != record.claim_type:
        raise ProfessorResponseValidationError("Professor Claim Type does not match the context record.")
    if reference.conflict_group != record.conflict_group:
        raise ProfessorResponseValidationError("Professor conflict provenance does not match the context record.")
    if reference.origin is not None and (
        record.claim_provenance is None or reference.origin != record.claim_provenance.origin
    ):
        raise ProfessorResponseValidationError("Professor claim origin does not match the context record.")
    if reference.cited_claims and (
        record.claim_provenance is None or reference.cited_claims != record.claim_provenance.cited_claims
    ):
        raise ProfessorResponseValidationError("Professor claim citations do not match the context record.")
    if attachment_only and _key(reference.ref_type, reference.ref_id) not in context.permitted_attachments:
        raise ProfessorResponseValidationError("Professor attachment reference was not explicitly authorized.")


def _validate_grounded_assertion(reference: ProfessorEvidenceReference, context: ProfessorContext, *, kind: ProfessorAssertionKind) -> None:
    record = context.permitted_references.get(_key(reference.ref_type, reference.ref_id))
    _validate_reference(reference, context, context.permitted_references)
    if kind in {
        ProfessorAssertionKind.FACTUAL,
        ProfessorAssertionKind.PLATFORM_OBSERVATION,
        ProfessorAssertionKind.USER_AUTHORED_CONCLUSION,
    } and record.claim_type == ClaimType.AI_EXPLANATION:
        raise ProfessorResponseValidationError("An AI explanation cannot be used as factual evidence.")


def validate_professor_response(response: ProfessorResponse, context: ProfessorContext) -> ProfessorResponse:
    """Validate grounding, provenance, conflict visibility, and authority boundaries.

    This function is deliberately independent of provider execution and performs
    no database writes.
    """
    if response.intent != context.intent:
        raise ProfessorResponseValidationError("Professor response intent does not match the request.")

    text = _all_text(response)
    for pattern in _FORBIDDEN_LEARNING_ASSERTIONS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            raise ProfessorResponseValidationError(
                "Professor response implies a learning or review state mutation."
            )

    permitted = context.permitted_references
    for reference in response.evidence:
        _validate_reference(reference, context, permitted)
    for reference in response.attachment_references:
        _validate_reference(reference, context, permitted, attachment_only=True)

    if not response.grounded_assertions:
        raise ProfessorResponseValidationError("Professor response must classify its assertions and grounding.")
    for assertion in response.grounded_assertions:
        if assertion.assertion_kind in {
            ProfessorAssertionKind.FACTUAL,
            ProfessorAssertionKind.PLATFORM_OBSERVATION,
            ProfessorAssertionKind.USER_AUTHORED_CONCLUSION,
        } and not assertion.references:
            raise ProfessorResponseValidationError("Factual and platform assertions require provenance references.")
        for reference in assertion.references:
            _validate_grounded_assertion(reference, context, kind=assertion.assertion_kind)

    cited_conflicts: Set[str] = {
        reference.conflict_group
        for reference in response.evidence + response.attachment_references
        if reference.conflict_group
    }
    for group in cited_conflicts:
        group_records = [record for record in context.records if record.conflict_group == group]
        cited_ids = {
            reference.ref_id
            for reference in response.evidence + response.attachment_references
            if reference.conflict_group == group
        }
        if {record.ref_id for record in group_records} - cited_ids:
            raise ProfessorResponseValidationError(
                "Conflicting evidence must remain visible side-by-side.",
                detail={"conflict_group": group},
            )

    for action in response.suggested_next_actions:
        if not action.advisory:
            raise ProfessorResponseValidationError("Professor next actions must be advisory.")
        if action.target_id is not None:
            authorized_ids = {record.ref_id for record in context.records}
            if context.target is not None:
                authorized_ids.add(context.target.id)
            for value in context.deterministic_facts.values():
                if isinstance(value, str):
                    authorized_ids.add(value)
            if action.target_id not in authorized_ids:
                raise ProfessorResponseValidationError("Professor action target is outside the assembled context.")

    return response


def permitted_reference_keys(context: ProfessorContext) -> Iterable[str]:
    """Small inspection helper for the future generator adapter."""
    return context.permitted_references.keys()
