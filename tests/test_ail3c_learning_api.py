"""AIL.3C: concept binding, human conclusion, qualification and Count Toward
Learning. Everything is built through production code (LabService,
ExperimentExecutionService, the Lab router); only MA6 EvaluationRun outcomes
are seeded, exactly as tests/test_ail3b_experiment_execution.py does."""

import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db  # noqa: F401  (client fixture wires the DB)
from app.auth import get_current_user
from app.db.enums import (
    ArtifactType,
    EvaluationFinding,
    EvaluationMethod,
    EvaluationRunStatus,
    EvidenceRefType,
    EvidenceType,
    ExperimentType,
    GradingMode,
)
from app.errors import NotFoundError
from app.main import app as fastapi_app
from app.models.artifacts_eval import Artifact
from app.models.concepts import Concept
from app.models.evaluation_definitions import EvaluationCriterion
from app.models.evaluation_runs import EvaluationCriterionResult, EvaluationRun
from app.models.lab import ExperimentTaskRun
from app.models.learner import LearningEvidence
from app.models.radar import Development
from app.models.tasks import AgentRun, TaskRun
from app.schemas.lab import ExperimentCreate, ExperimentModelSelection
from app.services.concept_graph_service import ConceptGraphService
from app.services.experiment_execution_service import ExperimentExecutionService
from app.services.experiment_learning_qualification_service import (
    ALREADY_COUNTED,
    CONCEPT_REBIND_REQUIRED,
    EVALUATION_FAILED,
    EVALUATION_INCOMPLETE,
    EVALUATION_NOT_CONFIGURED,
    EVALUATION_PENDING,
    EXPERIMENT_INCOMPLETE,
    MISSING_CONCEPT,
    NO_MEANINGFUL_EVALUATION,
    READY,
    ExperimentLearningQualificationService,
)
from app.services.lab_service import LabService
from app.services.learner_state_service import DEMONSTRATED, NOT_STARTED, PRACTICED, LearnerStateService
from tests.ail1a_factories import make_concept, make_published_version, make_user
from tests.conftest import (
    make_agent_run,
    make_evaluation_definition,
    make_evaluation_definition_version,
)
from tests.test_ail3a_personal_lab import _active_agent, _model_bundle, _snapshot
from tests.test_ail3b_experiment_execution import _complete_execution, _require_evaluation

MET = EvaluationFinding.MET
NOT_MET = EvaluationFinding.NOT_MET
PARTIAL = EvaluationFinding.PARTIAL
NOT_APPLICABLE = EvaluationFinding.NOT_APPLICABLE
COMPLETED = EvaluationRunStatus.COMPLETED


# -- Builders (production paths; only MA6 outcomes are seeded) ---------------


def _concept(db):
    version = make_published_version(db)
    return db.get(Concept, version.concept_id), version


def _create_experiment(db, bootstrap, *, concept_id=None, development_id=None):
    service = LabService(db)
    _, versions = service.starter_kits(bootstrap.user)
    agent = _active_agent(db, bootstrap.project.id, 41, "AIL3C Agent")
    model_a, provider_a, pm_a = _model_bundle(db, "lab/c-a", "C Provider A")
    model_b, provider_b, pm_b = _model_bundle(db, "lab/c-b", "C Provider B")
    snapshot_a = _snapshot(db, model_a, pm_a, provider_a)
    snapshot_b = _snapshot(db, model_b, pm_b, provider_b)
    db.commit()
    return service.create_experiment(
        bootstrap.user,
        ExperimentCreate(
            experiment_type=ExperimentType.MODEL_COMPARISON,
            hypothesis="Which model handles the kit better?",
            eval_set_version_id=versions[0].id,
            agent_version_ids=[agent.id],
            models=[
                ExperimentModelSelection(model_id=model_a.id, provider_model_snapshot_id=snapshot_a.id),
                ExperimentModelSelection(model_id=model_b.id, provider_model_snapshot_id=snapshot_b.id),
            ],
            repetitions=1,
            concept_id=concept_id,
            development_id=development_id,
        ),
    )


def _slots(db, experiment):
    return db.execute(
        select(ExperimentTaskRun)
        .where(ExperimentTaskRun.experiment_id == experiment.id)
        .order_by(ExperimentTaskRun.task_position, ExperimentTaskRun.label)
    ).scalars().all()


def _evaluate(db, experiment, definition, findings=MET, *, status=COMPLETED, only=None, with_results=True):
    """Seed one MA6 EvaluationRun (+ one criterion result) per slot Agent Run.
    ``findings`` is one finding for every slot or a per-slot list."""
    criterion = db.execute(
        select(EvaluationCriterion).where(EvaluationCriterion.evaluation_definition_version_id == definition.id)
    ).scalars().one()
    for index, slot in enumerate(_slots(db, experiment)):
        if only is not None and index not in only:
            continue
        agent_run = db.execute(select(AgentRun).where(AgentRun.task_run_id == slot.task_run_id)).scalar_one()
        artifact = Artifact(
            agent_run_id=agent_run.id, type=ArtifactType.REPORT, storage_ref=f"test://{agent_run.id}", content_hash="a" * 64
        )
        db.add(artifact)
        db.flush()
        run = EvaluationRun(
            subject_agent_run_id=agent_run.id,
            subject_artifact_id=artifact.id,
            subject_artifact_content_hash=artifact.content_hash,
            evaluation_definition_version_id=definition.id,
            method=EvaluationMethod.DETERMINISTIC,
            status=status,
        )
        db.add(run)
        db.flush()
        if with_results:
            finding = findings[index] if isinstance(findings, list) else findings
            db.add(EvaluationCriterionResult(
                evaluation_run_id=run.id,
                evaluation_criterion_id=criterion.id,
                criterion_key=criterion.key,
                order_index=0,
                finding=finding,
                rationale="seeded MA6 finding",
            ))
    db.commit()


def _finished(db, bootstrap, *, concept=True, findings=MET, evaluated=True, development_id=None, **evaluate_kwargs):
    """A launched, fully executed experiment created with a Concept and a
    required evaluation definition; evaluations seeded as requested."""
    concept_row, version = _concept(db) if concept else (None, None)
    experiment = _create_experiment(
        db, bootstrap, concept_id=concept_row.id if concept_row else None, development_id=development_id
    )
    definition = make_evaluation_definition_version(
        db, definition=make_evaluation_definition(db, project=bootstrap.project), version=1
    )
    _require_evaluation(db, experiment, definition.id)
    ExperimentExecutionService(db).launch(bootstrap.user.id, experiment.id)
    _complete_execution(db, experiment)
    if evaluated:
        _evaluate(db, experiment, definition, findings, **evaluate_kwargs)
    return experiment, definition, concept_row, version


def _assess(db, user_id, experiment):
    return ExperimentLearningQualificationService(db).assess(user_id, experiment.id)


def _evidence_rows(db, experiment_id):
    return db.execute(
        select(LearningEvidence).where(
            LearningEvidence.ref_type == EvidenceRefType.EXPERIMENT, LearningEvidence.ref_id == experiment_id
        )
    ).scalars().all()


@pytest.fixture()
def as_user():
    """Authenticate API calls as a different user, then restore."""
    def _override(user):
        fastapi_app.dependency_overrides[get_current_user] = lambda: user
    yield _override
    fastapi_app.dependency_overrides.pop(get_current_user, None)


# -- A-D: Concept binding and version freezing -------------------------------


def test_experiment_creation_freezes_the_current_concept_version(db, bootstrap):
    concept, version = _concept(db)
    experiment = _create_experiment(db, bootstrap, concept_id=concept.id)
    assert experiment.concept_id == concept.id
    assert experiment.concept_version_id == version.id  # A
    assert db.get(type(version), experiment.concept_version_id).concept_id == concept.id  # B


def test_creation_without_a_concept_leaves_no_version(db, bootstrap):
    experiment = _create_experiment(db, bootstrap)
    assert experiment.concept_id is None and experiment.concept_version_id is None


def test_later_concept_version_does_not_rewrite_the_frozen_version(db, bootstrap):
    experiment, _, concept, v1 = _finished(db, bootstrap)
    graph = ConceptGraphService(db)
    draft = graph.create_draft_version(
        concept_id=concept.id, plain_definition="A newer definition.", change_note="revised", change_severity="minor"
    )
    v2 = graph.publish_version(draft.id)
    assert graph.get_current_version(concept.id).id == v2.id != v1.id

    db.refresh(experiment)
    assert experiment.concept_version_id == v1.id  # C

    evidence, created, _ = ExperimentLearningQualificationService(db).count_toward_learning(
        bootstrap.user.id, experiment.id
    )
    assert created and evidence.concept_version_id == v1.id  # D: frozen, not "current at click time"


def test_bind_concept_api_freezes_version_and_rejects_unknown_concept(client, auth_headers, db, bootstrap):
    concept, version = _concept(db)
    experiment = _create_experiment(db, bootstrap)

    ok = client.put(f"/lab/experiments/{experiment.id}/concept", json={"concept_id": concept.id}, headers=auth_headers)
    assert ok.status_code == 200
    assert ok.json()["concept_id"] == concept.id and ok.json()["concept_version_id"] == version.id

    missing = client.put(f"/lab/experiments/{experiment.id}/concept", json={"concept_id": "nope"}, headers=auth_headers)
    assert missing.status_code == 404
    assert db.execute(select(func.count()).select_from(Concept)).scalar() == 1  # no silent creation

    forged = client.put(
        f"/lab/experiments/{experiment.id}/concept",
        json={"concept_id": concept.id, "user_id": "someone-else"},
        headers=auth_headers,
    )
    assert forged.status_code == 422


def test_concept_cannot_be_reassigned_after_evidence_exists(client, auth_headers, db, bootstrap):
    experiment, _, concept, version = _finished(db, bootstrap)
    other = make_published_version(db, make_concept(db, slug="other", name="Other Concept"))
    ExperimentLearningQualificationService(db).count_toward_learning(bootstrap.user.id, experiment.id)

    response = client.put(
        f"/lab/experiments/{experiment.id}/concept", json={"concept_id": other.concept_id}, headers=auth_headers
    )
    assert response.status_code == 409
    db.refresh(experiment)
    assert (experiment.concept_id, experiment.concept_version_id) == (concept.id, version.id)


def test_legacy_experiment_without_frozen_version_requires_explicit_rebind(client, auth_headers, db, bootstrap):
    experiment, _, concept, version = _finished(db, bootstrap)
    experiment.concept_version_id = None  # a pre-AIL.3C row: concept_id but no provenance
    db.commit()

    assert _assess(db, bootstrap.user.id, experiment)["status"] == CONCEPT_REBIND_REQUIRED
    evidence, created, _ = ExperimentLearningQualificationService(db).count_toward_learning(
        bootstrap.user.id, experiment.id
    )
    assert evidence is None and not created and not _evidence_rows(db, experiment.id)

    bound = client.put(f"/lab/experiments/{experiment.id}/concept", json={"concept_id": concept.id}, headers=auth_headers)
    assert bound.json()["concept_version_id"] == version.id
    db.expire_all()  # the API committed in its own session
    assert _assess(db, bootstrap.user.id, experiment)["status"] == READY


# -- E-H: Qualification against canonical MA6 evidence -----------------------


def test_fully_evaluated_experiment_is_ready_despite_bookkeeping_task_runs(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    tagged = db.execute(select(func.count()).select_from(TaskRun).where(TaskRun.experiment_id == experiment.id)).scalar()
    assert tagged > len(_slots(db, experiment))  # bookkeeping runs exist and must not be required
    assessment = _assess(db, bootstrap.user.id, experiment)
    assert assessment["status"] == READY and assessment["ready"] is True


def test_unrelated_completed_evaluation_cannot_qualify(db, bootstrap):  # E
    experiment, definition, *_ = _finished(db, bootstrap, evaluated=False)
    stranger = make_agent_run(db)
    artifact = Artifact(agent_run_id=stranger.id, type=ArtifactType.REPORT, storage_ref="test://x", content_hash="b" * 64)
    db.add(artifact)
    db.flush()
    run = EvaluationRun(
        subject_agent_run_id=stranger.id,
        subject_artifact_id=artifact.id,
        subject_artifact_content_hash=artifact.content_hash,
        evaluation_definition_version_id=definition.id,
        method=EvaluationMethod.DETERMINISTIC,
        status=COMPLETED,
    )
    db.add(run)
    db.flush()
    criterion = db.execute(select(EvaluationCriterion).where(
        EvaluationCriterion.evaluation_definition_version_id == definition.id)).scalars().one()
    db.add(EvaluationCriterionResult(
        evaluation_run_id=run.id, evaluation_criterion_id=criterion.id, criterion_key=criterion.key,
        order_index=0, finding=MET, rationale="not mine"))
    db.commit()

    assessment = _assess(db, bootstrap.user.id, experiment)
    assert assessment["ready"] is False and assessment["status"] == EVALUATION_PENDING


def test_incomplete_required_evaluations_cannot_qualify(db, bootstrap):  # F
    experiment, *_ = _finished(db, bootstrap, only={0, 1, 2})
    assert _assess(db, bootstrap.user.id, experiment)["ready"] is False


def test_running_evaluation_is_pending_not_ready(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap, status=EvaluationRunStatus.RUNNING, with_results=False)
    assert _assess(db, bootstrap.user.id, experiment)["status"] == EVALUATION_PENDING


def test_failed_evaluation_is_not_ready(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap, status=EvaluationRunStatus.FAILED, with_results=False)
    assert _assess(db, bootstrap.user.id, experiment)["status"] == EVALUATION_FAILED


def test_completed_evaluation_without_criterion_results_is_incomplete(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap, with_results=False)
    assert _assess(db, bootstrap.user.id, experiment)["status"] == EVALUATION_INCOMPLETE


def test_evaluation_on_a_different_definition_version_does_not_count(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap, evaluated=False)
    other = make_evaluation_definition_version(db, definition=make_evaluation_definition(
        db, project=bootstrap.project, name="Other rubric"), version=1)
    _evaluate(db, experiment, other, MET)
    assert _assess(db, bootstrap.user.id, experiment)["ready"] is False


# Findings describe candidate performance; they never decide whether the
# learner completed a legitimate hands-on activity. Slots alternate model-1 /
# model-2, so [x, y] * 6 gives each candidate its own finding on every task.
@pytest.mark.parametrize(
    "findings",
    [
        pytest.param(MET, id="both-candidates-perform-well"),
        pytest.param([MET, NOT_MET] * 6, id="one-candidate-not-met"),
        pytest.param([PARTIAL, MET] * 6, id="one-candidate-partial"),
        pytest.param([NOT_MET, PARTIAL] * 6, id="both-candidates-perform-poorly"),
        pytest.param(NOT_MET, id="all-not-met"),
    ],
)
def test_completed_fully_evaluated_experiment_qualifies_whatever_the_candidates_scored(
    client, auth_headers, db, bootstrap, findings
):
    experiment, _, concept, version = _finished(db, bootstrap, findings=findings)
    assert _assess(db, bootstrap.user.id, experiment)["status"] == READY

    def ma6():
        return db.execute(text("SELECT id, finding, rationale FROM evaluation_criterion_results ORDER BY id")).fetchall()

    before = ma6()
    response = client.post(f"/lab/experiments/{experiment.id}/count-toward-learning", headers=auth_headers)
    assert response.status_code == 200 and response.json()["created"] is True
    row = _evidence_rows(db, experiment.id)[0]
    assert (row.evidence_type, row.concept_version_id, row.score) == (EvidenceType.LAB, version.id, None)
    assert ma6() == before  # canonical MA6 findings stay exactly as evaluated
    assert LearnerStateService(db).state(bootstrap.user.id, concept.id).ladder == PRACTICED


def test_all_not_applicable_is_not_meaningful_evidence(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap, findings=NOT_APPLICABLE)
    assert _assess(db, bootstrap.user.id, experiment)["status"] == NO_MEANINGFUL_EVALUATION
    _, created, _ = ExperimentLearningQualificationService(db).count_toward_learning(bootstrap.user.id, experiment.id)
    assert not created and not _evidence_rows(db, experiment.id)


def test_one_execution_with_no_meaningful_findings_blocks_qualification(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap, findings=[MET] * 11 + [NOT_APPLICABLE])
    assert _assess(db, bootstrap.user.id, experiment)["status"] == NO_MEANINGFUL_EVALUATION


def test_experiment_status_completed_alone_is_not_a_pass(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap, evaluated=False)
    experiment.config_snapshot = {k: v for k, v in experiment.config_snapshot.items() if k != "evaluation_definition_version_id"}
    db.commit()
    assert ExperimentExecutionService(db).read(bootstrap.user.id, experiment.id)["experiment"]["overall_status"] == "COMPLETED"
    assert _assess(db, bootstrap.user.id, experiment)["status"] == EVALUATION_NOT_CONFIGURED


def test_missing_concept_is_reported(db, bootstrap):
    no_concept, *_ = _finished(db, bootstrap, concept=False)
    assert _assess(db, bootstrap.user.id, no_concept)["status"] == MISSING_CONCEPT


def test_version_from_a_different_concept_is_invalid_provenance(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    foreign_version = make_published_version(db, make_concept(db, slug="foreign", name="Foreign"))
    experiment.concept_version_id = foreign_version.id
    db.commit()
    assert _assess(db, bootstrap.user.id, experiment)["status"] == CONCEPT_REBIND_REQUIRED


def test_unlaunched_experiment_is_incomplete(db, bootstrap):
    concept, _ = _concept(db)
    experiment = _create_experiment(db, bootstrap, concept_id=concept.id)
    assert _assess(db, bootstrap.user.id, experiment)["status"] == EXPERIMENT_INCOMPLETE


# -- I-L: Count Toward Learning ----------------------------------------------


def test_qualification_and_conclusion_never_create_evidence(client, auth_headers, db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    for _ in range(3):
        assert client.get(f"/lab/experiments/{experiment.id}/learning-qualification", headers=auth_headers).json()["status"] == READY
    client.put(f"/lab/experiments/{experiment.id}/conclusion", json={"conclusion_type": "tradeoff"}, headers=auth_headers)
    assert not _evidence_rows(db, experiment.id)


def test_explicit_count_creates_lab_evidence_with_frozen_provenance(client, auth_headers, db, bootstrap):
    experiment, _, concept, version = _finished(db, bootstrap)
    assert LearnerStateService(db).state(bootstrap.user.id, concept.id).ladder == NOT_STARTED

    response = client.post(f"/lab/experiments/{experiment.id}/count-toward-learning", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True and body["qualification"]["status"] == ALREADY_COUNTED

    rows = _evidence_rows(db, experiment.id)
    assert len(rows) == 1
    row = rows[0]
    assert (row.evidence_type, row.grader, row.passed, row.score) == (EvidenceType.LAB, GradingMode.DETERMINISTIC, True, None)
    assert (row.ref_type, row.ref_id) == (EvidenceRefType.EXPERIMENT, experiment.id)
    assert (row.user_id, row.concept_id, row.concept_version_id) == (bootstrap.user.id, concept.id, version.id)
    assert body["evidence"]["id"] == row.id


def test_count_retry_returns_the_same_evidence(client, auth_headers, db, bootstrap):  # I
    experiment, *_ = _finished(db, bootstrap)
    first = client.post(f"/lab/experiments/{experiment.id}/count-toward-learning", headers=auth_headers).json()
    second = client.post(f"/lab/experiments/{experiment.id}/count-toward-learning", headers=auth_headers).json()
    assert (first["created"], second["created"]) == (True, False)
    assert first["evidence"]["id"] == second["evidence"]["id"]
    assert len(_evidence_rows(db, experiment.id)) == 1


def test_learner_state_is_derived_and_never_demonstrated_by_one_lab(client, auth_headers, db, bootstrap):  # L
    experiment, _, concept, _ = _finished(db, bootstrap)
    body = client.post(f"/lab/experiments/{experiment.id}/count-toward-learning", headers=auth_headers).json()
    state = LearnerStateService(db).state(bootstrap.user.id, concept.id)
    assert state.ladder == PRACTICED != DEMONSTRATED  # existing ladder rule for passed LAB evidence
    assert body["learner_state"]["ladder"] == state.ladder
    assert [e.evidence_type for e in state.evidence] == [EvidenceType.LAB]


def test_count_uses_authenticated_user_not_request_body(client, auth_headers, db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    response = client.post(
        f"/lab/experiments/{experiment.id}/count-toward-learning", json={"user_id": "forged"}, headers=auth_headers
    )
    assert response.status_code == 200
    assert _evidence_rows(db, experiment.id)[0].user_id == bootstrap.user.id


def test_cross_user_access_is_rejected_everywhere(client, db, bootstrap, as_user):  # K
    experiment, *_ = _finished(db, bootstrap)
    intruder = make_user(db, email="intruder@example.com")
    db.commit()
    service = ExperimentLearningQualificationService(db)
    with pytest.raises(NotFoundError):
        service.assess(intruder.id, experiment.id)
    with pytest.raises(NotFoundError):
        service.count_toward_learning(intruder.id, experiment.id)

    as_user(intruder)
    concept = db.get(Concept, experiment.concept_id)
    for method, path, body in (
        ("get", "learning-qualification", None),
        ("post", "count-toward-learning", None),
        ("put", "conclusion", {"conclusion_type": "inconclusive"}),
        ("put", "concept", {"concept_id": concept.id}),
    ):
        response = getattr(client, method)(f"/lab/experiments/{experiment.id}/{path}", **({"json": body} if body else {}))
        assert response.status_code == 404, (method, path)
    assert not _evidence_rows(db, experiment.id)


# -- J: exactly-once under contention ----------------------------------------


def test_concurrent_count_attempts_create_exactly_one_evidence_row(db, session_factory, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def attempt():
        session = session_factory()
        try:
            service = ExperimentLearningQualificationService(session)
            barrier.wait(timeout=10)
            evidence, created, _ = service.count_toward_learning(bootstrap.user.id, experiment.id)
            results.append((evidence.id, created))
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - re-surfaced by the assertions below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2 and results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    db.expire_all()
    assert len(_evidence_rows(db, experiment.id)) == 1


def test_lost_race_reads_the_canonical_row_instead_of_failing(db, session_factory, bootstrap):
    """Force the IntegrityError path deterministically: a stale pre-check says
    'nothing yet' while another session has already inserted the evidence."""
    experiment, *_ = _finished(db, bootstrap)
    winner, created, _ = ExperimentLearningQualificationService(db).count_toward_learning(bootstrap.user.id, experiment.id)
    assert created

    session = session_factory()
    try:
        service = ExperimentLearningQualificationService(session)
        real = service._find_evidence
        calls = {"n": 0}

        def stale_first(user_id, experiment_id):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(user_id, experiment_id)

        service._find_evidence = stale_first
        evidence, created_again, assessment = service.count_toward_learning(bootstrap.user.id, experiment.id)
    finally:
        session.close()

    assert calls["n"] >= 2  # the insert really was attempted and rejected
    assert evidence.id == winner.id and created_again is False
    assert assessment["status"] == ALREADY_COUNTED
    db.expire_all()
    assert len(_evidence_rows(db, experiment.id)) == 1


def test_database_rejects_a_duplicate_experiment_evidence_row(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    ExperimentLearningQualificationService(db).count_toward_learning(bootstrap.user.id, experiment.id)
    from app.services.learning_evidence_service import LearningEvidenceService

    with pytest.raises(IntegrityError):
        LearningEvidenceService(db).record_evidence(
            user_id=bootstrap.user.id,
            concept_id=experiment.concept_id,
            concept_version_id=experiment.concept_version_id,
            evidence_type=EvidenceType.LAB,
            grader=GradingMode.DETERMINISTIC,
            passed=True,
            ref_type=EvidenceRefType.EXPERIMENT,
            ref_id=experiment.id,
        )
    db.rollback()
    assert len(_evidence_rows(db, experiment.id)) == 1


# -- Human conclusion ---------------------------------------------------------


def test_owner_saves_and_edits_a_conclusion(client, auth_headers, db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    url = f"/lab/experiments/{experiment.id}/conclusion"

    first = client.put(url, json={"conclusion_type": "tradeoff", "conclusion_text": "  A is cheaper, B is tidier.  "}, headers=auth_headers)
    assert first.status_code == 200
    saved = first.json()["conclusion"]
    assert saved["type"] == "tradeoff" and saved["text"] == "A is cheaper, B is tidier." and saved["concluded_at"]

    second = client.put(url, json={"conclusion_type": "more_testing_needed", "conclusion_text": "Try a longer kit."}, headers=auth_headers)
    edited = second.json()["conclusion"]
    assert edited["type"] == "more_testing_needed" and edited["text"] == "Try a longer kit."
    assert edited["concluded_at"] >= saved["concluded_at"]

    results = client.get(f"/lab/experiments/{experiment.id}/results", headers=auth_headers).json()
    assert results["experiment"]["conclusion"]["type"] == "more_testing_needed"


@pytest.mark.parametrize("kind", ["inconclusive", "no_meaningful_difference"])
def test_neutral_conclusions_need_no_winner_or_text(client, auth_headers, db, bootstrap, kind):
    experiment, *_ = _finished(db, bootstrap)
    response = client.put(f"/lab/experiments/{experiment.id}/conclusion", json={"conclusion_type": kind}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["conclusion"] == {**response.json()["conclusion"], "type": kind, "text": None}
    assert "winner" not in str(response.json()).lower()


def test_invalid_conclusions_are_rejected(client, auth_headers, db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    url = f"/lab/experiments/{experiment.id}/conclusion"
    assert client.put(url, json={"conclusion_type": "model_a_wins"}, headers=auth_headers).status_code == 422
    assert client.put(url, json={"conclusion_type": "custom", "conclusion_text": "x" * 4001}, headers=auth_headers).status_code == 422
    assert client.put(url, json={"conclusion_type": "custom", "conclusion_text": "   "}, headers=auth_headers).status_code == 409
    assert client.put(url, json={"conclusion_type": "tradeoff", "user_id": "forged"}, headers=auth_headers).status_code == 422
    db.refresh(experiment)
    assert experiment.conclusion_type is None and experiment.concluded_at is None


def test_conclusion_is_rejected_before_results_exist(client, auth_headers, db, bootstrap):
    draft = _create_experiment(db, bootstrap)
    url = f"/lab/experiments/{draft.id}/conclusion"
    assert client.put(url, json={"conclusion_type": "inconclusive"}, headers=auth_headers).status_code == 409

    ExperimentExecutionService(db).launch(bootstrap.user.id, draft.id)  # RUNNING, nothing finished
    assert client.put(url, json={"conclusion_type": "inconclusive"}, headers=auth_headers).status_code == 409


def test_conclusion_changes_no_evidence_and_no_ma6_rows(client, auth_headers, db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)

    def ma6():
        return (
            db.execute(text("SELECT id, status FROM evaluation_runs ORDER BY id")).fetchall(),
            db.execute(text("SELECT id, finding, rationale FROM evaluation_criterion_results ORDER BY id")).fetchall(),
        )

    before = ma6()
    for kind in ("tradeoff", "inconclusive", "custom"):
        client.put(f"/lab/experiments/{experiment.id}/conclusion",
                   json={"conclusion_type": kind, "conclusion_text": "my reading"}, headers=auth_headers)
    assert ma6() == before
    assert not _evidence_rows(db, experiment.id)


# -- Radar and Learning Plan stay untouched ----------------------------------

_RADAR_AND_PLAN = (
    "radar_sources", "radar_items", "developments", "development_models", "claims", "claim_origins",
    "claim_citations", "development_concepts", "development_terms", "attention_samples", "triage_decisions",
    "learning_plan_items",
)


def _snapshot_tables(db):
    return {t: sorted(map(repr, db.execute(text(f"SELECT * FROM {t}")).fetchall())) for t in _RADAR_AND_PLAN}


def test_conclusion_and_count_do_not_mutate_radar_or_learning_plan(client, auth_headers, db, bootstrap):
    development = Development(title="Origin", development_type="capability_change", candidate_key="origin-dev")
    db.add(development)
    db.commit()
    experiment, *_ = _finished(db, bootstrap, development_id=development.id)
    before = _snapshot_tables(db)

    client.put(f"/lab/experiments/{experiment.id}/conclusion", json={"conclusion_type": "tradeoff"}, headers=auth_headers)
    assert client.post(f"/lab/experiments/{experiment.id}/count-toward-learning", headers=auth_headers).status_code == 200

    assert _snapshot_tables(db) == before
    assert client.get(f"/lab/experiments/{experiment.id}", headers=auth_headers).json()["development_id"] == development.id
