"""AIL.5D.4 — the assistance model of the step-scoped AI Professor (pure: no database, no provider).

The Professor helps a learner with ONE authored step. How much help is appropriate depends on what the learner is doing,
so every request is placed in a *mode* and answered at a *level* the SERVER chooses (a client can ask for "a hint", never
for a level or a mode):

    mode         when                                   what the Professor does
    -----------  -------------------------------------  -----------------------------------------------------------------
    teaching     teach / example steps                  explains the concept normally
    guided       reflect, think, check, explain-back,   coaches through the activity and never completes it for the learner
                 reflection (and lab, later)
    independent  practice (5D.5)                        progressive help only: clarify -> hint -> stronger hint -> explain
    assessment   an AIL.5C attempt is open               NOT a Professor mode: the existing AIL.5C lock pauses the Professor

The ladder (``HELP_LADDER``) is shared by guided and independent modes. ``ask`` is a free question about the step (what does
this term mean, what is this step asking, give another example) and is answered at the ``clarify`` rung; ``hint`` climbs
one rung each time it is requested on the same step (hint, then stronger hint, then explain). The top rung (``explain``) is only reachable once explaining cannot solve the exercise for the learner (for
example after a think answer is committed, or after a knowledge check is passed).

Assistance is tracked on the same H0-H5 scale the platform already uses for independence (``AssistanceLevel``): the highest
level of help a learner received on a step is what a later practice/independence decision reads. Using the Professor never
creates evidence and never, by itself, changes what a learner has demonstrated.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

from app.academy_steps import StepType
from app.db.enums import AssistanceLevel


class ProfessorMode(str, Enum):
    TEACHING = "teaching"
    GUIDED = "guided"
    INDEPENDENT = "independent"


class HelpRequest(str, Enum):
    ASK = "ask"
    HINT = "hint"


class HelpLevel(str, Enum):
    CLARIFY = "clarify"
    HINT = "hint"
    STRONGER_HINT = "stronger_hint"
    EXPLAIN = "explain"


HELP_LADDER = (HelpLevel.CLARIFY, HelpLevel.HINT, HelpLevel.STRONGER_HINT, HelpLevel.EXPLAIN)

# Levels on the platform's existing assistance scale (H0 independent .. H5 solution shown). A free question in teaching
# mode is not assistance on an exercise, so it carries no level.
LEVEL_ASSISTANCE = {
    HelpLevel.CLARIFY: AssistanceLevel.H1,
    HelpLevel.HINT: AssistanceLevel.H2,
    HelpLevel.STRONGER_HINT: AssistanceLevel.H3,
    HelpLevel.EXPLAIN: AssistanceLevel.H4,
}

_TEACHING_TYPES = frozenset({StepType.TEACH.value, StepType.EXAMPLE.value})


def mode_for_step(step: Dict[str, Any]) -> ProfessorMode:
    """The mode of a step, from the authored step only. A future ``practice`` step declares ``mode: independent``."""
    declared = step.get("mode")
    if declared in (ProfessorMode.INDEPENDENT.value, ProfessorMode.GUIDED.value):
        return ProfessorMode(declared)
    return ProfessorMode.TEACHING if step["type"] in _TEACHING_TYPES else ProfessorMode.GUIDED


def solution_eligible(step: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """True when fully explaining the step cannot do the learner's exercise for them."""
    step_type = step["type"]
    if step_type in _TEACHING_TYPES:
        return True
    if step_type == StepType.THINK.value:
        return bool(state.get("committed"))
    if step_type == StepType.CHECK.value:
        return bool(state.get("passed"))
    if step_type in (StepType.REFLECT.value, StepType.REFLECTION.value, StepType.EXPLAIN_BACK.value):
        return True   # the question may be explained; the learner's own response may never be written for them
    return False      # lab (and anything new) stays on the hint rungs until its own slice decides


def decide_level(mode: ProfessorMode, request: HelpRequest, prior_hints: int, eligible: bool) -> HelpLevel:
    """The level this request is answered at. Server-decided, monotone, and never above the cap."""
    if mode == ProfessorMode.TEACHING and request == HelpRequest.ASK:
        return HelpLevel.EXPLAIN
    if request == HelpRequest.ASK:
        return HelpLevel.CLARIFY
    # A free question is the first rung (clarify). Each hint request climbs one rung from there: hint, stronger hint, explain.
    cap = len(HELP_LADDER) - 1 if eligible else HELP_LADDER.index(HelpLevel.STRONGER_HINT)
    return HELP_LADDER[min(1 + max(prior_hints, 0), cap)]


def assistance_for(mode: ProfessorMode, level: HelpLevel, *, revealed_solution: bool) -> Optional[AssistanceLevel]:
    """The H-level recorded for this help, or None when the help was not assistance on an exercise."""
    if mode == ProfessorMode.TEACHING:
        return None
    if revealed_solution:
        return AssistanceLevel.H5
    return LEVEL_ASSISTANCE[level]


_COMMON = (
    "Use only the supplied step context. Never invent content, answers or IDs. Never write the learner's own response for "
    "them. Never grade, pass, fail or mark anything; you cannot change progress or evidence."
)
_LEVEL_TEXT = {
    HelpLevel.CLARIFY: "Clarify what the step is asking or what a term means. Do not hint at the answer.",
    HelpLevel.HINT: "Give one short conceptual hint that points the learner's thinking in a useful direction. Do not state the answer.",
    HelpLevel.STRONGER_HINT: "Give a stronger, more specific hint that narrows the learner's options. Still do not state the answer.",
    HelpLevel.EXPLAIN: "Explain the idea fully. You may discuss any authored material present in the context.",
}


def instructions(mode: ProfessorMode, level: HelpLevel, step_type: str, eligible: bool) -> str:
    """Server-authored guidance that travels with the context facts."""
    if mode == ProfessorMode.TEACHING:
        body = "Explain this lesson step normally and clearly, using the step content and the learner's question."
    else:
        body = _LEVEL_TEXT[level]
        if not eligible:
            body += " The exercise is not finished: do not reveal, confirm or rule out the answer."
        if mode == ProfessorMode.INDEPENDENT:
            body += " This is independent practice: prefer the smallest help that unblocks the learner."
    return f"{body} {_COMMON}"


def describe(mode: ProfessorMode, level: HelpLevel, request: HelpRequest, *, prior_hints: int, eligible: bool, step_type: str) -> Dict[str, Any]:
    """The assistance facts placed in the Professor context (and echoed to the learner)."""
    return {
        "mode": mode.value, "level": level.value, "request": request.value, "prior_hints": prior_hints, "solution_eligible": eligible,
        "instructions": instructions(mode, level, step_type, eligible),
    }
