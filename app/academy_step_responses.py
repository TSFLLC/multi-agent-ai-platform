"""AIL.5D.3 — what a learner may submit to an interactive step, validated purely against the authored step.

The step definition (``app.academy_steps``) says what the step is; this module says what a valid response to it looks like.
It is pure (no database) so the same rules run in the service, the API and tests. A response is *data*: validating it never
grades it, and a valid response is never evidence.

Accepted shapes, by step type:

* ``reflect`` / ``reflection``  ``{"text": str}``                      -> one ``reflection`` response
* ``think`` (question)          ``{"text": str}``                      -> one ``answer`` response
* ``think`` (statements)        ``{"statements": {id: {"choice", "reasoning"?}}}``  -> one ``answer`` per statement id
* ``explain_back``              ``{"points": {key: str}}``            -> one ``outline`` per point key

Every other step type takes no direct response: ``teach`` / ``example`` complete on Continue, ``check`` is answered through
the existing knowledge-check mechanism, and ``lab`` belongs to the later Lab slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.academy_steps import CLAIM_ANSWERS, StepType
from app.db.enums import AcademyStepResponseKind

MAX_TEXT_CHARS = 8000
MAX_POINT_CHARS = 4000
MAX_REASONING_CHARS = 2000
DEFAULT_MIN_TEXT_CHARS = 8
MIN_POINT_CHARS = 5

RESPONDABLE = frozenset({StepType.REFLECT, StepType.REFLECTION, StepType.THINK, StepType.EXPLAIN_BACK})


class ResponseError(ValueError):
    """The submitted response is not valid for this step. The message says what to fix and never echoes private data."""


@dataclass(frozen=True)
class ResponsePart:
    kind: AcademyStepResponseKind
    key: str
    content: Dict[str, Any]


def _text(value: Any, what: str, *, lo: int, hi: int) -> str:
    if not isinstance(value, str):
        raise ResponseError(f"{what} must be text")
    value = value.strip()
    if len(value) < lo:
        raise ResponseError(f"{what} needs at least {lo} characters")
    if len(value) > hi:
        raise ResponseError(f"{what} must be at most {hi} characters")
    return value


def _only(payload: dict, allowed: set) -> None:
    extra = sorted(k for k, v in payload.items() if v is not None and k not in allowed)
    if extra:
        raise ResponseError(f"unexpected field(s) for this step: {', '.join(extra)}")


def accepts_response(step: dict) -> bool:
    return StepType(step["type"]) in RESPONDABLE


def validate_response(step: dict, payload: Optional[dict]) -> List[ResponsePart]:
    """The parts to persist for ``payload`` on ``step``, or ``ResponseError``."""
    if not isinstance(payload, dict):
        raise ResponseError("a response is required")
    step_type = StepType(step["type"])
    content = step["content"]
    if step_type in (StepType.REFLECT, StepType.REFLECTION):
        _only(payload, {"text"})
        minimum = int(content.get("min_chars", DEFAULT_MIN_TEXT_CHARS)) if step_type == StepType.REFLECT else DEFAULT_MIN_TEXT_CHARS
        return [ResponsePart(AcademyStepResponseKind.REFLECTION, "", {"text": _text(payload.get("text"), "your response", lo=minimum, hi=MAX_TEXT_CHARS)})]
    if step_type == StepType.THINK:
        if "statements" in content:
            _only(payload, {"statements"})
            given = payload.get("statements")
            ids = [s["id"] for s in content["statements"]]
            if not isinstance(given, dict) or set(given) != set(ids):
                raise ResponseError("answer every statement")
            parts = []
            for sid in ids:
                item = given[sid]
                if not isinstance(item, dict) or set(item) - {"choice", "reasoning"}:
                    raise ResponseError("each statement needs a choice and optional reasoning")
                if item.get("choice") not in CLAIM_ANSWERS:
                    raise ResponseError(f"each choice must be one of {', '.join(CLAIM_ANSWERS)}")
                body: Dict[str, Any] = {"choice": item["choice"]}
                if item.get("reasoning") not in (None, ""):
                    body["reasoning"] = _text(item["reasoning"], "your reasoning", lo=1, hi=MAX_REASONING_CHARS)
                parts.append(ResponsePart(AcademyStepResponseKind.ANSWER, sid, body))
            return parts
        _only(payload, {"text"})
        return [ResponsePart(AcademyStepResponseKind.ANSWER, "", {"text": _text(payload.get("text"), "your answer", lo=DEFAULT_MIN_TEXT_CHARS, hi=MAX_TEXT_CHARS)})]
    if step_type == StepType.EXPLAIN_BACK:
        _only(payload, {"points"})
        given = payload.get("points")
        keys = [p["key"] for p in content["points"]]
        if not isinstance(given, dict) or set(given) != set(keys):
            raise ResponseError("fill in every point")
        return [ResponsePart(AcademyStepResponseKind.OUTLINE, key, {"text": _text(given[key], "each point", lo=MIN_POINT_CHARS, hi=MAX_POINT_CHARS)}) for key in keys]
    raise ResponseError("this step does not take a direct response")
