"""Prompt construction from immutable/versioned execution inputs — MA3,
Section 10.

Combines the Agent Version's system_instructions (via its Prompt
Version), and the Task's own input, into exactly what the provider
receives. Never mutates the Agent Version or Prompt Version it reads from
— both are already frozen/immutable per Section 12.3.

Reproducibility without exposing secrets: rather than persisting the full
rendered prompt text in a new column, callers record a
``PromptAssembly.content_hash`` (sha256) — the assembled prompt is always
re-derivable byte-for-byte from (prompt_version.content, task.description,
task.requirements), all of which are already durably stored — the hash is
what proves a later re-derivation matches what was actually sent.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from app.models.agents import AgentVersion, PromptVersion
from app.models.tasks import Task


@dataclass
class PromptAssembly:
    system_prompt: Optional[str]
    user_prompt: str
    content_hash: str
    length_chars: int


def build_prompt(
    *,
    agent_version: AgentVersion,
    prompt_version: Optional[PromptVersion],
    task: Task,
    extra_context: Optional[str] = None,
) -> PromptAssembly:
    """``extra_context`` (MA4) is an opaque, already-rendered text block
    appended after the Task-derived user prompt — e.g. a candidate
    artifact + review instructions for a REVIEWER run, or a previous
    candidate + reviewer issues + repair instructions for a REPAIR run
    (see ``app.review_contract``). Defaults to ``None`` so a plain
    SINGLE_AGENT run's prompt/``content_hash`` is byte-for-byte identical
    to MA3's, unchanged."""
    system_prompt = prompt_version.content if prompt_version else None

    user_parts = [task.title]
    if task.description:
        user_parts.append(task.description)
    if task.requirements:
        user_parts.append(json.dumps(task.requirements, sort_keys=True, default=str))
    if extra_context:
        user_parts.append(extra_context)
    user_prompt = "\n\n".join(user_parts)

    combined = f"{system_prompt or ''}\n---\n{user_prompt}"
    content_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()

    return PromptAssembly(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        content_hash=content_hash,
        length_chars=len(combined),
    )
