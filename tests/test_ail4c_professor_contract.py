"""AIL.4C.1 Professor contract/context tests.

These tests deliberately stop before provider execution.  The assembler and
validator must be safe to call without creating Agent Runs or learning data.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.enums import (
    ConceptKind,
    EvidenceType,
    ExecutionMode,
    ExperimentStatus,
    ExperimentType,
    GradingMode,
    LearningItemType,
    TaskRunStatus,
)
from app.errors import NotFoundError
from app.models.lab import Experiment
from app.models.learner import LearningEvidence, LearningPlanItem
from app.models.learning_review import ReviewAttempt, ReviewPromptDelivery
from app.models.radar import Claim, ClaimCreationMethod, ClaimStatus, ClaimType, Development
from app.models.tasks import TaskRun
from app.schemas.professor import (
    ProfessorAttachment,
    ProfessorAttachmentType,
    ProfessorContext,
    ProfessorContextRecord,
    ProfessorContextRequest,
    ProfessorEvidenceReference,
    ProfessorClaimProvenance,
    ProfessorProvenanceReference,
    ProfessorAssertionKind,
    ProfessorGroundedAssertion,
    ProfessorIntent,
    ProfessorProvenanceKind,
    ProfessorResponse,
    ProfessorSuggestedAction,
    ProfessorTarget,
    ProfessorTargetType,
)
from app.services.concept_graph_service import ConceptGraphService
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.professor_context_service import ProfessorContextAssembler
from app.services.professor_contract import (
    ProfessorResponseValidationError,
    validate_professor_response,
)
from tests.ail1a_factories import make_concept, make_published_version, make_user
from tests.conftest import make_task


def _context(intent=ProfessorIntent.EXPLAIN_THIS, *, records=None, attachments=None):
    return ProfessorContext(
        intent=intent,
        user_id="user-1",
        records=records or [],
        attachments=attachments or [],
    )


def _record(ref_type, ref_id, *, provenance=ProfessorProvenanceKind.LEARNING_RECORD, claim_type=None, conflict=None):
    return ProfessorContextRecord(
        ref_type=ref_type,
        ref_id=ref_id,
        role="test",
        provenance_kind=provenance,
        claim_type=claim_type,
        conflict_group=conflict,
        data={},
    )


def test_all_six_experiences_use_one_fixed_intent_contract():
    assert [intent.value for intent in ProfessorIntent] == [
        "ASK_PROFESSOR",
        "EXPLAIN_THIS",
        "WHAT_SHOULD_I_LEARN_NEXT",
        "UNDERSTAND_MY_EXPERIMENT",
        "HELP_ME_REVIEW",
        "WHY_DOES_THIS_MATTER",
    ]


def test_response_requires_context_membership_and_preserves_provenance():
    context = _context(records=[_record("claim", "claim-1")])
    valid = ProfessorResponse(
        intent=ProfessorIntent.EXPLAIN_THIS,
        direct_answer="The recorded item is available.",
        evidence=[
            ProfessorEvidenceReference(
                ref_type="claim",
                ref_id="claim-1",
                role="support",
                provenance_kind=ProfessorProvenanceKind.LEARNING_RECORD,
            )
        ],
        grounded_assertions=[
            ProfessorGroundedAssertion(
                assertion_kind=ProfessorAssertionKind.FACTUAL,
                text="The recorded item is available.",
                references=[
                    ProfessorEvidenceReference(
                        ref_type="claim",
                        ref_id="claim-1",
                        role="support",
                        provenance_kind=ProfessorProvenanceKind.LEARNING_RECORD,
                    )
                ],
            )
        ],
    )
    assert validate_professor_response(valid, context) == valid

    invalid = valid.model_copy(
        update={
            "evidence": [
                ProfessorEvidenceReference(
                    ref_type="claim",
                    ref_id="not-in-context",
                    role="support",
                    provenance_kind=ProfessorProvenanceKind.LEARNING_RECORD,
                )
            ]
        }
    )
    with pytest.raises(ProfessorResponseValidationError):
        validate_professor_response(invalid, context)


def test_conflicting_evidence_must_be_returned_side_by_side():
    records = [
        _record("claim", "claim-a", conflict="conflict-1"),
        _record("claim", "claim-b", conflict="conflict-1"),
    ]
    context = _context(records=records)
    one_side = ProfessorResponse(
        intent=ProfessorIntent.EXPLAIN_THIS,
        direct_answer="There is evidence to compare.",
        evidence=[
            ProfessorEvidenceReference(
                ref_type="claim",
                ref_id="claim-a",
                role="support",
                provenance_kind=ProfessorProvenanceKind.LEARNING_RECORD,
                conflict_group="conflict-1",
            )
        ],
        grounded_assertions=[
            ProfessorGroundedAssertion(
                assertion_kind=ProfessorAssertionKind.FACTUAL,
                text="There is evidence to compare.",
                references=[
                    ProfessorEvidenceReference(
                        ref_type="claim",
                        ref_id="claim-a",
                        role="support",
                        provenance_kind=ProfessorProvenanceKind.LEARNING_RECORD,
                        conflict_group="conflict-1",
                    )
                ],
            )
        ],
    )
    with pytest.raises(ProfessorResponseValidationError):
        validate_professor_response(one_side, context)


def test_response_cannot_advance_learning_or_review_state():
    context = _context()
    response = ProfessorResponse(
        intent=ProfessorIntent.HELP_ME_REVIEW,
        direct_answer="You are now demonstrated.",
        grounded_assertions=[
            ProfessorGroundedAssertion(
                assertion_kind=ProfessorAssertionKind.AI_EXPLANATION,
                text="You are now demonstrated.",
            )
        ],
    )
    with pytest.raises(ProfessorResponseValidationError):
        validate_professor_response(response, context)

    with pytest.raises(ValueError):
        ProfessorSuggestedAction(type="mark_state", reason="change state", advisory=False)


def test_unsupported_factual_assertion_requires_grounding():
    context = _context(records=[_record("claim", "claim-1", claim_type=ClaimType.FACT)])
    response = ProfessorResponse(
        intent=ProfessorIntent.EXPLAIN_THIS,
        direct_answer="The provider is faster.",
    )
    with pytest.raises(ProfessorResponseValidationError):
        validate_professor_response(response, context)


def test_ai_explanation_may_be_unreferenced_but_cannot_become_evidence():
    explanation = ProfessorResponse(
        intent=ProfessorIntent.ASK_PROFESSOR,
        direct_answer="This is an interpretation, not a recorded fact.",
        grounded_assertions=[
            ProfessorGroundedAssertion(
                assertion_kind=ProfessorAssertionKind.AI_EXPLANATION,
                text="This is an interpretation, not a recorded fact.",
            )
        ],
    )
    assert validate_professor_response(explanation, _context(ProfessorIntent.ASK_PROFESSOR)) == explanation

    context = _context(
        records=[
            _record("claim", "claim-ai", provenance=ProfessorProvenanceKind.AI_EXPLANATION, claim_type=ClaimType.AI_EXPLANATION)
        ]
    )
    factual = ProfessorResponse(
        intent=ProfessorIntent.EXPLAIN_THIS,
        direct_answer="The record proves the fact.",
        grounded_assertions=[
            ProfessorGroundedAssertion(
                assertion_kind=ProfessorAssertionKind.FACTUAL,
                text="The record proves the fact.",
                references=[
                    ProfessorEvidenceReference(
                        ref_type="claim", ref_id="claim-ai", role="support",
                        provenance_kind=ProfessorProvenanceKind.AI_EXPLANATION,
                        claim_type=ClaimType.AI_EXPLANATION,
                    )
                ],
            )
        ],
    )
    with pytest.raises(ProfessorResponseValidationError):
        validate_professor_response(factual, context)


def test_claim_origin_and_citation_references_must_match_context():
    provenance = ProfessorClaimProvenance(
        origin_kind="source_item",
        origin=ProfessorProvenanceReference(ref_type="radar_item", ref_id="source-1"),
        cited_claims=[ProfessorProvenanceReference(ref_type="claim", ref_id="claim-2")],
    )
    record = _record(
        "claim",
        "claim-1",
        provenance=ProfessorProvenanceKind.EXTERNAL_KNOWLEDGE,
        claim_type=ClaimType.RESEARCH_RESULT,
    )
    record.claim_provenance = provenance
    context = _context(records=[record, _record("claim", "claim-2")])
    good = ProfessorResponse(
        intent=ProfessorIntent.EXPLAIN_THIS,
        direct_answer="The source-backed claim is recorded.",
        grounded_assertions=[
            ProfessorGroundedAssertion(
                assertion_kind=ProfessorAssertionKind.FACTUAL,
                text="The source-backed claim is recorded.",
                references=[
                    ProfessorEvidenceReference(
                        ref_type="claim", ref_id="claim-1", role="support",
                        provenance_kind=ProfessorProvenanceKind.EXTERNAL_KNOWLEDGE,
                        claim_type=ClaimType.RESEARCH_RESULT,
                        origin=provenance.origin,
                        cited_claims=provenance.cited_claims,
                    )
                ],
            )
        ],
    )
    assert validate_professor_response(good, context) == good

    bad = good.model_copy(deep=True)
    bad.grounded_assertions[0].references[0].origin = ProfessorProvenanceReference(
        ref_type="radar_item", ref_id="unauthorized-source"
    )
    with pytest.raises(ProfessorResponseValidationError):
        validate_professor_response(bad, context)


def test_advisory_actions_have_fixed_types_and_authorized_targets():
    with pytest.raises(ValueError):
        ProfessorSuggestedAction(type="delete_learning_evidence", reason="delete it")

    context = _context(records=[_record("concept", "concept-1")])
    unauthorized = ProfessorResponse(
        intent=ProfessorIntent.EXPLAIN_THIS,
        direct_answer="An explanation.",
        grounded_assertions=[
            ProfessorGroundedAssertion(assertion_kind=ProfessorAssertionKind.AI_EXPLANATION, text="An explanation.")
        ],
        suggested_next_actions=[
            ProfessorSuggestedAction(type="learn", target_id="other-concept", reason="Study it next")
        ],
    )
    with pytest.raises(ProfessorResponseValidationError):
        validate_professor_response(unauthorized, context)

    valid = unauthorized.model_copy(deep=True)
    valid.suggested_next_actions[0].target_id = "concept-1"
    assert validate_professor_response(valid, context) == valid


def test_concept_context_is_user_scoped_and_read_only(db, bootstrap):
    concept = make_concept(db, slug="professor-concept", name="Professor Concept", kind=ConceptKind.DEFINITIONAL)
    version = make_published_version(db, concept)
    ConceptGraphService(db).create_learning_item(
        concept_id=concept.id,
        item_type=LearningItemType.RESOURCE,
        title="A bounded lesson",
        body_md="A safe explanation.",
        reviewed=True,
    )
    db.commit()

    before = (len(db.new), len(db.dirty), len(db.deleted))
    context = ProfessorContextAssembler(db).assemble(
        bootstrap.user.id,
        ProfessorContextRequest(
            intent=ProfessorIntent.EXPLAIN_THIS,
            target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=concept.id),
        ),
    )
    after = (len(db.new), len(db.dirty), len(db.deleted))

    assert before == after == (0, 0, 0)
    assert {record.ref_type for record in context.records} >= {"concept", "concept_version", "learning_item"}
    assert context.deterministic_facts["learner_state"]["ladder"] == "not_started"
    assert version.id in {record.ref_id for record in context.records if record.ref_type == "concept_version"}


def test_professor_context_and_validation_do_not_mutate_protected_domains(db, bootstrap):
    concept = make_concept(db, slug="professor-snapshot-concept")
    version = make_published_version(db, concept)
    development = Development(
        title="Snapshot development",
        development_type="release",
        candidate_key="professor-snapshot-development",
        first_seen_at=datetime.now(timezone.utc),
    )
    db.add(development)
    db.flush()
    db.add(
        Claim(
            text="A recorded fact",
            claim_type=ClaimType.FACT,
            development_id=development.id,
            as_of=datetime.now(timezone.utc),
            created_by=ClaimCreationMethod.USER,
            status=ClaimStatus.ACTIVE,
        )
    )
    db.add(
        Experiment(
            user_id=bootstrap.user.id,
            experiment_type=ExperimentType.VARIANCE,
            status=ExperimentStatus.COMPLETED,
            hypothesis="A bounded hypothesis",
            repetitions=1,
            config_snapshot={"bounded": True},
            estimated_currency="USD",
            conclusion_type="observation",
            conclusion_text="A user conclusion",
            concluded_at=datetime.now(timezone.utc),
        )
    )
    db.commit()

    def snapshot():
        return {
            "evidence": db.execute(select(LearningEvidence.id, LearningEvidence.passed, LearningEvidence.score)).all(),
            "plan": db.execute(select(LearningPlanItem.id, LearningPlanItem.state, LearningPlanItem.position)).all(),
            "claims": db.execute(select(Claim.id, Claim.text, Claim.status)).all(),
            "experiments": db.execute(select(Experiment.id, Experiment.conclusion_type, Experiment.conclusion_text)).all(),
            "reviews": db.execute(select(ReviewAttempt.id, ReviewAttempt.status, ReviewAttempt.resulting_learning_evidence_id)).all(),
            "deliveries": db.execute(select(ReviewPromptDelivery.id, ReviewPromptDelivery.slot)).all(),
        }

    before = snapshot()
    context = ProfessorContextAssembler(db).assemble(
        bootstrap.user.id,
        ProfessorContextRequest(
            intent=ProfessorIntent.WHY_DOES_THIS_MATTER,
            target=ProfessorTarget(type=ProfessorTargetType.DEVELOPMENT, id=development.id),
        ),
    )
    validate_professor_response(
        ProfessorResponse(
            intent=ProfessorIntent.WHY_DOES_THIS_MATTER,
            direct_answer="This is an interpretation of the recorded material.",
            grounded_assertions=[
                ProfessorGroundedAssertion(
                    assertion_kind=ProfessorAssertionKind.AI_EXPLANATION,
                    text="This is an interpretation of the recorded material.",
                )
            ],
        ),
        context,
    )
    assert snapshot() == before


def test_cross_user_learning_attachment_fails_closed(db, bootstrap):
    other = make_user(db, org=bootstrap.organization, email="other-professor@example.com")
    concept = make_concept(db, slug="private-professor-concept")
    version = make_published_version(db, concept)
    evidence = LearningEvidenceService(db).record_evidence(
        user_id=other.id,
        concept_id=concept.id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.LESSON_COMPLETED,
        grader=GradingMode.DETERMINISTIC,
        commit=False,
    )
    db.commit()

    request = ProfessorContextRequest(
        intent=ProfessorIntent.ASK_PROFESSOR,
        attachments=[ProfessorAttachment(type=ProfessorAttachmentType.LEARNING_EVIDENCE, id=evidence.id)],
    )
    with pytest.raises(NotFoundError):
        ProfessorContextAssembler(db).assemble(bootstrap.user.id, request)


def test_platform_attachment_requires_project_opt_in_and_membership(db, bootstrap):
    task = make_task(db, project=bootstrap.project, execution_mode=ExecutionMode.SINGLE_AGENT)
    run = TaskRun(task_id=task.id, status=TaskRunStatus.CREATED)
    db.add(run)
    db.commit()

    request = ProfessorContextRequest(
        intent=ProfessorIntent.ASK_PROFESSOR,
        attachments=[
            ProfessorAttachment(
                type=ProfessorAttachmentType.TASK_RUN,
                id=run.id,
                project_id=bootstrap.project.id,
            )
        ],
    )
    with pytest.raises(NotFoundError):
        ProfessorContextAssembler(db).assemble(bootstrap.user.id, request)

    bootstrap.project.ail_evidence_opt_in = True
    db.commit()
    context = ProfessorContextAssembler(db).assemble(bootstrap.user.id, request)
    assert context.attachments[0].id == run.id
    assert context.records[-1].claim_type.value == "PLATFORM_OBSERVATION"


def test_experiment_conclusion_is_separate_from_platform_observation(db, bootstrap):
    experiment = Experiment(
        user_id=bootstrap.user.id,
        experiment_type=ExperimentType.VARIANCE,
        status=ExperimentStatus.COMPLETED,
        hypothesis="Does output vary?",
        repetitions=2,
        config_snapshot={"bounded": True},
        estimated_currency="USD",
        conclusion_type="tradeoff",
        conclusion_text="The results show a tradeoff.",
        concluded_at=datetime.now(timezone.utc),
    )
    db.add(experiment)
    db.commit()

    context = ProfessorContextAssembler(db).assemble(
        bootstrap.user.id,
        ProfessorContextRequest(
            intent=ProfessorIntent.UNDERSTAND_MY_EXPERIMENT,
            target=ProfessorTarget(type=ProfessorTargetType.EXPERIMENT, id=experiment.id),
        ),
    )
    experiment_record = next(record for record in context.records if record.ref_type == "experiment")
    conclusion_record = next(record for record in context.records if record.ref_type == "experiment_conclusion")
    assert experiment_record.provenance_kind == ProfessorProvenanceKind.PLATFORM_OBSERVATION
    assert conclusion_record.provenance_kind == ProfessorProvenanceKind.USER_AUTHORED_CONCLUSION
    assert conclusion_record.data["conclusion_text"] == "The results show a tradeoff."


def test_development_claim_types_and_conflicts_are_preserved(db, bootstrap):
    development = Development(
        title="A model change",
        development_type="model_release",
        candidate_key="professor-development",
        first_seen_at=datetime.now(timezone.utc),
    )
    db.add(development)
    db.flush()
    for text in ("The provider says this is faster.", "Independent evidence disagrees."):
        db.add(
            Claim(
                text=text,
                claim_type=ClaimType.PROVIDER_CLAIM if text.startswith("The provider") else ClaimType.RESEARCH_RESULT,
                development_id=development.id,
                as_of=datetime.now(timezone.utc),
                created_by=ClaimCreationMethod.USER,
                status=ClaimStatus.ACTIVE,
                conditions={"conflict_key": "speed"},
            )
        )
    db.flush()
    db.commit()

    context = ProfessorContextAssembler(db).assemble(
        bootstrap.user.id,
        ProfessorContextRequest(
            intent=ProfessorIntent.WHY_DOES_THIS_MATTER,
            target=ProfessorTarget(type=ProfessorTargetType.DEVELOPMENT, id=development.id),
        ),
    )
    claim_records = [record for record in context.records if record.ref_type == "claim"]
    assert {record.claim_type.value for record in claim_records} == {"PROVIDER_CLAIM", "RESEARCH_RESULT"}
    assert len({record.conflict_group for record in claim_records}) == 1
