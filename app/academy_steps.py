"""AIL.5D.1 — the structured Day/Step authored contract.

A structured Day is an ordinary, immutable, versioned ``LearningItem`` whose ``spec_json`` carries an ordered
list of steps::

    spec = {..., "step_schema_version": 1, "steps": [ {step}, ... ]}

There is no second content store and no second versioning system: a new authored revision is a new
``LearningItem`` version (``ConceptGraphService.create_learning_item_version``), exactly like every other
Level 1 item. This module is pure (no database, no network) so the same rules run when content is authored,
when it is read, and in tests.

What this module owns
---------------------
* the closed step vocabulary and each type's content shape (``validate_structured_spec``);
* the split between what a learner may see and what stays on the server (``public_steps`` strips ``private``);
* a stable per-step ``fingerprint`` so step progress can tell "unchanged" from "revised" across item versions.

What it deliberately does not own
---------------------------------
* grading, readiness, evidence and demonstration — those stay in AIL.5C / ``LearningEvidence``. A step can
  *point at* an existing mechanism through ``binding`` (the Day's knowledge check, an AIL.5C assessment
  definition key, a Personal Lab engine); it never re-implements one.
* rendering. ``content`` is data; markdown fields are rendered by the existing safe renderer.

Non-structured items (every existing Learning Item) have no ``steps`` key and are untouched by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional

STEP_SCHEMA_VERSION = 1
MAX_STEPS = 40
MAX_SPEC_BYTES = 400_000
MAX_TEXT = 20_000
KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class StepType(str, Enum):
    TEACH = "teach"
    EXAMPLE = "example"
    REFLECT = "reflect"
    THINK = "think"
    CHECK = "check"
    EXPLAIN_BACK = "explain_back"
    LAB = "lab"
    REFLECTION = "reflection"
    PRACTICE = "practice"


# Steps a learner may complete just by pressing Continue. Every other type is completed by its own
# interaction (a committed answer, a graded check, a finished lab run, ...), which later slices verify
# on the server; until then the service refuses to complete them without a server-verified basis.
SELF_COMPLETABLE = frozenset({StepType.TEACH, StepType.EXAMPLE})

BLOCK_KINDS = ("heading", "md", "callout", "pair", "compare", "tabs", "accordion", "cards")
CALLOUT_TONES = ("idea", "key", "warn")
CLAIM_ANSWERS = ("true", "false", "depends")
LAB_ENGINES = (
    "personal_lab_experiment", "agent_version", "bounded_manual_grounding", "ma7_workflow",
    "evaluation_or_personal_lab", "build_with_me_agent",
)

STEP_KEYS = frozenset({"key", "type", "title", "estimated_minutes", "required", "content", "private", "binding", "professor_hint"})


class StepContractError(ValueError):
    """The authored steps do not satisfy the structured Day/Step contract. The message names the path."""


# -- tiny validators --------------------------------------------------------------------------------------------------


def _fail(path: str, message: str) -> None:
    raise StepContractError(f"{path}: {message}")


def _dict(value: Any, path: str, *, allowed: Optional[frozenset] = None, required: frozenset = frozenset()) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    if allowed is not None:
        extra = sorted(set(value) - allowed)
        if extra:
            _fail(path, f"unknown field(s): {', '.join(extra)}")
    missing = sorted(required - set(value))
    if missing:
        _fail(path, f"missing field(s): {', '.join(missing)}")
    return value


def _text(value: Any, path: str, *, max_len: int = MAX_TEXT, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")
    if len(value) > max_len:
        _fail(path, f"must be at most {max_len} characters")
    return value


def _int(value: Any, path: str, *, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        _fail(path, f"must be an integer between {lo} and {hi}")
    return value


def _list(value: Any, path: str, *, lo: int = 1, hi: int = 50) -> list:
    if not isinstance(value, list) or not lo <= len(value) <= hi:
        _fail(path, f"must be a list of {lo} to {hi} item(s)")
    return value


def _id(value: Any, path: str) -> str:
    if not isinstance(value, str) or not KEY_RE.match(value) or len(value) > 40:
        _fail(path, "must be a lowercase slug (letters, digits, hyphens; at most 40 characters)")
    return value


# -- content blocks (teach) -------------------------------------------------------------------------------------------


def _block(block: Any, path: str) -> None:
    b = _dict(block, path)
    kind = b.get("kind")
    if kind not in BLOCK_KINDS:
        _fail(path, f"kind must be one of {', '.join(BLOCK_KINDS)}")
    if kind in ("heading", "md"):
        _dict(b, path, allowed=frozenset({"kind", "text"}), required=frozenset({"text"}))
        _text(b["text"], f"{path}.text")
    elif kind == "callout":
        _dict(b, path, allowed=frozenset({"kind", "tone", "title", "text"}), required=frozenset({"tone", "text"}))
        if b["tone"] not in CALLOUT_TONES:
            _fail(f"{path}.tone", f"must be one of {', '.join(CALLOUT_TONES)}")
        _text(b.get("title"), f"{path}.title", max_len=160, optional=True)
        _text(b["text"], f"{path}.text")
    elif kind == "pair":
        _dict(b, path, allowed=frozenset({"kind", "left", "right"}), required=frozenset({"left", "right"}))
        for side in ("left", "right"):
            s = _dict(b[side], f"{path}.{side}", allowed=frozenset({"title", "text"}), required=frozenset({"title", "text"}))
            _text(s["title"], f"{path}.{side}.title", max_len=160)
            _text(s["text"], f"{path}.{side}.text")
    elif kind == "compare":
        _dict(b, path, allowed=frozenset({"kind", "head", "rows"}), required=frozenset({"head", "rows"}))
        head = _list(b["head"], f"{path}.head", lo=2, hi=2)
        for i, h in enumerate(head):
            _text(h, f"{path}.head[{i}]", max_len=160)
        for i, row in enumerate(_list(b["rows"], f"{path}.rows", hi=30)):
            for j, cell in enumerate(_list(row, f"{path}.rows[{i}]", lo=2, hi=2)):
                _text(cell, f"{path}.rows[{i}][{j}]", max_len=1000)
    elif kind == "tabs":
        _dict(b, path, allowed=frozenset({"kind", "items"}), required=frozenset({"items"}))
        for i, it in enumerate(_list(b["items"], f"{path}.items", lo=2, hi=6)):
            it = _dict(it, f"{path}.items[{i}]", allowed=frozenset({"label", "sub", "text"}), required=frozenset({"label", "text"}))
            _text(it["label"], f"{path}.items[{i}].label", max_len=120)
            _text(it.get("sub"), f"{path}.items[{i}].sub", max_len=160, optional=True)
            _text(it["text"], f"{path}.items[{i}].text")
    elif kind == "accordion":
        _dict(b, path, allowed=frozenset({"kind", "items"}), required=frozenset({"items"}))
        for i, it in enumerate(_list(b["items"], f"{path}.items", hi=12)):
            it = _dict(it, f"{path}.items[{i}]", allowed=frozenset({"title", "text"}), required=frozenset({"title", "text"}))
            _text(it["title"], f"{path}.items[{i}].title", max_len=200)
            _text(it["text"], f"{path}.items[{i}].text")
    elif kind == "cards":
        _dict(b, path, allowed=frozenset({"kind", "items"}), required=frozenset({"items"}))
        for i, it in enumerate(_list(b["items"], f"{path}.items", hi=12)):
            it = _dict(it, f"{path}.items[{i}]", allowed=frozenset({"title", "text", "caution"}), required=frozenset({"title", "text"}))
            _text(it["title"], f"{path}.items[{i}].title", max_len=160)
            _text(it["text"], f"{path}.items[{i}].text", max_len=2000)
            _text(it.get("caution"), f"{path}.items[{i}].caution", max_len=1000, optional=True)


# -- per-type content / private / binding -----------------------------------------------------------------------------


def _content_teach(c: dict, path: str, ctx: dict) -> None:
    _dict(c, path, allowed=frozenset({"blocks"}), required=frozenset({"blocks"}))
    for i, block in enumerate(_list(c["blocks"], f"{path}.blocks", hi=30)):
        _block(block, f"{path}.blocks[{i}]")


def _content_example(c: dict, path: str, ctx: dict) -> None:
    _dict(c, path, allowed=frozenset({"lead_md", "cases", "takeaway_md"}), required=frozenset({"cases"}))
    _text(c.get("lead_md"), f"{path}.lead_md", optional=True)
    _text(c.get("takeaway_md"), f"{path}.takeaway_md", optional=True)
    for i, case in enumerate(_list(c["cases"], f"{path}.cases", hi=4)):
        case = _dict(case, f"{path}.cases[{i}]", allowed=frozenset({"tag", "title", "verdict", "body_md"}), required=frozenset({"title", "body_md"}))
        _text(case.get("tag"), f"{path}.cases[{i}].tag", max_len=60, optional=True)
        _text(case["title"], f"{path}.cases[{i}].title", max_len=200)
        _text(case.get("verdict"), f"{path}.cases[{i}].verdict", max_len=200, optional=True)
        _text(case["body_md"], f"{path}.cases[{i}].body_md")


def _content_reflect(c: dict, path: str, ctx: dict) -> None:
    _dict(c, path, allowed=frozenset({"prompt_md", "min_chars"}), required=frozenset({"prompt_md"}))
    _text(c["prompt_md"], f"{path}.prompt_md")
    if "min_chars" in c:
        _int(c["min_chars"], f"{path}.min_chars", lo=1, hi=2000)


def _content_think(c: dict, path: str, ctx: dict) -> None:
    _dict(c, path, allowed=frozenset({"scenario_md", "question_md", "statements"}), required=frozenset({"scenario_md"}))
    _text(c["scenario_md"], f"{path}.scenario_md")
    if ("question_md" in c) == ("statements" in c):
        _fail(path, "needs exactly one of question_md or statements")
    if "question_md" in c:
        _text(c["question_md"], f"{path}.question_md")
    else:
        seen = set()
        for i, s in enumerate(_list(c["statements"], f"{path}.statements", hi=10)):
            s = _dict(s, f"{path}.statements[{i}]", allowed=frozenset({"id", "text"}), required=frozenset({"id", "text"}))
            sid = _id(s["id"], f"{path}.statements[{i}].id")
            if sid in seen:
                _fail(f"{path}.statements[{i}].id", f"duplicate statement id {sid!r}")
            seen.add(sid)
            _text(s["text"], f"{path}.statements[{i}].text", max_len=1000)


def _content_check(c: dict, path: str, ctx: dict) -> None:
    _dict(c, path, allowed=frozenset({"intro_md"}), required=frozenset({"intro_md"}))
    _text(c["intro_md"], f"{path}.intro_md")


def _content_explain_back(c: dict, path: str, ctx: dict) -> None:
    _dict(c, path, allowed=frozenset({"prompt_md", "points"}), required=frozenset({"prompt_md", "points"}))
    _text(c["prompt_md"], f"{path}.prompt_md")
    seen = set()
    for i, p in enumerate(_list(c["points"], f"{path}.points", hi=8)):
        p = _dict(p, f"{path}.points[{i}]", allowed=frozenset({"key", "label", "hint"}), required=frozenset({"key", "label"}))
        k = _id(p["key"], f"{path}.points[{i}].key")
        if k in seen:
            _fail(f"{path}.points[{i}].key", f"duplicate point key {k!r}")
        seen.add(k)
        _text(p["label"], f"{path}.points[{i}].label", max_len=200)
        _text(p.get("hint"), f"{path}.points[{i}].hint", max_len=500, optional=True)


def _content_lab(c: dict, path: str, ctx: dict) -> None:
    kit_bound = isinstance(ctx.get("binding"), dict) and "kit" in ctx["binding"]
    _dict(c, path, allowed=frozenset({"problem_md", "predictions", "prompt_text", "record"}), required=frozenset({"problem_md"} if kit_bound else {"problem_md", "predictions"}))
    _text(c["problem_md"], f"{path}.problem_md")
    if kit_bound and "predictions" in c:
        _fail(f"{path}.predictions", "a kit-bound lab takes its predictions from the Lab Kit scenario")
    seen = set()
    for i, p in enumerate(_list(c["predictions"], f"{path}.predictions", hi=8) if "predictions" in c else []):
        p = _dict(p, f"{path}.predictions[{i}]", allowed=frozenset({"id", "text"}), required=frozenset({"id", "text"}))
        pid = _id(p["id"], f"{path}.predictions[{i}].id")
        if pid in seen:
            _fail(f"{path}.predictions[{i}].id", f"duplicate prediction id {pid!r}")
        seen.add(pid)
        _text(p["text"], f"{path}.predictions[{i}].text", max_len=1000)
    _text(c.get("prompt_text"), f"{path}.prompt_text", max_len=4000, optional=True)
    if "record" in c:
        for i, r in enumerate(_list(c["record"], f"{path}.record", hi=10)):
            _text(r, f"{path}.record[{i}]", max_len=300)


def _content_practice(c: dict, path: str, ctx: dict) -> None:
    _dict(c, path, allowed=frozenset({"prompt_md"}), required=frozenset({"prompt_md"}))
    _text(c["prompt_md"], f"{path}.prompt_md")


def _content_reflection(c: dict, path: str, ctx: dict) -> None:
    _dict(c, path, allowed=frozenset({"prompt_md", "compare_to"}), required=frozenset({"prompt_md"}))
    _text(c["prompt_md"], f"{path}.prompt_md")
    if "compare_to" in c:
        target = c["compare_to"]
        if target not in ctx["earlier_reflect_keys"]:
            _fail(f"{path}.compare_to", "must be the key of an EARLIER reflect step")


_CONTENT = {
    StepType.TEACH: _content_teach, StepType.EXAMPLE: _content_example, StepType.REFLECT: _content_reflect,
    StepType.THINK: _content_think, StepType.CHECK: _content_check, StepType.EXPLAIN_BACK: _content_explain_back,
    StepType.LAB: _content_lab, StepType.REFLECTION: _content_reflection, StepType.PRACTICE: _content_practice,
}


def _private(step_type: StepType, step: dict, path: str) -> None:
    has = "private" in step
    if step_type == StepType.THINK:
        if not has:
            _fail(path, "a think step needs private (the reveal is server-side only)")
        p = _dict(step["private"], path, allowed=frozenset({"reveal_md", "claims"}))
        content = step["content"]
        if "statements" in content:
            claims = _dict(p.get("claims"), f"{path}.claims") if "claims" in p else _fail(f"{path}.claims", "required for a statements think step")
            ids = {s["id"] for s in content["statements"]}
            if set(claims) != ids:
                _fail(f"{path}.claims", "must have exactly one entry per statement id")
            for cid, claim in claims.items():
                claim = _dict(claim, f"{path}.claims.{cid}", allowed=frozenset({"answer", "why_md"}), required=frozenset({"answer", "why_md"}))
                if claim["answer"] not in CLAIM_ANSWERS:
                    _fail(f"{path}.claims.{cid}.answer", f"must be one of {', '.join(CLAIM_ANSWERS)}")
                _text(claim["why_md"], f"{path}.claims.{cid}.why_md")
            if "reveal_md" in p:
                _fail(f"{path}.reveal_md", "not allowed with statements")
        else:
            if "reveal_md" not in p or "claims" in p:
                _fail(path, "a question think step needs private.reveal_md only")
            _text(p["reveal_md"], f"{path}.reveal_md")
    elif step_type == StepType.LAB:
        if has:
            p = _dict(step["private"], path, allowed=frozenset({"authored_check_md"}), required=frozenset({"authored_check_md"}))
            _text(p["authored_check_md"], f"{path}.authored_check_md")
    elif has:
        _fail(path, f"a {step_type.value} step must not carry private content")


def _binding(step_type: StepType, step: dict, path: str, spec: dict) -> None:
    has = "binding" in step
    if step_type == StepType.CHECK:
        if not has:
            _fail(path, "a check step must bind to the Day's knowledge check")
        _dict(step["binding"], path, allowed=frozenset({"kind"}), required=frozenset({"kind"}))
        if step["binding"]["kind"] != "item_knowledge_check":
            _fail(f"{path}.kind", "must be 'item_knowledge_check'")
    elif step_type == StepType.EXPLAIN_BACK:
        if has:
            b = _dict(step["binding"], path, allowed=frozenset({"kind", "definition_key"}), required=frozenset({"kind", "definition_key"}))
            if b["kind"] != "assessment":
                _fail(f"{path}.kind", "must be 'assessment'")
            _text(b["definition_key"], f"{path}.definition_key", max_len=160)
            bound = spec.get("assessment_definition_key")
            if bound and b["definition_key"] != bound:
                _fail(f"{path}.definition_key", "must equal the Day's assessment_definition_key")
    elif step_type == StepType.LAB:
        if not has:
            _fail(path, "a lab step must bind to an existing execution engine")
        b = _dict(step["binding"], path, allowed=frozenset({"kind", "engine", "kit"}), required=frozenset({"kind", "engine"}))
        if "kit" in b:
            kit = _dict(b["kit"], f"{path}.kit", allowed=frozenset({"kit_key", "scenario_key"}), required=frozenset({"kit_key", "scenario_key"}))
            _id(kit["kit_key"], f"{path}.kit.kit_key")
            _id(kit["scenario_key"], f"{path}.kit.scenario_key")
            if b["engine"] != "personal_lab_experiment":
                _fail(f"{path}.engine", "a kit-bound lab runs on the Personal Lab engine ('personal_lab_experiment')")
        if b["kind"] != "personal_lab":
            _fail(f"{path}.kind", "must be 'personal_lab'")
        if b["engine"] not in LAB_ENGINES:
            _fail(f"{path}.engine", f"must be one of {', '.join(LAB_ENGINES)}")
        if spec.get("engine_binding") and spec["engine_binding"] != b["engine"]:
            _fail(f"{path}.engine", "must equal the Day's engine_binding")
    elif step_type == StepType.PRACTICE:
        if not has:
            _fail(path, "a practice step must bind to an Educational Lab Kit")
        b = _dict(step["binding"], path, allowed=frozenset({"kind", "kit_key", "scenario_keys"}), required=frozenset({"kind", "kit_key", "scenario_keys"}))
        if b["kind"] != "academy_lab_kit":
            _fail(f"{path}.kind", "must be 'academy_lab_kit'")
        _id(b["kit_key"], f"{path}.kit_key")
        keys = _list(b["scenario_keys"], f"{path}.scenario_keys", hi=6)
        for i, k in enumerate(keys):
            _id(k, f"{path}.scenario_keys[{i}]")
        if len(set(keys)) != len(keys):
            _fail(f"{path}.scenario_keys", "must not repeat a scenario")
    elif has:
        _fail(path, f"a {step_type.value} step must not carry a binding")


# -- public entry points ----------------------------------------------------------------------------------------------


def is_structured(spec: Any) -> bool:
    """True when a LearningItem spec opts into the structured contract. Existing items never do."""
    return isinstance(spec, dict) and ("steps" in spec or "step_schema_version" in spec)


def validate_structured_spec(spec: Any) -> List[dict]:
    """Validate a structured spec and return its steps. Raises ``StepContractError``; never mutates ``spec``."""
    spec = _dict(spec, "spec")
    if not is_structured(spec):
        _fail("spec", "is not a structured spec (no steps)")
    if spec.get("step_schema_version") != STEP_SCHEMA_VERSION or isinstance(spec.get("step_schema_version"), bool):
        _fail("spec.step_schema_version", f"must be {STEP_SCHEMA_VERSION}")
    if "steps" not in spec:
        _fail("spec.steps", "is required with step_schema_version")
    try:
        size = len(json.dumps(spec, sort_keys=True).encode("utf-8"))
    except (TypeError, ValueError):
        _fail("spec", "must be JSON-serialisable")
    if size > MAX_SPEC_BYTES:
        _fail("spec", f"is larger than {MAX_SPEC_BYTES} bytes")
    steps = _list(spec["steps"], "spec.steps", lo=1, hi=MAX_STEPS)

    keys: List[str] = []
    earlier_reflect: List[str] = []
    counts = {t: 0 for t in StepType}
    required_total = 0
    for i, step in enumerate(steps):
        path = f"steps[{i}]"
        step = _dict(step, path, allowed=STEP_KEYS, required=frozenset({"key", "type", "title", "estimated_minutes", "required", "content"}))
        key = step["key"]
        if not isinstance(key, str) or not KEY_RE.match(key) or not 2 <= len(key) <= 64:
            _fail(f"{path}.key", "must be a lowercase slug of 2 to 64 characters")
        if key in keys:
            _fail(f"{path}.key", f"duplicate step key {key!r}")
        keys.append(key)
        try:
            step_type = StepType(step["type"])
        except ValueError:
            _fail(f"{path}.type", f"must be one of {', '.join(t.value for t in StepType)}")
        _text(step["title"], f"{path}.title", max_len=160)
        _int(step["estimated_minutes"], f"{path}.estimated_minutes", lo=1, hi=180)
        if not isinstance(step["required"], bool):
            _fail(f"{path}.required", "must be true or false")
        _text(step.get("professor_hint"), f"{path}.professor_hint", max_len=500, optional=True)
        content = _dict(step["content"], f"{path}.content")
        _CONTENT[step_type](content, f"{path}.content", {"earlier_reflect_keys": list(earlier_reflect), "binding": step.get("binding")})
        _private(step_type, step, f"{path}.private")
        _binding(step_type, step, f"{path}.binding", spec)
        counts[step_type] += 1
        required_total += 1 if step["required"] else 0
        if step_type == StepType.REFLECT:
            earlier_reflect.append(key)

    if required_total < 1:
        _fail("spec.steps", "needs at least one required step")
    if counts[StepType.CHECK] > 1:
        _fail("spec.steps", "at most one check step per Day (it binds to the Day's knowledge check)")
    if counts[StepType.EXPLAIN_BACK] > 1:
        _fail("spec.steps", "at most one explain_back step per Day")
    if counts[StepType.CHECK] and not spec.get("knowledge_check"):
        _fail("spec.steps", "a check step needs the Day's knowledge_check questions")
    if counts[StepType.LAB] and spec.get("kind") not in (None, "lab"):
        _fail("spec.steps", "a lab step is only valid on a lab Day")
    return steps


def fingerprint(step: dict) -> str:
    """Stable digest of the learner-visible definition of a step. Ignores ``private``, so correcting an answer
    key does not silently invalidate progress; changing what the learner sees or must do does."""
    view = {k: step[k] for k in ("key", "type", "title", "required", "content", "binding") if k in step}
    return hashlib.sha256(json.dumps(view, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def public_steps(spec: Any) -> Optional[List[dict]]:
    """The learner-safe view: ordered steps with ``position`` and ``fingerprint``, and NO ``private`` content.
    Returns None for a non-structured item. Re-validates, so a malformed spec can never reach a client."""
    if not is_structured(spec):
        return None
    steps = validate_structured_spec(spec)
    out = []
    for position, step in enumerate(steps, start=1):
        view = {k: v for k, v in step.items() if k != "private"}
        view["position"] = position
        view["fingerprint"] = fingerprint(step)
        out.append(json.loads(json.dumps(view)))
    return out


def strip_private(spec: Any) -> Any:
    """Copy of ``spec`` safe to serialise to a client: structured steps lose ``private``; anything else is unchanged."""
    if not isinstance(spec, dict) or "steps" not in spec or not isinstance(spec["steps"], list):
        return spec
    clean = dict(spec)
    clean["steps"] = [{k: v for k, v in s.items() if k != "private"} if isinstance(s, dict) else s for s in spec["steps"]]
    return clean
