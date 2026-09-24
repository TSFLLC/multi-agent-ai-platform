"""Deterministic validation for the AIL.4C Professor response contract."""

import re
from typing import Iterable, Set

from app.errors import ConflictError
from app.schemas.professor import ProfessorContext, ProfessorEvidenceReference, ProfessorResponse


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
    if attachment_only and _key(reference.ref_type, reference.ref_id) not in context.permitted_attachments:
        raise ProfessorResponseValidationError("Professor attachment reference was not explicitly authorized.")


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

    return response


def permitted_reference_keys(context: ProfessorContext) -> Iterable[str]:
    """Small inspection helper for the future generator adapter."""
    return context.permitted_references.keys()
