"""AIL.5D.5 — the Educational Lab Kit contract (pure: no database, no provider).

An Educational Lab Kit is a reusable, versioned recipe for a hands-on experiment, stored as an ordinary immutable
``LearningItem`` (``spec_json.lab_kit``), so it is versioned exactly like every other Learning Item and a learner can never
mutate it. A kit holds authored *scenarios*; a learner works through a scenario in their own **Practice Instance**, which
runs on the existing governed execution engine (a Task Run per run). A kit is NEVER an execution engine.

    Lab Kit (immutable) + scenario (authored configuration)  ->  Practice Instance (the learner's own)  ->  governed Task Runs

What a scenario authors (everything a learner may and must do):

* the objective and instructions, the prompt template and the ONLY variables a learner may change (``text``, ``choice``,
  or an authored ``model`` choice) - there is no other way to alter what is run;
* the predictions required before the first run, the minimum number of runs, whether a change between runs, a comparison, an
  observation and a reflection are required (together: the completion conditions);
* the run cap and the permitted models;
* an optional private ``reveal_md`` (a lab answer) that is served only once the learner has earned it.

Two modes, both first-class: ``guided`` scenarios carry substantial structure (predictions, observation, comparison,
reflection); ``independent`` scenarios carry less guidance (no predictions, a lighter completion condition) and are meant for
"practice this concept again". Neither mode is an assessment: practice is safe to fail, repeatable, and never demonstrated
evidence (AIL.5C owns demonstration).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set

from app.db.enums import PracticeInstanceStatus as PracticeStatus

KIT_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VAR_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TOKEN_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
MAX_KIT_BYTES = 300_000
MAX_PROMPT_CHARS = 6000
MAX_RUNS_HARD_CAP = 10          # no scenario may authorise more runs than this per practice instance
MAX_TEXT = 20_000


_ORDER = [PracticeStatus.CREATED, PracticeStatus.PREDICTED, PracticeStatus.RUNNING, PracticeStatus.OBSERVING,
          PracticeStatus.COMPARING, PracticeStatus.REFLECTING, PracticeStatus.COMPLETED]


class KitContractError(ValueError):
    """The authored Lab Kit does not satisfy the contract. The message names the path."""


def _fail(path: str, message: str) -> None:
    raise KitContractError(f"{path}: {message}")


def _dict(value: Any, path: str, allowed: frozenset, required: frozenset = frozenset()) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    extra = sorted(set(value) - allowed)
    if extra:
        _fail(path, f"unknown field(s): {', '.join(extra)}")
    missing = sorted(required - set(value))
    if missing:
        _fail(path, f"missing field(s): {', '.join(missing)}")
    return value


def _text(value: Any, path: str, *, hi: int = MAX_TEXT, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")
    if len(value) > hi:
        _fail(path, f"must be at most {hi} characters")
    return value


def _int(value: Any, path: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        _fail(path, f"must be an integer between {lo} and {hi}")
    return value


def _slug(value: Any, path: str) -> str:
    if not isinstance(value, str) or not KIT_KEY_RE.match(value) or not 2 <= len(value) <= 80:
        _fail(path, "must be a lowercase slug of 2 to 80 characters")
    return value


SCENARIO_KEYS = frozenset({
    "key", "title", "mode", "objective_md", "instructions_md", "prompt_template", "variables", "predictions", "requirements",
    "limits", "allowed_models", "reveal_md", "observation_prompt_md", "comparison_prompt_md", "reflection_prompt_md",
})
REQUIREMENT_KEYS = frozenset({"min_runs", "require_change", "require_comparison", "require_observation", "require_reflection"})


def _variable(var: Any, path: str) -> dict:
    var = _dict(var, path, frozenset({"key", "label", "kind", "options", "max_chars", "default"}), frozenset({"key", "label", "kind"}))
    if not isinstance(var["key"], str) or not VAR_KEY_RE.match(var["key"]):
        _fail(f"{path}.key", "must be lowercase letters, digits and underscores, starting with a letter")
    _text(var["label"], f"{path}.label", hi=160)
    kind = var["kind"]
    if kind == "text":
        _int(var.get("max_chars", 0), f"{path}.max_chars", 20, 4000)
        _text(var.get("default"), f"{path}.default", hi=var["max_chars"], optional=True)
        if "options" in var:
            _fail(f"{path}.options", "is not allowed for a text variable")
    elif kind == "choice":
        options = var.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 12:
            _fail(f"{path}.options", "needs 2 to 12 authored options")
        keys = set()
        for i, option in enumerate(options):
            option = _dict(option, f"{path}.options[{i}]", frozenset({"key", "label", "text"}), frozenset({"key", "label", "text"}))
            _slug(option["key"], f"{path}.options[{i}].key")
            if option["key"] in keys:
                _fail(f"{path}.options[{i}].key", f"duplicate option {option['key']!r}")
            keys.add(option["key"])
            _text(option["label"], f"{path}.options[{i}].label", hi=200)
            _text(option["text"], f"{path}.options[{i}].text", hi=MAX_PROMPT_CHARS)
        if var.get("default") is not None and var["default"] not in keys:
            _fail(f"{path}.default", "must be one of the option keys")
        if "max_chars" in var:
            _fail(f"{path}.max_chars", "is not allowed for a choice variable")
    elif kind == "model":
        options = var.get("options")
        if not isinstance(options, list) or not 1 <= len(options) <= 6 or not all(isinstance(o, str) and o.strip() for o in options):
            _fail(f"{path}.options", "needs 1 to 6 canonical model ids")
        if var.get("default") is not None and var["default"] not in options:
            _fail(f"{path}.default", "must be one of the options")
    else:
        _fail(f"{path}.kind", "must be text, choice or model")
    return var


def _scenario(sc: Any, path: str) -> dict:
    sc = _dict(sc, path, SCENARIO_KEYS, frozenset({"key", "title", "mode", "objective_md", "instructions_md", "prompt_template", "requirements", "limits"}))
    _slug(sc["key"], f"{path}.key")
    _text(sc["title"], f"{path}.title", hi=200)
    if sc["mode"] not in ("guided", "independent"):
        _fail(f"{path}.mode", "must be guided or independent")
    _text(sc["objective_md"], f"{path}.objective_md")
    _text(sc["instructions_md"], f"{path}.instructions_md")
    template = _text(sc["prompt_template"], f"{path}.prompt_template", hi=MAX_PROMPT_CHARS)
    variables = sc.get("variables", [])
    if not isinstance(variables, list) or len(variables) > 6:
        _fail(f"{path}.variables", "must be a list of at most 6 variables")
    declared: Dict[str, dict] = {}
    for i, var in enumerate(variables):
        var = _variable(var, f"{path}.variables[{i}]")
        if var["key"] in declared:
            _fail(f"{path}.variables[{i}].key", f"duplicate variable {var['key']!r}")
        declared[var["key"]] = var
    used = set(TOKEN_RE.findall(template))
    prompt_vars = {k for k, v in declared.items() if v["kind"] != "model"}
    if used - set(declared):
        _fail(f"{path}.prompt_template", f"uses undeclared variable(s): {', '.join(sorted(used - set(declared)))}")
    if any(declared[k]["kind"] == "model" for k in used):
        _fail(f"{path}.prompt_template", "a model variable selects the model; it cannot appear in the prompt")
    if prompt_vars - used:
        _fail(f"{path}.variables", f"variable(s) never used by the prompt template: {', '.join(sorted(prompt_vars - used))}")
    if sum(1 for v in declared.values() if v["kind"] == "model") > 1:
        _fail(f"{path}.variables", "at most one model variable")

    predictions = sc.get("predictions", [])
    if not isinstance(predictions, list) or len(predictions) > 6:
        _fail(f"{path}.predictions", "must be a list of at most 6 predictions")
    seen: Set[str] = set()
    for i, p in enumerate(predictions):
        p = _dict(p, f"{path}.predictions[{i}]", frozenset({"id", "text"}), frozenset({"id", "text"}))
        _slug(p["id"], f"{path}.predictions[{i}].id")
        if len(p["id"]) > 24:
            _fail(f"{path}.predictions[{i}].id", "must be at most 24 characters")
        if p["id"] in seen:
            _fail(f"{path}.predictions[{i}].id", f"duplicate prediction id {p['id']!r}")
        seen.add(p["id"])
        _text(p["text"], f"{path}.predictions[{i}].text", hi=1000)

    req = _dict(sc["requirements"], f"{path}.requirements", REQUIREMENT_KEYS, frozenset({"min_runs"}))
    min_runs = _int(req["min_runs"], f"{path}.requirements.min_runs", 1, MAX_RUNS_HARD_CAP)
    for flag in REQUIREMENT_KEYS - {"min_runs"}:
        if flag in req and not isinstance(req[flag], bool):
            _fail(f"{path}.requirements.{flag}", "must be true or false")
    if (req.get("require_change") or req.get("require_comparison")) and min_runs < 2:
        _fail(f"{path}.requirements.min_runs", "a change or a comparison needs at least 2 runs")
    if req.get("require_change") and not declared:
        _fail(f"{path}.requirements.require_change", "needs at least one variable the learner may change")

    limits = _dict(sc["limits"], f"{path}.limits", frozenset({"max_runs"}), frozenset({"max_runs"}))
    max_runs = _int(limits["max_runs"], f"{path}.limits.max_runs", 1, MAX_RUNS_HARD_CAP)
    if max_runs < min_runs:
        _fail(f"{path}.limits.max_runs", "must allow at least min_runs")

    models = sc.get("allowed_models", [])
    if not isinstance(models, list) or len(models) > 6 or not all(isinstance(m, str) and m.strip() for m in models):
        _fail(f"{path}.allowed_models", "must be a list of at most 6 canonical model ids")
    for var in declared.values():
        if var["kind"] == "model" and models and not set(var["options"]) <= set(models):
            _fail(f"{path}.allowed_models", "must include every model the model variable offers")

    for field in ("reveal_md", "observation_prompt_md", "comparison_prompt_md", "reflection_prompt_md"):
        _text(sc.get(field), f"{path}.{field}", optional=True)

    if sc["mode"] == "guided":
        if not predictions:
            _fail(f"{path}.predictions", "a guided scenario requires at least one prediction before the first run")
        if not req.get("require_observation"):
            _fail(f"{path}.requirements.require_observation", "a guided scenario requires an observation")
    else:
        if predictions:
            _fail(f"{path}.predictions", "an independent scenario carries less guidance: no predictions")
        for field in ("observation_prompt_md", "comparison_prompt_md", "reflection_prompt_md"):
            if sc.get(field):
                _fail(f"{path}.{field}", "an independent scenario carries less guidance: no prompting questions")
    return sc


def validate_lab_kit(spec: Any) -> List[dict]:
    """Validate ``spec['lab_kit']`` and return its scenarios. Never mutates ``spec``."""
    spec = spec if isinstance(spec, dict) else {}
    if "lab_kit" not in spec:
        _fail("spec", "is not a lab kit spec")
    try:
        if len(json.dumps(spec, sort_keys=True).encode("utf-8")) > MAX_KIT_BYTES:
            _fail("spec", f"is larger than {MAX_KIT_BYTES} bytes")
    except (TypeError, ValueError):
        _fail("spec", "must be JSON-serialisable")
    kit = _dict(spec["lab_kit"], "lab_kit", frozenset({"key", "version", "title", "agent", "scenarios"}), frozenset({"key", "version", "title", "agent", "scenarios"}))
    _slug(kit["key"], "lab_kit.key")
    _int(kit["version"], "lab_kit.version", 1, 1000)
    if spec.get("kit_key") != kit["key"]:
        _fail("spec.kit_key", "must equal lab_kit.key")
    _text(kit["title"], "lab_kit.title", hi=200)
    agent = _dict(kit["agent"], "lab_kit.agent", frozenset({"key", "name", "role", "prompt"}), frozenset({"key", "name", "role", "prompt"}))
    _slug(agent["key"], "lab_kit.agent.key")
    _text(agent["name"], "lab_kit.agent.name", hi=200)
    _text(agent["role"], "lab_kit.agent.role", hi=120)
    _text(agent["prompt"], "lab_kit.agent.prompt", hi=4000)
    scenarios = kit["scenarios"]
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 20:
        _fail("lab_kit.scenarios", "needs 1 to 20 scenarios")
    keys: Set[str] = set()
    for i, sc in enumerate(scenarios):
        sc = _scenario(sc, f"lab_kit.scenarios[{i}]")
        if sc["key"] in keys:
            _fail(f"lab_kit.scenarios[{i}].key", f"duplicate scenario key {sc['key']!r}")
        keys.add(sc["key"])
    return scenarios


def is_lab_kit(spec: Any) -> bool:
    return isinstance(spec, dict) and "lab_kit" in spec


def scenario_of(spec: dict, scenario_key: str) -> Optional[dict]:
    return next((s for s in spec["lab_kit"]["scenarios"] if s["key"] == scenario_key), None)


def scenario_fingerprint(scenario: dict) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(scenario, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# -- what a learner may submit -----------------------------------------------------------------------------------------------------


class VariableError(ValueError):
    """The submitted variables are not what the scenario permits."""


def validate_variables(scenario: dict, values: Any) -> Dict[str, str]:
    """The variables of one run, validated against the scenario. Only declared variables are accepted; a choice must be one of
    the authored options; text is bounded. Missing variables take the authored default (and must have one)."""
    values = {} if values is None else values
    if not isinstance(values, dict):
        raise VariableError("variables must be an object")
    declared = {v["key"]: v for v in scenario.get("variables", [])}
    extra = sorted(set(values) - set(declared))
    if extra:
        raise VariableError(f"not a variable of this scenario: {', '.join(extra)}")
    out: Dict[str, str] = {}
    for key, var in declared.items():
        value = values.get(key, var.get("default"))
        if value is None:
            raise VariableError(f"a value for {var['label']!r} is required")
        if not isinstance(value, str):
            raise VariableError(f"{var['label']!r} must be text")
        if var["kind"] == "text":
            value = value.strip()
            if not value:
                raise VariableError(f"a value for {var['label']!r} is required")
            if len(value) > var["max_chars"]:
                raise VariableError(f"{var['label']!r} must be at most {var['max_chars']} characters")
        elif var["kind"] == "choice":
            if value not in {o["key"] for o in var["options"]}:
                raise VariableError(f"{var['label']!r} must be one of the offered options")
        else:  # model
            if value not in var["options"]:
                raise VariableError(f"{var['label']!r} must be one of the offered models")
        out[key] = value
    return out


def render_prompt(scenario: dict, variables: Dict[str, str]) -> str:
    """The exact prompt a run sends: the authored template with each variable replaced once (no recursion, no code)."""
    declared = {v["key"]: v for v in scenario.get("variables", [])}

    def replace(match: "re.Match[str]") -> str:
        var = declared[match.group(1)]
        value = variables[var["key"]]
        return next(o["text"] for o in var["options"] if o["key"] == value) if var["kind"] == "choice" else value

    prompt = TOKEN_RE.sub(replace, scenario["prompt_template"])
    if len(prompt) > MAX_PROMPT_CHARS:
        raise VariableError(f"the prompt is longer than {MAX_PROMPT_CHARS} characters")
    return prompt


def public_scenario(scenario: dict) -> dict:
    """The learner-visible scenario: everything but the private reveal (and the model options' internals stay as authored)."""
    return {k: v for k, v in scenario.items() if k != "reveal_md"}


# -- the lifecycle, derived from recorded facts -------------------------------------------------------------------------------------


def _changed(completed_runs: List[Dict[str, str]]) -> bool:
    return len(completed_runs) >= 2 and any(run != completed_runs[0] for run in completed_runs[1:])


def evaluate(scenario: dict, facts: Dict[str, Any]) -> Dict[str, Any]:
    """Which completion conditions are met, what is still required (in order), and the status the facts support.

    ``facts``: ``predictions`` (set of saved prediction ids), ``completed_runs`` (variables of each completed run, in order),
    ``runs_started`` (int), ``observation``/``comparison``/``reflection`` (bool: saved)."""
    req = scenario["requirements"]
    completed = facts.get("completed_runs", [])
    met = {
        "prediction": {p["id"] for p in scenario.get("predictions", [])} <= set(facts.get("predictions", ())),
        "runs": len(completed) >= req["min_runs"],
        "change": (not req.get("require_change")) or _changed(completed),
        "observation": (not req.get("require_observation")) or bool(facts.get("observation")),
        "comparison": (not req.get("require_comparison")) or (bool(facts.get("comparison")) and len(completed) >= 2),
        "reflection": (not req.get("require_reflection")) or bool(facts.get("reflection")),
    }
    order = ["prediction", "runs", "change", "observation", "comparison", "reflection"]
    next_required = [name for name in order if not met[name]]
    if not next_required:
        status = PracticeStatus.COMPLETED
    elif met["runs"] and met["change"] and met["observation"] and met["comparison"]:
        status = PracticeStatus.REFLECTING
    elif met["runs"] and met["change"]:
        status = PracticeStatus.COMPARING
    elif completed:
        status = PracticeStatus.OBSERVING
    elif facts.get("runs_started"):
        status = PracticeStatus.RUNNING
    elif scenario.get("predictions") and met["prediction"]:
        status = PracticeStatus.PREDICTED
    else:
        status = PracticeStatus.CREATED
    return {"met": met, "next_required": next_required, "status": status}


def advance(current: PracticeStatus, target: PracticeStatus) -> PracticeStatus:
    """Forward-only: a status never moves backwards, and ABANDONED/COMPLETED are final."""
    if current in (PracticeStatus.COMPLETED, PracticeStatus.ABANDONED):
        return current
    if target == PracticeStatus.ABANDONED:
        return target
    return target if _ORDER.index(target) > _ORDER.index(current) else current
