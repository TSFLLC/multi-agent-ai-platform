"""Prompt construction — Section 10, MA3."""

from app.prompt_builder import build_prompt
from tests.conftest import make_agent, make_agent_version, make_task


def test_build_prompt_combines_system_and_task(db):
    from app.models.agents import PromptVersion

    agent = make_agent(db)
    prompt_version = PromptVersion(agent_id=agent.id, version=1, content="You are terse.")
    db.add(prompt_version)
    db.flush()
    agent_version = make_agent_version(db, agent=agent)
    agent_version.prompt_version_id = prompt_version.id
    task = make_task(db)
    task.description = "Do the specific thing."
    db.commit()

    assembly = build_prompt(agent_version=agent_version, prompt_version=prompt_version, task=task)
    assert assembly.system_prompt == "You are terse."
    assert task.title in assembly.user_prompt
    assert task.description in assembly.user_prompt
    assert len(assembly.content_hash) == 64


def test_build_prompt_with_no_prompt_version_has_no_system_prompt(db):
    task = make_task(db)
    agent_version = make_agent_version(db)
    assembly = build_prompt(agent_version=agent_version, prompt_version=None, task=task)
    assert assembly.system_prompt is None
    assert task.title in assembly.user_prompt


def test_build_prompt_is_deterministic_same_inputs_same_hash(db):
    task = make_task(db)
    agent_version = make_agent_version(db)
    a = build_prompt(agent_version=agent_version, prompt_version=None, task=task)
    b = build_prompt(agent_version=agent_version, prompt_version=None, task=task)
    assert a.content_hash == b.content_hash


def test_build_prompt_includes_requirements_when_present(db):
    task = make_task(db)
    task.requirements = {"must_include": "foo"}
    db.commit()
    agent_version = make_agent_version(db)
    assembly = build_prompt(agent_version=agent_version, prompt_version=None, task=task)
    assert "foo" in assembly.user_prompt
