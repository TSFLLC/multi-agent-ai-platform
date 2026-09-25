"""AIL.5C role separation, context isolation and no-direct-mutation guarantees.

These are structural (import-graph / AST) proofs plus runtime checks:

* Professor and Mentor code cannot reach grading or the evidence writer;
* the Grader's packet code cannot reach Mentor / Professor / profile / state;
* the Grader service never writes evidence or state;
* ``LearningEvidenceService.supersede`` is only used by the review service;
* only the Grader service creates GRADER runs.
"""

import ast
import pathlib
from typing import Dict, Set

import pytest

from app.models.agents import Agent
from app.services.assessment_grader_service import GRADER_AGENT_ID, AssessmentGraderService
from app.services.professor_execution_service import PROFESSOR_AGENT_ID
from app.services.system_project_service import ensure_ail_system_project

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _module_name(path: pathlib.Path) -> str:
    return ".".join(path.relative_to(APP.parent).with_suffix("").parts)


def _imports(path: pathlib.Path) -> Set[str]:
    found: Set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def _graph() -> Dict[str, Set[str]]:
    modules = {_module_name(p): p for p in APP.rglob("*.py")}
    graph = {}
    for name, path in modules.items():
        graph[name] = {m for m in _imports(path) if m in modules}
    return graph


# The shared execution engine is a boundary, not a dependency of the Grader's own
# code: it lazily imports the Professor for the Professor's prompt.
EXECUTOR = "app.services.execution_service"


def _closure(start: str, stop=frozenset({EXECUTOR})) -> Set[str]:
    graph, seen, todo = _graph(), set(), [start]
    while todo:
        module = todo.pop()
        if module in seen or module not in graph:
            continue
        seen.add(module)
        if module in stop and module != start:
            continue
        todo.extend(graph[module])
    return seen


GRADING = {
    "app.services.assessment_grader_service",
    "app.services.assessment_service",
    "app.services.assessment_review_service",
    "app.services.assessment_evidence_writer",
}
MENTOR_PROFESSOR = [
    "app.services.project_mentor_service",
    "app.services.professor_execution_service",
    "app.services.professor_context_service",
    "app.services.professor_contract",
]


@pytest.mark.parametrize("module", MENTOR_PROFESSOR)
def test_mentor_and_professor_cannot_reach_grading_or_the_evidence_writer(module):
    reachable = _closure(module)
    assert not (reachable & GRADING), sorted(reachable & GRADING)


def test_the_grader_packet_cannot_reach_mentor_professor_profile_or_state():
    forbidden = {
        "app.services.project_mentor_service",
        "app.services.professor_execution_service",
        "app.services.professor_context_service",
        "app.services.learner_profile_service",
        "app.services.learner_state_service",
        "app.services.learning_plan_service",
        "app.services.learning_evidence_service",
        "app.models.learner",  # profile / interests / plan / evidence rows
    }
    reachable = _closure("app.services.assessment_grading_packet")
    assert not (reachable & forbidden), sorted(reachable & forbidden)


def test_the_grader_service_never_writes_evidence_or_state():
    reachable = _closure("app.services.assessment_grader_service")
    assert "app.services.assessment_evidence_writer" not in reachable
    assert "app.services.learner_state_service" not in reachable
    assert "app.services.learning_evidence_service" not in reachable
    source = (APP / "services" / "assessment_grader_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not called & {"record_evidence", "supersede"}


def test_supersede_is_only_used_by_the_assessment_review_service():
    users = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "supersede"
            ):
                users.append(path.name)
    assert set(users) == {"assessment_review_service.py"}


def test_only_the_grader_service_creates_grader_runs():
    creators = []
    for path in APP.rglob("*.py"):
        if path.name == "enums.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "GRADER"
                and isinstance(node.value, ast.Name)
                and node.value.id == "AgentRunRole"
            ):
                creators.append(path.name)
    assert set(creators) == {"assessment_grader_service.py"}


def test_only_evidence_writer_and_review_service_append_learning_evidence():
    writers = []
    for path in APP.rglob("*.py"):
        if "assessment" not in path.name:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("record_evidence",)
            ):
                writers.append(path.name)
    assert set(writers) <= {"assessment_evidence_writer.py", "assessment_review_service.py"}


def test_no_assessment_code_sets_a_ladder_rung():
    """State is derived by LearnerStateService only: no assessment module assigns,
    returns or stores a ladder value."""
    for path in APP.rglob("*.py"):
        if "assessment" not in path.name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)] + [
                    t.attr for t in node.targets if isinstance(t, ast.Attribute)
                ]
                assert not {"ladder", "learner_state", "mastery", "state_ladder"} & set(targets), (
                    path.name,
                    targets,
                )
    assert "learner_concept_states" not in {
        t for t in __import__("app.db.base", fromlist=["Base"]).Base.metadata.tables
    }


# -- runtime role separation ---------------------------------------------------------------------------------


def test_the_grader_is_a_separate_registered_agent_with_no_tools(db, bootstrap):
    project = ensure_ail_system_project(db, bootstrap.user)
    version = AssessmentGraderService(db).ensure_grader_agent(project)
    agent = db.get(Agent, GRADER_AGENT_ID)
    assert GRADER_AGENT_ID != PROFESSOR_AGENT_ID
    assert agent.role == "grader" and version.role == "grader" and agent.name == "Academy Grader"
    assert not version.tool_grants  # no tools, no retrieval, no web
    assert (
        "Do not give advice"
        in db.get(
            __import__("app.models.agents", fromlist=["PromptVersion"]).PromptVersion,
            version.prompt_version_id,
        ).content
    )
    # idempotent, and the Professor is unaffected
    assert AssessmentGraderService(db).ensure_grader_agent(project).id == version.id
    assert db.get(Agent, PROFESSOR_AGENT_ID) is None or db.get(Agent, PROFESSOR_AGENT_ID).role == "professor"


def test_the_professor_can_only_create_professor_runs(db):
    source = (APP / "services" / "professor_execution_service.py").read_text(encoding="utf-8")
    assert "AgentRunRole.PROFESSOR" in source and "AgentRunRole.GRADER" not in source
    mentor = (APP / "services" / "project_mentor_service.py").read_text(encoding="utf-8")
    assert "GRADER" not in mentor and "grade" not in mentor.lower().replace("upgrade", "")


def test_a_passing_assessment_never_edits_existing_evidence(db, bootstrap):
    from app.db.enums import EvidenceType, GradingMode
    from app.models.learner import LearningEvidence
    from app.services.assessment_service import AssessmentService
    from app.services.learning_evidence_service import LearningEvidenceService
    from tests.ail5c_factories import kc_definition, make_ail_concept

    user = bootstrap.user
    concept, version = make_ail_concept(db)
    old = LearningEvidenceService(db).record_evidence(
        user_id=user.id,
        concept_id=concept.id,
        concept_version_id=version.id,
        evidence_type=EvidenceType.LESSON_COMPLETED,
        grader=GradingMode.DETERMINISTIC,
    )
    snapshot = (old.id, old.evidence_type, old.passed, old.grader, old.superseded_by_id, old.score)
    defn = kc_definition(db, user, concept)
    svc = AssessmentService(db)
    attempt = svc.start(user, defn.definition_key)
    svc.save_draft(
        user.id,
        attempt.id,
        {"responses": {i["entry_key"]: {"selected": [1]} for i in attempt.challenge_instance["items"]}},
    )
    svc.submit(user, attempt.id, {"declaration": "no_external_help"})
    db.refresh(old)
    assert (old.id, old.evidence_type, old.passed, old.grader, old.superseded_by_id, old.score) == snapshot
    assert db.query(LearningEvidence).count() == 2  # append-only: one new row
