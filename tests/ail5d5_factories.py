"""AIL.5D.5 test factories: a structured lab Day bound to a real Educational Lab Kit, and a stand-in for the worker.

A run is an ordinary Task Run + Agent Run (the existing engine). Tests have no worker, so ``finish_run`` writes what the worker
would: the Agent Run's terminal status and its output artifact."""

import copy

from app import academy_lab_kits as kits
from app.db.enums import AgentRunStatus
from app.models.academy import AcademyPracticeRun
from app.models.tasks import AgentRun
from tests.ail5d1_factories import make_structured_item, sample_spec
from tests.conftest import make_artifact_with_content

KIT = "day5-grounding-lab"
GUIDED = "grounded-vs-ungrounded"
INDEPENDENT = "practice-misleading-source"
LAB_STEP = "lab-run"
PRACTICE_STEP = "practice-again"


def lab_steps(*, practice_pool=(INDEPENDENT,), kit=KIT, scenario=GUIDED):
    return [
        {"key": "intro", "type": "teach", "title": "Why test grounding?", "estimated_minutes": 3, "required": True,
         "content": {"blocks": [{"kind": "md", "text": "Grounding gives the model a source."}]}},
        {"key": LAB_STEP, "type": "lab", "title": "Grounded vs ungrounded", "estimated_minutes": 15, "required": True,
         "content": {"problem_md": "Does a source change the answer?"},
         "binding": {"kind": "personal_lab", "engine": "personal_lab_experiment", "kit": {"kit_key": kit, "scenario_key": scenario}}},
        {"key": PRACTICE_STEP, "type": "practice", "title": "Practice this concept", "estimated_minutes": 10, "required": False,
         "content": {"prompt_md": "Try it again on your own."},
         "binding": {"kind": "academy_lab_kit", "kit_key": kit, "scenario_keys": list(practice_pool)}},
    ]


def make_lab_day(db, *, slug="d5-lab-concept", day=5, steps=None):
    spec = sample_spec(day=day, kind="lab", steps=steps if steps is not None else lab_steps())
    spec.pop("knowledge_check", None)
    return make_structured_item(db, slug=slug, spec=spec, title=f"Day {day}: Grounded vs. ungrounded")


def finish_run(db, tmp_path, run_id, text="The museum opened in 1968."):
    """What the worker does when a run succeeds."""
    run = db.get(AcademyPracticeRun, run_id)
    agent_run = db.get(AgentRun, run.agent_run_id)
    agent_run.status = AgentRunStatus.COMPLETED
    db.flush()
    make_artifact_with_content(db, tmp_path, agent_run=agent_run, content=text)
    db.commit()


def fail_run(db, run_id):
    run = db.get(AcademyPracticeRun, run_id)
    db.get(AgentRun, run.agent_run_id).status = AgentRunStatus.FAILED
    db.commit()


def kit_with(mutator, *, key="test-kit"):
    """A registered, validated copy of the day5 kit under another key (so a test can add scenarios or a model variable)."""
    spec = copy.deepcopy(kits.KITS[KIT])
    spec["kit_key"] = spec["lab_kit"]["key"] = key
    mutator(spec["lab_kit"])
    from app import academy_lab_kit as K

    K.validate_lab_kit(spec)
    kits.KITS[key] = spec
    return spec
