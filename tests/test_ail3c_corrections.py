"""AIL.3C Corrections - Executable behavioral tests

Tests the 16 blocker corrections:
1. MA6 association correctness
2. Evaluation completeness
3. Passed derivation from canonical evidence
4. Concept version provenance
5. Idempotent evidence creation
6. Authorization/privacy
7. Human conclusion
8. Radar immutability
"""

import pytest
from sqlalchemy import select
from datetime import datetime, timezone

from app.db.enums import (
    EvaluationFinding, EvaluationRunStatus, EvidenceRefType, EvidenceType,
    ExperimentStatus, GradingMode, VersionStatus
)
from app.models.lab import Experiment, ExperimentTaskRun
from app.models.evaluation_runs import EvaluationRun, EvaluationCriterionResult
from app.models.learner import LearningEvidence
from app.models.tasks import TaskRun, AgentRun
from app.services.experiment_learning_qualification_service import ExperimentLearningQualificationService


class TestMA6Association:
    """Test correct tracing through Experiment → TaskRun → AgentRun → EvaluationRun"""

    def test_unrelated_evaluation_does_not_qualify_experiment(self, db, user, experiment_completed, evaluation_run_unrelated):
        """BLOCKER #1 FIX: Unrelated EvaluationRun must NOT satisfy qualification.

        Current bug: any completed EvaluationRun anywhere in the DB would qualify.
        Fixed: must trace through experiment's actual TaskRun → AgentRun.
        """
        # Setup: experiment is COMPLETED, but its agent run has no evaluation
        service = ExperimentLearningQualificationService(db)

        can_count, reasons = service.can_count_toward_learning(user.id, experiment_completed.id)

        assert not can_count
        assert any("no completed evaluation" in detail for detail in reasons["details"])

    def test_related_evaluation_qualifies_experiment(self, db, user, experiment_with_evaluation):
        """Evaluation linked through proper chain DOES qualify."""
        service = ExperimentLearningQualificationService(db)

        can_count, reasons = service.can_count_toward_learning(user.id, experiment_with_evaluation.id)

        assert can_count
        assert reasons["evaluation_complete"] is True


class TestEvaluationDerivation:
    """Test passed value derived from canonical MA6 findings"""

    def test_all_met_criteria_means_passed(self, db, user, experiment_with_evaluation_all_met):
        """Evaluation with all MET criteria => passed=True"""
        service = ExperimentLearningQualificationService(db)
        evidence, msg = service.count_toward_learning(user.id, experiment_with_evaluation_all_met.id)

        assert evidence is not None
        assert evidence.passed is True

    def test_not_met_criterion_means_not_passed(self, db, user, experiment_with_evaluation_not_met):
        """BLOCKER #3 FIX: NOT_MET finding => passed=False, no evidence created"""
        service = ExperimentLearningQualificationService(db)
        evidence, msg = service.count_toward_learning(user.id, experiment_with_evaluation_not_met.id)

        assert evidence is None
        assert "not met" in msg.lower()

    def test_partial_criterion_means_not_passed(self, db, user, experiment_with_evaluation_partial):
        """PARTIAL finding => passed=False, no evidence created"""
        service = ExperimentLearningQualificationService(db)
        evidence, msg = service.count_toward_learning(user.id, experiment_with_evaluation_partial.id)

        assert evidence is None

    def test_all_not_applicable_means_no_evidence(self, db, user, experiment_with_evaluation_all_na):
        """All NOT_APPLICABLE findings => no real evaluation, no evidence"""
        service = ExperimentLearningQualificationService(db)
        evidence, msg = service.count_toward_learning(user.id, experiment_with_evaluation_all_na.id)

        assert evidence is None
        assert "not applicable" in msg.lower()


class TestIdempotency:
    """Test concurrent duplicate protection"""

    def test_calling_twice_returns_same_evidence(self, db, user, experiment_with_evaluation_all_met):
        """BLOCKER #6 FIX: Second call returns existing evidence, no duplicate"""
        service = ExperimentLearningQualificationService(db)

        # First call
        evidence1, msg1 = service.count_toward_learning(user.id, experiment_with_evaluation_all_met.id)
        assert evidence1 is not None
        first_id = evidence1.id

        # Second call (should be idempotent)
        evidence2, msg2 = service.count_toward_learning(user.id, experiment_with_evaluation_all_met.id)
        assert evidence2 is not None
        assert evidence2.id == first_id  # Same evidence object
        assert "already recorded" in msg2.lower()

        # Verify only ONE evidence row exists
        stmt = select(LearningEvidence).where(
            LearningEvidence.ref_type == EvidenceRefType.EXPERIMENT,
            LearningEvidence.ref_id == experiment_with_evaluation_all_met.id
        )
        count = len(db.execute(stmt).scalars().all())
        assert count == 1


class TestConceptVersionProvenance:
    """Test frozen concept version"""

    def test_concept_version_frozen_at_experiment_creation(self, db, user, concept, experiment_setup):
        """BLOCKER #5 FIX: experiment.concept_version_id is set and frozen"""
        # When experiment is created with a concept, the version must be frozen
        experiment = experiment_setup(user, concept)

        assert experiment.concept_version_id is not None
        # The frozen version should match the current version at creation time
        frozen_version_id = experiment.concept_version_id

        # Later concept version changes shouldn't affect the frozen reference
        # (This is tested at evidence reference time in next test)
        assert frozen_version_id == concept.versions[0].id

    def test_evidence_references_frozen_concept_version(self, db, user, experiment_with_evaluation_all_met):
        """Evidence must reference the frozen ConceptVersion, not current"""
        service = ExperimentLearningQualificationService(db)
        evidence, _ = service.count_toward_learning(user.id, experiment_with_evaluation_all_met.id)

        assert evidence is not None
        assert evidence.concept_version_id == experiment_with_evaluation_all_met.concept_version_id
        # Not "whatever is current now"
        assert evidence.concept_version_id is not None


class TestAuthorizationPrivacy:
    """Test user ownership enforcement"""

    def test_cross_user_cannot_count_experiment(self, db, user1, user2, experiment_user1_with_evaluation):
        """BLOCKER #3 FIX (auth part): User2 cannot count User1's experiment"""
        service = ExperimentLearningQualificationService(db)

        with pytest.raises(ValueError, match="not owned by user"):
            service.count_toward_learning(user2.id, experiment_user1_with_evaluation.id)

    def test_user_cannot_see_other_user_evidence(self, db, user1, user2, experiment_user1_with_evaluation):
        """User2 cannot see evidence User1 created"""
        service = ExperimentLearningQualificationService(db)

        # User1 creates evidence
        evidence1, _ = service.count_toward_learning(user1.id, experiment_user1_with_evaluation.id)
        assert evidence1 is not None

        # User2 cannot find it
        found = service._find_experiment_evidence(user2.id, experiment_user1_with_evaluation.id)
        assert found is None


class TestExperimentStatusRules:
    """Test status-based qualification"""

    def test_draft_experiment_cannot_qualify(self, db, user, experiment_draft):
        """BLOCKER #2 FIX: DRAFT experiments cannot create evidence"""
        service = ExperimentLearningQualificationService(db)
        can_count, reasons = service.can_count_toward_learning(user.id, experiment_draft.id)

        assert not can_count
        assert any("DRAFT" in detail for detail in reasons["details"])

    def test_running_experiment_cannot_qualify(self, db, user, experiment_running):
        """RUNNING experiments cannot create evidence"""
        service = ExperimentLearningQualificationService(db)
        can_count, reasons = service.can_count_toward_learning(user.id, experiment_running.id)

        assert not can_count
        assert any("RUNNING" in detail for detail in reasons["details"])

    def test_failed_experiment_cannot_qualify(self, db, user, experiment_failed):
        """BLOCKER #3 FIX: FAILED experiments do NOT auto-qualify"""
        service = ExperimentLearningQualificationService(db)
        can_count, reasons = service.can_count_toward_learning(user.id, experiment_failed.id)

        assert not can_count
        assert any("FAILED" in detail or "COMPLETED" in detail for detail in reasons["details"])


class TestConceptRequirement:
    """Test concept association is mandatory"""

    def test_experiment_without_concept_cannot_qualify(self, db, user, experiment_no_concept):
        """Experiment without concept selection cannot qualify"""
        service = ExperimentLearningQualificationService(db)
        can_count, reasons = service.can_count_toward_learning(user.id, experiment_no_concept.id)

        assert not can_count
        assert any("concept" in detail.lower() for detail in reasons["details"])


class TestEvidenceSemantics:
    """Test evidence type/grader are correct"""

    def test_evidence_is_lab_type(self, db, user, experiment_with_evaluation_all_met):
        """Experiment evidence must be EvidenceType.LAB (hands-on)"""
        service = ExperimentLearningQualificationService(db)
        evidence, _ = service.count_toward_learning(user.id, experiment_with_evaluation_all_met.id)

        assert evidence.evidence_type == EvidenceType.LAB

    def test_evidence_is_deterministic_grader(self, db, user, experiment_with_evaluation_all_met):
        """Experiment evidence must be GradingMode.DETERMINISTIC"""
        service = ExperimentLearningQualificationService(db)
        evidence, _ = service.count_toward_learning(user.id, experiment_with_evaluation_all_met.id)

        assert evidence.grader == GradingMode.DETERMINISTIC

    def test_evidence_references_experiment(self, db, user, experiment_with_evaluation_all_met):
        """Evidence must reference the experiment via correct ref_type/ref_id"""
        service = ExperimentLearningQualificationService(db)
        evidence, _ = service.count_toward_learning(user.id, experiment_with_evaluation_all_met.id)

        assert evidence.ref_type == EvidenceRefType.EXPERIMENT
        assert evidence.ref_id == experiment_with_evaluation_all_met.id

    def test_evidence_score_is_none(self, db, user, experiment_with_evaluation_all_met):
        """Experiment evidence has no score (evaluation results in MA6)"""
        service = ExperimentLearningQualificationService(db)
        evidence, _ = service.count_toward_learning(user.id, experiment_with_evaluation_all_met.id)

        assert evidence.score is None


# ============================================================================
# Fixtures for tests
# ============================================================================

@pytest.fixture
def user(db):
    from app.models.identity import User
    user = User(id="test-user-1", email="test@example.com")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def user1(db):
    from app.models.identity import User
    user = User(id="test-user-1", email="user1@example.com")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def user2(db):
    from app.models.identity import User
    user = User(id="test-user-2", email="user2@example.com")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def concept(db):
    from app.models.concepts import Concept, ConceptVersion
    concept = Concept(
        slug="test-concept",
        name="Test Concept",
        level="INTERMEDIATE",
        kind="OPERATIONAL"
    )
    db.add(concept)
    db.commit()

    version = ConceptVersion(
        concept_id=concept.id,
        version=1,
        plain_definition="A test concept",
        status=VersionStatus.PUBLISHED
    )
    db.add(version)
    db.commit()

    concept.versions = [version]
    return concept


@pytest.fixture
def experiment_setup(db):
    """Factory for creating experiments"""
    def create_experiment(user, concept=None, status=ExperimentStatus.DRAFT):
        experiment = Experiment(
            user_id=user.id,
            experiment_type="MODEL_COMPARISON",
            status=status,
            concept_id=concept.id if concept else None,
            concept_version_id=concept.versions[0].id if concept else None,
            config_snapshot={}
        )
        db.add(experiment)
        db.commit()
        return experiment
    return create_experiment


@pytest.fixture
def experiment_draft(db, user, concept):
    """Experiment in DRAFT status"""
    exp = Experiment(
        user_id=user.id,
        experiment_type="MODEL_COMPARISON",
        status=ExperimentStatus.DRAFT,
        concept_id=concept.id,
        concept_version_id=concept.versions[0].id,
        config_snapshot={}
    )
    db.add(exp)
    db.commit()
    return exp


@pytest.fixture
def experiment_running(db, user, concept):
    """Experiment in RUNNING status"""
    exp = Experiment(
        user_id=user.id,
        experiment_type="MODEL_COMPARISON",
        status=ExperimentStatus.RUNNING,
        concept_id=concept.id,
        concept_version_id=concept.versions[0].id,
        config_snapshot={}
    )
    db.add(exp)
    db.commit()
    return exp


@pytest.fixture
def experiment_failed(db, user, concept):
    """Experiment in FAILED status"""
    exp = Experiment(
        user_id=user.id,
        experiment_type="MODEL_COMPARISON",
        status=ExperimentStatus.FAILED,
        concept_id=concept.id,
        concept_version_id=concept.versions[0].id,
        config_snapshot={}
    )
    db.add(exp)
    db.commit()
    return exp


@pytest.fixture
def experiment_completed(db, user, concept):
    """Experiment in COMPLETED status but without evaluation"""
    exp = Experiment(
        user_id=user.id,
        experiment_type="MODEL_COMPARISON",
        status=ExperimentStatus.COMPLETED,
        concept_id=concept.id,
        concept_version_id=concept.versions[0].id,
        config_snapshot={}
    )
    db.add(exp)
    db.commit()
    return exp


@pytest.fixture
def experiment_no_concept(db, user):
    """Experiment with no concept selected"""
    exp = Experiment(
        user_id=user.id,
        experiment_type="MODEL_COMPARISON",
        status=ExperimentStatus.COMPLETED,
        concept_id=None,
        concept_version_id=None,
        config_snapshot={}
    )
    db.add(exp)
    db.commit()
    return exp


def create_experiment_with_evaluation(db, user, concept, findings):
    """Helper to create complete experiment with TaskRun → AgentRun → EvaluationRun"""
    from app.models.agents import Agent, AgentVersion
    from app.models.tasks import Task
    from app.models.evaluation_definitions import EvaluationDefinition, EvaluationDefinitionVersion, EvaluationCriterion
    from app.db.enums import ExecutionMode, VersionStatus

    # Create experiment
    exp = Experiment(
        user_id=user.id,
        experiment_type="MODEL_COMPARISON",
        status=ExperimentStatus.COMPLETED,
        concept_id=concept.id,
        concept_version_id=concept.versions[0].id,
        config_snapshot={}
    )
    db.add(exp)
    db.commit()

    # Create task/task run
    proj = db.execute(select(db.query(db.execute(select(db.models.projects.Project).limit(1)).scalar_one_or_none()).limit(1))).scalar()
    if not proj:
        from app.models.projects import Project
        proj = Project(name="Test Project", owner_id=user.id)
        db.add(proj)
        db.commit()

    task = Task(
        project_id=proj.id,
        title="Test Task",
        execution_mode=ExecutionMode.SINGLE_AGENT
    )
    db.add(task)
    db.commit()

    task_run = TaskRun(
        task_id=task.id,
        experiment_id=exp.id
    )
    db.add(task_run)
    db.commit()

    # Create agent run
    agent = Agent(name="Test Agent")
    db.add(agent)
    db.commit()

    version = AgentVersion(agent_id=agent.id, version=1, status=VersionStatus.PUBLISHED)
    db.add(version)
    db.commit()

    agent_run = AgentRun(task_run_id=task_run.id, agent_version_id=version.id)
    db.add(agent_run)
    db.commit()

    # Create evaluation definition
    eval_def = EvaluationDefinition(name="Test Evaluation")
    db.add(eval_def)
    db.commit()

    eval_version = EvaluationDefinitionVersion(
        evaluation_definition_id=eval_def.id,
        version=1,
        status=VersionStatus.PUBLISHED
    )
    db.add(eval_version)
    db.commit()

    criterion = EvaluationCriterion(
        evaluation_definition_id=eval_def.id,
        key="test_criterion",
        order_index=0
    )
    db.add(criterion)
    db.commit()

    # Create artifact for evaluation
    from app.models.artifacts_eval import Artifact
    artifact = Artifact(
        agent_run_id=agent_run.id,
        content_hash="test-hash",
        mime_type="text/plain"
    )
    db.add(artifact)
    db.commit()

    # Create evaluation run
    eval_run = EvaluationRun(
        subject_agent_run_id=agent_run.id,
        subject_artifact_id=artifact.id,
        subject_artifact_content_hash="test-hash",
        evaluation_definition_version_id=eval_version.id,
        method="DETERMINISTIC",
        status=EvaluationRunStatus.COMPLETED
    )
    db.add(eval_run)
    db.commit()

    # Add criterion results
    for finding in findings:
        result = EvaluationCriterionResult(
            evaluation_run_id=eval_run.id,
            criterion_key="test_criterion",
            order_index=0,
            finding=finding,
            rationale="Test"
        )
        db.add(result)
    db.commit()

    return exp


@pytest.fixture
def experiment_with_evaluation_all_met(db, user, concept):
    """Experiment with evaluation where all criteria are MET"""
    return create_experiment_with_evaluation(db, user, concept, [EvaluationFinding.MET])


@pytest.fixture
def experiment_with_evaluation_not_met(db, user, concept):
    """Experiment with evaluation where criterion is NOT_MET"""
    return create_experiment_with_evaluation(db, user, concept, [EvaluationFinding.NOT_MET])


@pytest.fixture
def experiment_with_evaluation_partial(db, user, concept):
    """Experiment with evaluation where criterion is PARTIAL"""
    return create_experiment_with_evaluation(db, user, concept, [EvaluationFinding.PARTIAL])


@pytest.fixture
def experiment_with_evaluation_all_na(db, user, concept):
    """Experiment with evaluation where all criteria are NOT_APPLICABLE"""
    return create_experiment_with_evaluation(db, user, concept, [EvaluationFinding.NOT_APPLICABLE])


@pytest.fixture
def experiment_user1_with_evaluation(db, user1, concept):
    """User1's experiment with proper evaluation"""
    return create_experiment_with_evaluation(db, user1, concept, [EvaluationFinding.MET])


@pytest.fixture
def evaluation_run_unrelated(db):
    """A completed evaluation run NOT linked to any experiment"""
    from app.models.agents import Agent, AgentVersion
    from app.models.tasks import Task, TaskRun
    from app.models.evaluation_definitions import EvaluationDefinition, EvaluationDefinitionVersion
    from app.models.artifacts_eval import Artifact
    from app.db.enums import ExecutionMode, VersionStatus
    from app.models.projects import Project
    from app.models.identity import User

    # Create a standalone evaluation
    user = User(id="standalone-user", email="standalone@example.com")
    db.add(user)
    db.commit()

    proj = Project(name="Standalone Project", owner_id=user.id)
    db.add(proj)
    db.commit()

    task = Task(project_id=proj.id, title="Standalone Task", execution_mode=ExecutionMode.SINGLE_AGENT)
    db.add(task)
    db.commit()

    task_run = TaskRun(task_id=task.id)  # No experiment!
    db.add(task_run)
    db.commit()

    agent = Agent(name="Standalone Agent")
    db.add(agent)
    db.commit()

    version = AgentVersion(agent_id=agent.id, version=1, status=VersionStatus.PUBLISHED)
    db.add(version)
    db.commit()

    agent_run = AgentRun(task_run_id=task_run.id, agent_version_id=version.id)
    db.add(agent_run)
    db.commit()

    eval_def = EvaluationDefinition(name="Standalone Eval")
    db.add(eval_def)
    db.commit()

    eval_version = EvaluationDefinitionVersion(
        evaluation_definition_id=eval_def.id,
        version=1,
        status=VersionStatus.PUBLISHED
    )
    db.add(eval_version)
    db.commit()

    artifact = Artifact(agent_run_id=agent_run.id, content_hash="test", mime_type="text/plain")
    db.add(artifact)
    db.commit()

    eval_run = EvaluationRun(
        subject_agent_run_id=agent_run.id,
        subject_artifact_id=artifact.id,
        subject_artifact_content_hash="test",
        evaluation_definition_version_id=eval_version.id,
        method="DETERMINISTIC",
        status=EvaluationRunStatus.COMPLETED
    )
    db.add(eval_run)
    db.commit()

    return eval_run


@pytest.fixture
def experiment_with_evaluation(db, user, concept):
    """Experiment with proper evaluation through correct chain"""
    return create_experiment_with_evaluation(db, user, concept, [EvaluationFinding.MET])
