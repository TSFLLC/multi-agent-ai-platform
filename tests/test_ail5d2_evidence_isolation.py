"""AIL.5D.2 evidence-isolation guard: practice can never be a demonstration.

Practice (guided or independent) is marked on the evidence row (``score_json.practice is true``). The DEMONSTRATED
floor refuses such rows whatever wrote them, the canonical write path refuses the marker on demonstration-only evidence,
and the two existing Lab evidence writers carry the marker through. Nothing about legitimate evidence changes.
"""

from types import SimpleNamespace

import pytest

from app.db.enums import AssistanceLevel, EvidenceRefType, EvidenceType, ExecutionVerification, GradingMode
from app.models.learner import LearningEvidence
from app.services.independence_policy import (
    DEMONSTRATION_ONLY_EVIDENCE_TYPES, PRACTICE_MARKER, counts_toward_demonstrated, counts_toward_practiced, demonstration_floor,
    is_practice, with_practice_marker,
)
from app.services.learner_state_service import LearnerStateService
from app.services.learning_evidence_service import LearningEvidenceService
from tests.ail1a_factories import make_concept, make_published_version
from tests.test_ail3c_learning_api import _evidence_rows, _finished
from app.services.experiment_learning_qualification_service import ExperimentLearningQualificationService

LAB_REQUIREMENT = {"requires_all": [
    {"evidence_type": "knowledge_check", "grader_in": ["deterministic"]},
    {"evidence_type": "lab", "grader_in": ["deterministic"]},
]}


def _row(**over):
    base = dict(
        superseded_by_id=None, on_demo_data=False, grader=GradingMode.DETERMINISTIC, execution_verification=None,
        assistance_level=None, evidence_type=EvidenceType.LAB, milestone_attempt_id=None, score=None, passed=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


# -- the policy floor ----------------------------------------------------------------------------------------------------------


def test_a_practice_marked_row_fails_the_demonstration_floor_and_nothing_else_about_the_floor_changed():
    assert demonstration_floor(_row()) == (True, "ok")
    assert demonstration_floor(_row(score={"fixture": "x"})) == (True, "ok")                # other score payloads are unaffected
    assert demonstration_floor(_row(score={PRACTICE_MARKER: False})) == (True, "ok")         # only an explicit true marks practice
    assert demonstration_floor(_row(score={PRACTICE_MARKER: "yes"})) == (True, "ok")
    assert demonstration_floor(_row(score={PRACTICE_MARKER: True})) == (False, "practice")
    assert counts_toward_demonstrated(_row(score={PRACTICE_MARKER: True})) is False
    # the pre-existing floor rules still apply in their own right
    assert demonstration_floor(_row(on_demo_data=True)) == (False, "demo_data")
    assert demonstration_floor(_row(assistance_level=AssistanceLevel.H3))[0] is False
    assert demonstration_floor(_row(grader=GradingMode.SELF)) == (False, "self_reported")


def test_practice_still_counts_toward_practiced_but_never_more():
    assert counts_toward_practiced(_row(score={PRACTICE_MARKER: True})) is True
    assert counts_toward_practiced(_row(assistance_level=AssistanceLevel.H5, score={PRACTICE_MARKER: True})) is False  # H5 is never practice


def test_the_marker_helpers():
    assert with_practice_marker({"a": 1}, True) == {"a": 1, PRACTICE_MARKER: True}
    assert with_practice_marker(None, True) == {PRACTICE_MARKER: True}
    assert with_practice_marker({"a": 1}, False) == {"a": 1} and with_practice_marker(None, False) is None
    assert is_practice(_row(score={PRACTICE_MARKER: True})) and not is_practice(_row(score=None))


# -- the canonical write path --------------------------------------------------------------------------------------------------


def _concept(db, slug, requirements=LAB_REQUIREMENT):
    concept = make_concept(db, slug=slug, name=slug)
    version = make_published_version(db, concept=concept, evidence_requirements=requirements)
    db.commit()
    return concept, version


def _write(db, user, concept, version, evidence_type, *, score=None, grader=GradingMode.DETERMINISTIC, **kw):
    return LearningEvidenceService(db).record_evidence(
        user_id=user.id, concept_id=concept.id, concept_version_id=version.id, evidence_type=evidence_type, grader=grader,
        passed=True, score=score, **kw,
    )


@pytest.mark.parametrize("evidence_type", sorted(DEMONSTRATION_ONLY_EVIDENCE_TYPES, key=lambda t: t.value))
def test_the_write_path_refuses_the_practice_marker_on_demonstration_only_evidence(db, bootstrap, evidence_type):
    concept, version = _concept(db, f"iso-{evidence_type.value}")
    with pytest.raises(ValueError, match="Practice work can never be recorded as demonstration evidence"):
        _write(db, bootstrap.user, concept, version, evidence_type, score={PRACTICE_MARKER: True})
    assert db.query(LearningEvidence).count() == 0  # nothing was written
    # the same type without the marker is exactly as before
    _write(db, bootstrap.user, concept, version, evidence_type, score={"raw": 1, "max": 1},
           execution_verification=ExecutionVerification.PLATFORM_VERIFIED)
    assert db.query(LearningEvidence).count() == 1


def test_the_write_path_accepts_practice_marked_lab_and_observation_evidence(db, bootstrap):
    concept, version = _concept(db, "iso-allowed")
    for evidence_type in (EvidenceType.LAB, EvidenceType.OBSERVATION, EvidenceType.INTERPRETATION):
        _write(db, bootstrap.user, concept, version, evidence_type, score=with_practice_marker(None, True))
    assert db.query(LearningEvidence).count() == 3


# -- the learner ladder -----------------------------------------------------------------------------------------------------------


def test_a_legitimate_lab_still_demonstrates_but_the_same_lab_marked_practice_does_not(db, bootstrap):
    user = bootstrap.user
    # legitimate, unmarked: existing behaviour, unchanged -> DEMONSTRATED
    real, real_v = _concept(db, "iso-real")
    _write(db, user, real, real_v, EvidenceType.KNOWLEDGE_CHECK)
    _write(db, user, real, real_v, EvidenceType.LAB, ref_type=EvidenceRefType.EXPERIMENT, ref_id="exp-real")
    assert LearnerStateService(db).state(user.id, real.id).ladder == "demonstrated"
    # the identical evidence, marked as practice -> at most PRACTICED
    practice, practice_v = _concept(db, "iso-practice")
    _write(db, user, practice, practice_v, EvidenceType.KNOWLEDGE_CHECK)
    _write(db, user, practice, practice_v, EvidenceType.LAB, score=with_practice_marker(None, True), ref_type=EvidenceRefType.EXPERIMENT, ref_id="exp-practice")
    assert LearnerStateService(db).state(user.id, practice.id).ladder == "practiced"


def test_many_practice_attempts_never_add_up_to_a_demonstration(db, bootstrap):
    user = bootstrap.user
    concept, version = _concept(db, "iso-many")
    _write(db, user, concept, version, EvidenceType.KNOWLEDGE_CHECK)
    for i in range(6):
        _write(db, user, concept, version, EvidenceType.LAB, score=with_practice_marker({"attempt": i}, True), ref_type=EvidenceRefType.EXPERIMENT, ref_id=f"exp-{i}")
    assert LearnerStateService(db).state(user.id, concept.id).ladder == "practiced"


def test_a_real_demonstration_leg_alongside_practice_still_demonstrates(db, bootstrap):
    user = bootstrap.user
    concept, version = _concept(db, "iso-mixed")
    _write(db, user, concept, version, EvidenceType.KNOWLEDGE_CHECK)
    _write(db, user, concept, version, EvidenceType.LAB, score=with_practice_marker(None, True), ref_type=EvidenceRefType.EXPERIMENT, ref_id="exp-p")
    assert LearnerStateService(db).state(user.id, concept.id).ladder == "practiced"
    _write(db, user, concept, version, EvidenceType.LAB, ref_type=EvidenceRefType.EXPERIMENT, ref_id="exp-real")
    assert LearnerStateService(db).state(user.id, concept.id).ladder == "demonstrated"


# -- the existing Lab writers carry the marker through -------------------------------------------------------------------------------


def _count(db, bootstrap, experiment):
    evidence, created, _ = ExperimentLearningQualificationService(db).count_toward_learning(bootstrap.user.id, experiment.id)
    assert created and evidence is not None
    return _evidence_rows(db, experiment.id)[0]


def test_counting_an_ordinary_experiment_is_unchanged(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    row = _count(db, bootstrap, experiment)
    assert row.score is None and demonstration_floor(row) == (True, "ok")


def test_counting_a_practice_experiment_marks_its_evidence_so_it_can_never_demonstrate(db, bootstrap):
    experiment, *_ = _finished(db, bootstrap)
    experiment.config_snapshot = {**experiment.config_snapshot, "practice": True}
    db.commit()
    row = _count(db, bootstrap, experiment)
    assert row.score == {PRACTICE_MARKER: True} and demonstration_floor(row) == (False, "practice")


# -- the curriculum-fixture lab writer -------------------------------------------------------------------------------------------------


def _fixture_run(db, bootstrap, tmp_path, *, practice):
    from app.db.enums import AgentRunStatus, TaskRunStatus
    from app.models.tasks import Task
    from tests.ail5d1_factories import make_structured_item
    from tests.conftest import make_agent_run, make_artifact_with_content, make_task_run

    item = make_structured_item(db, slug=f"fixture-{'p' if practice else 'n'}")
    requirements = {"academy_fixture": "level1-day15-bounded-grounding", "learning_item_id": item.id}
    if practice:
        requirements["practice"] = True
    task = Task(project_id=bootstrap.project.id, title="Fixture", execution_mode=bootstrap_mode(), requirements=requirements, created_by=bootstrap.user.id)
    db.add(task)
    db.flush()
    task_run = make_task_run(db, task, status=TaskRunStatus.COMPLETED)
    run = make_agent_run(db, task_run, status=AgentRunStatus.COMPLETED)
    make_artifact_with_content(db, tmp_path, run, content="The library opens at 9:00 AM.")
    db.commit()
    return run


def bootstrap_mode():
    from app.db.enums import ExecutionMode

    return ExecutionMode.SINGLE_AGENT


@pytest.mark.parametrize("practice", [False, True])
def test_the_curriculum_fixture_writer_marks_practice_and_leaves_ordinary_lab_evidence_unchanged(db, bootstrap, tmp_path, practice):
    from app.services.academy_level1_fixtures import qualify_agent_fixture

    run = _fixture_run(db, bootstrap, tmp_path, practice=practice)
    row = qualify_agent_fixture(db, bootstrap.user, run.id, "h1", conclusion="The answer was supported by the source.")
    assert (row.score or {}).get(PRACTICE_MARKER) is (True if practice else None)
    assert demonstration_floor(row)[0] is (not practice)
    assert row.score["fixture"] == "level1-day15-bounded-grounding"  # the existing score payload is preserved
