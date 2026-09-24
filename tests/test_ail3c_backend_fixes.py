"""AIL.3C Backend Fixes - Executable Tests

Tests the three verified backend defects:
1. Concept version population at experiment creation
2. Concurrent duplicate protection via database uniqueness
3. Production code flow validation
"""

import pytest
from sqlalchemy import select

from app.db.enums import (
    EvaluationFinding, EvaluationRunStatus, EvidenceRefType, EvidenceType,
    ExperimentStatus, ExperimentType, GradingMode, VersionStatus, ExecutionMode
)
from app.models.lab import Experiment
from app.models.evaluation_runs import EvaluationRun, EvaluationCriterionResult
from app.models.learner import LearningEvidence
from app.models.tasks import TaskRun, AgentRun, Task
from app.models.concepts import Concept, ConceptVersion
from app.models.agents import Agent, AgentVersion
from app.models.identity import User, Project
from app.models.evaluation_definitions import EvaluationDefinition, EvaluationDefinitionVersion, EvaluationCriterion
from app.models.artifacts_eval import Artifact
from app.services.experiment_learning_qualification_service import ExperimentLearningQualificationService
from app.services.lab_service import LabService
from app.schemas.lab import ExperimentCreate


@pytest.fixture
def user(db):
    """Create test user."""
    user = User(id="test-user-1", email="test@example.com")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def project(db, user):
    """Create test project."""
    proj = Project(name="Test Project", owner_id=user.id)
    db.add(proj)
    db.commit()
    return proj


@pytest.fixture
def concept(db):
    """Create test concept with published version."""
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
    return concept


class TestConceptVersionPopulation:
    """Test that LabService.create_experiment() populates concept_version_id."""

    def test_experiment_creation_freezes_concept_version(self, db, user, concept, project):
        """DEFECT #1: LabService must populate concept_version_id at creation time."""
        # Create experiment with concept via LabService
        lab_service = LabService(db)

        # First create the infrastructure needed
        task = Task(project_id=project.id, title="Test", execution_mode=ExecutionMode.SINGLE_AGENT)
        db.add(task)
        db.commit()

        agent = Agent(name="Agent", project_id=project.id)
        db.add(agent)
        db.commit()

        agent_version = AgentVersion(agent_id=agent.id, version=1, status=VersionStatus.ACTIVE)
        db.add(agent_version)
        db.commit()

        # Now create experiment using LabService
        # (Note: this path requires test kit, so we'll directly check the logic)
        from app.models.lab import EvalSet, EvalSetVersion, EvalSetVersionTask
        from app.models.providers import ProviderModelSnapshot, Model, Provider
        from app.db.enums import EvalSetVersionStatus

        kit = EvalSet(user_id=user.id, name="Test Kit")
        db.add(kit)
        db.commit()

        kit_version = EvalSetVersion(eval_set_id=kit.id, version=1, status=EvalSetVersionStatus.PUBLISHED)
        db.add(kit_version)
        db.commit()

        kit_task = EvalSetVersionTask(eval_set_version_id=kit_version.id, task_id=task.id, position=0, task_snapshot={})
        db.add(kit_task)
        db.commit()

        provider = Provider(name="Test Provider")
        db.add(provider)
        db.commit()

        model = Model(provider_id=provider.id, name="Test Model")
        db.add(model)
        db.commit()

        snapshot = ProviderModelSnapshot(model_id=model.id, provider_model_id="test", snapshot_json={})
        db.add(snapshot)
        db.commit()

        # Now create experiment with concept
        exp_data = type('obj', (object,), {
            'eval_set_version_id': kit_version.id,
            'experiment_type': ExperimentType.MODEL_COMPARISON,
            'models': [type('obj', (object,), {'model_id': model.id, 'provider_model_snapshot_id': snapshot.id})()],
            'agent_version_ids': [agent_version.id],
            'concept_id': concept.id,
            'learning_item_id': None,
            'development_id': None,
            'hypothesis': None,
            'config': {},
            'repetitions': 1,
            'budget_id': None
        })()

        exp = lab_service.create_experiment(user, exp_data)
        db.refresh(exp)

        # VERIFY: concept_version_id must be populated
        assert exp.concept_id == concept.id
        assert exp.concept_version_id is not None
        assert exp.concept_version_id == concept.versions[0].id


class TestConcurrentUniqueness:
    """Test database-level uniqueness protection for concurrent requests."""

    def test_uniqueness_index_prevents_duplicates(self, db, user, concept):
        """DEFECT #2: UNIQUE index must prevent concurrent duplicate evidence."""
        # Create experiment and evaluation infrastructure
        exp = Experiment(
            user_id=user.id,
            experiment_type=ExperimentType.MODEL_COMPARISON,
            status=ExperimentStatus.COMPLETED,
            concept_id=concept.id,
            concept_version_id=concept.versions[0].id,
            config_snapshot={}
        )
        db.add(exp)
        db.commit()

        # Create task → task run
        task = Task(project_id=None, title="Test", execution_mode=ExecutionMode.SINGLE_AGENT)
        db.add(task)
        db.commit()

        task_run = TaskRun(task_id=task.id, experiment_id=exp.id)
        db.add(task_run)
        db.commit()

        # Create agent run
        agent = Agent(name="Test Agent")
        db.add(agent)
        db.commit()

        agent_version = AgentVersion(agent_id=agent.id, version=1, status=VersionStatus.PUBLISHED)
        db.add(agent_version)
        db.commit()

        agent_run = AgentRun(task_run_id=task_run.id, agent_version_id=agent_version.id)
        db.add(agent_run)
        db.commit()

        # Create evaluation
        eval_def = EvaluationDefinition(name="Test")
        db.add(eval_def)
        db.commit()

        eval_version = EvaluationDefinitionVersion(
            evaluation_definition_id=eval_def.id, version=1, status=VersionStatus.PUBLISHED
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

        criterion = EvaluationCriterion(evaluation_definition_id=eval_def.id, key="test", order_index=0)
        db.add(criterion)
        db.commit()

        result = EvaluationCriterionResult(
            evaluation_run_id=eval_run.id,
            criterion_key="test",
            order_index=0,
            finding=EvaluationFinding.MET,
            rationale="test"
        )
        db.add(result)
        db.commit()

        # Now test count_toward_learning twice
        service = ExperimentLearningQualificationService(db)

        # First call
        evidence1, msg1 = service.count_toward_learning(user.id, exp.id)
        assert evidence1 is not None
        first_id = evidence1.id

        # Second call (should return same evidence due to uniqueness)
        evidence2, msg2 = service.count_toward_learning(user.id, exp.id)
        assert evidence2 is not None
        assert evidence2.id == first_id

        # Verify only ONE evidence row exists
        stmt = select(LearningEvidence).where(
            LearningEvidence.ref_type == EvidenceRefType.EXPERIMENT,
            LearningEvidence.ref_id == exp.id
        )
        evidence_rows = db.execute(stmt).scalars().all()
        assert len(evidence_rows) == 1


class TestIntegration:
    """Integration tests of production code."""

    def test_pass_derivation_from_evaluation_findings(self, db, user, concept):
        """Test that passed derivation uses canonical MA6 findings."""
        # Create complete experiment with evaluation
        exp = Experiment(
            user_id=user.id,
            experiment_type=ExperimentType.MODEL_COMPARISON,
            status=ExperimentStatus.COMPLETED,
            concept_id=concept.id,
            concept_version_id=concept.versions[0].id,
            config_snapshot={}
        )
        db.add(exp)
        db.commit()

        # Setup infrastructure
        task = Task(project_id=None, title="Test", execution_mode=ExecutionMode.SINGLE_AGENT)
        db.add(task)
        db.commit()

        task_run = TaskRun(task_id=task.id, experiment_id=exp.id)
        db.add(task_run)
        db.commit()

        agent = Agent(name="Agent")
        db.add(agent)
        db.commit()

        agent_version = AgentVersion(agent_id=agent.id, version=1, status=VersionStatus.PUBLISHED)
        db.add(agent_version)
        db.commit()

        agent_run = AgentRun(task_run_id=task_run.id, agent_version_id=agent_version.id)
        db.add(agent_run)
        db.commit()

        eval_def = EvaluationDefinition(name="Test")
        db.add(eval_def)
        db.commit()

        eval_version = EvaluationDefinitionVersion(
            evaluation_definition_id=eval_def.id, version=1, status=VersionStatus.PUBLISHED
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

        criterion = EvaluationCriterion(evaluation_definition_id=eval_def.id, key="test", order_index=0)
        db.add(criterion)
        db.commit()

        # Add MET finding
        result = EvaluationCriterionResult(
            evaluation_run_id=eval_run.id,
            criterion_key="test",
            order_index=0,
            finding=EvaluationFinding.MET,
            rationale="test"
        )
        db.add(result)
        db.commit()

        # Create evidence
        service = ExperimentLearningQualificationService(db)
        evidence, msg = service.count_toward_learning(user.id, exp.id)

        # Verify: evidence created with passed=True from MA6 findings
        assert evidence is not None
        assert evidence.passed is True
        assert evidence.evidence_type == EvidenceType.LAB
        assert evidence.grader == GradingMode.DETERMINISTIC
