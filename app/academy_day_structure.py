"""AIL.5D.6 — the structural conversion of authored Days 2-20 into the structured Day/Step contract.

A MAPPING, not a rewrite (the same rule as Day 1, ``app.academy_day1_structure``). Every instructional line of a Day in
``docs/ail5-level1-practical-ai-foundations-curriculum-v2.md`` is carried VERBATIM, in authored order, into a step; nothing is
paraphrased or generated. The converter is deterministic (same document -> same steps) and fails loudly
(``DayStructureError``) when a Day no longer has the shape it expects, so content can never be dropped silently.

How an authored section maps (pedagogy follows the authored sequence):

Lecture Days   header -> overview (teach) | Opening hook -> teach | Core section N -> teach (+ ``think`` for an authored
               "Think about it", whose authored reveal or guidance is PRIVATE and served only after the learner commits) |
               Guided practice -> teach | Real-world case study -> example | Knowledge check -> ``check`` bound to the Day's
               existing knowledge check | Vocabulary + Connection -> one reading step (the vocabulary table stays structured).
Lab Days       Problem / Objective / Prior knowledge -> teach | Setup -> teach | PREDICTION -> the Lab Kit's predictions (the
               authored text, verbatim) | experiment / build / phases -> teach | analysis / failure / modification -> teach |
               the experiment itself -> a ``lab`` step (kit-bound Guided Lab for Days 4, 5 and 9; an engine launcher for the
               build labs) + an independent ``practice`` step where a Lab Kit exists | EXPLAIN-BACK / REFLECTION ->
               ``explain_back`` bound to the Day's existing AIL.5C assessment | Completion criteria -> teach | Optional stretch
               challenge -> an optional ``reflect``.

What is deliberately NOT carried into learner content (``EXCLUDED``; the content-preservation test pins this list):
  * "Evidence generated" and "Evidence requirements" - platform / AIL.5C metadata, never learner content;
  * "Knowledge check" question text - it already lives in the Day's existing knowledge check (``spec.knowledge_check``), which
    the ``check`` step is bound to;
  * stage directions addressed to the Professor/system rather than the learner.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from app.academy_lab_kits import DAY_KITS, get_kit
from app.academy_day1_structure import _blocks, _paragraphs, _step, _strip_rules

CONVERTER = "ail5d6-days-v1"
FIRST_DAY, LAST_DAY = 2, 20
LAB_DAY_NUMBERS = (4, 5, 9, 10, 14, 15, 19, 20)

EXCLUDED_HEADINGS = ("Evidence generated", "Evidence requirements")
# Learner-facing text the step TYPE itself conveys; derived, not curriculum content.
DERIVED_TEXT = {
    "check": "Answer each question, then check your answers. They are checked on the server, and you may try again.",
    "think": "Write your answer and your reasoning.",
}
STAGE_DIRECTION_RE = re.compile(r"^\*\[Learner writes[^\]]*\]\*\s*$")
THINK_HEAD_RE = re.compile(r'^\*\*"?Think about it"?[^*\n]*\*\*\s*$')
REVEAL_HEAD_RE = re.compile(r"^\*Professor reveals[^*\n]*\*\s*$")
KIT_PREDICTION_DAYS = (4, 5, 9)


class DayStructureError(ValueError):
    """The authored Day does not have the shape this converter expects."""


# -- parsing -----------------------------------------------------------------------------------------------------------------


def day_section(document: str, day: int) -> str:
    match = re.search(rf"^### DAY {day}\b.*?(?=^### DAY {day + 1}\b|^## )", document, re.M | re.S)
    if not match:
        raise DayStructureError(f"Day {day} section not found in the curriculum document")
    return match.group(0)


def split_sections(section: str) -> Tuple[str, str, List[Tuple[str, str]]]:
    """(title line, header text before the first #### heading, [(heading, body)] in authored order)."""
    parts = re.split(r"^#### (.+)$", section, flags=re.M)
    head_lines = parts[0].split("\n")
    title, header = head_lines[0], "\n".join(head_lines[1:])
    return title, _strip_rules(header), [(h.strip(), _strip_rules(b)) for h, b in zip(parts[1::2], parts[2::2])]


def _slug(text: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-") or "step"
    key, n = base, 2
    while key in taken or len(key) < 2:
        key = f"{base}-{n}"
        n += 1
    taken.add(key)
    return key


def _title(heading: str) -> str:
    """'Core section 2 (20–32 min) | How an LLM works' -> 'How an LLM works'; 'Opening hook (0–5 min)' -> 'Opening hook'."""
    text = heading.split("|", 1)[1].strip() if "|" in heading else re.sub(r"\s*\([^)]*\)\s*$", "", heading).strip()
    return text[:160]


def _minutes(heading: str, default: int) -> int:
    match = re.search(r"\((\d+)\s*[–-]\s*(\d+)\s*min", heading)
    if match:
        return max(1, min(180, int(match.group(2)) - int(match.group(1))))
    match = re.search(r"\((\d+)\s*minutes?\)", heading)
    return max(1, min(180, int(match.group(1)))) if match else default


def _teach(key: str, title: str, minutes: int, text: str, required: bool = True) -> dict:
    return _step(key, "teach", title, minutes, required, {"blocks": _blocks(text)})


def _split_think(text: str) -> List[Tuple[str, Any]]:
    """Split a section body into ('text', md) and ('think', {...}) parts, in authored order. Verbatim; nothing is dropped."""
    # The authored markers are often glued to the next line; give each its own paragraph (no text changes, only blank lines).
    marker = lambda ln: THINK_HEAD_RE.match(ln.strip()) or STAGE_DIRECTION_RE.match(ln.strip()) or REVEAL_HEAD_RE.match(ln.strip())  # noqa: E731
    spaced = "\n".join(f"\n{ln}\n" if marker(ln) else ln for ln in text.split("\n"))
    paragraphs = _paragraphs(spaced)
    parts: List[Tuple[str, Any]] = []
    buffer: List[str] = []
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        if not THINK_HEAD_RE.match(p.strip()):
            buffer.append(p)
            i += 1
            continue
        if buffer:
            parts.append(("text", "\n\n".join(buffer)))
            buffer = []
        i += 1
        question: List[str] = []
        while i < len(paragraphs) and not STAGE_DIRECTION_RE.match(paragraphs[i].strip()) and not REVEAL_HEAD_RE.match(paragraphs[i].strip()):
            question.append(paragraphs[i])
            i += 1
        guidance = ""
        if i < len(paragraphs) and STAGE_DIRECTION_RE.match(paragraphs[i].strip()):
            direction = paragraphs[i].strip()[2:-2]                     # inside "*[ ... ]*"
            sentences = direction.split(". ", 1)
            guidance = sentences[1].strip() if len(sentences) == 2 else ""
            i += 1
        reveal: List[str] = []
        if i < len(paragraphs) and REVEAL_HEAD_RE.match(paragraphs[i].strip()):
            i += 1
            while i < len(paragraphs) and not (paragraphs[i].startswith("**") and reveal and not paragraphs[i].startswith("**\"")):
                reveal.append(paragraphs[i])
                i += 1
        reveal_md = "\n\n".join(reveal).strip() or guidance
        if not question or not reveal_md:
            raise DayStructureError("a Think about it needs an authored question and an authored reveal or guidance")
        parts.append(("think", {"question": "\n\n".join(question).strip(), "reveal": reveal_md, "heading": p.strip().strip("*").strip()}))
    if buffer:
        parts.append(("text", "\n\n".join(buffer)))
    return parts


def _table_rows(text: str) -> Optional[List[List[str]]]:
    rows = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells) or (cells and cells[0].lower() == "term"):
            continue
        rows.append(cells)
    return rows or None


def _vocabulary_blocks(vocabulary: str, summary: Optional[str], connection: Optional[str], connection_title: str) -> List[dict]:
    blocks: List[dict] = []
    if summary:
        blocks += [{"kind": "heading", "text": "Summary / Key Takeaways"}, *_blocks(summary)]
    rows = _table_rows(vocabulary)
    if rows and all(len(r) == 2 for r in rows) and len(rows) <= 30 and all(len(c) <= 1000 for r in rows for c in r):
        blocks += [{"kind": "heading", "text": "Vocabulary"}, {"kind": "compare", "head": ["Term", "Plain-language definition"], "rows": rows}]
        remainder = "\n".join(ln for ln in vocabulary.split("\n") if not ln.strip().startswith("|")).strip()
        if remainder:
            blocks += _blocks(remainder)
    else:
        blocks += [{"kind": "heading", "text": "Vocabulary"}, *_blocks(vocabulary)]
    if connection:
        blocks += [{"kind": "heading", "text": connection_title}, *_blocks(connection)]
    return blocks


# -- lecture Days ---------------------------------------------------------------------------------------------------------------


def _lecture(day: int, header: str, sections: List[Tuple[str, str]]) -> Tuple[List[dict], List[str]]:
    steps: List[dict] = []
    taken: set = set()
    objectives = re.findall(r"^- (.+)$", header.split("**Prerequisites")[0], re.M)
    if len(objectives) < 3:
        raise DayStructureError(f"Day {day}: expected the authored learning objectives")
    steps.append(_teach(_slug("overview", taken), "What you'll learn today", 2, header))
    vocabulary = summary = connection = None
    connection_title = "What's next"
    for heading, body in sections:
        low = heading.lower()
        if any(low.startswith(x.lower()) for x in EXCLUDED_HEADINGS):
            continue
        if low.startswith("knowledge check"):
            if "**KC-" not in body:
                raise DayStructureError(f"Day {day}: knowledge check questions not found")
            steps.append(_step(_slug("knowledge-check", taken), "check", "Knowledge check", 5, True, {"intro_md": DERIVED_TEXT["check"]}, binding={"kind": "item_knowledge_check"}))
            continue
        if low.startswith("vocabulary"):
            vocabulary = body
            continue
        if low.startswith("summary"):
            summary = body
            continue
        if low.startswith("connection"):
            connection, connection_title = body, heading
            continue
        if low.startswith("real-world case study"):
            steps.append(_step(_slug("real-world-case-study", taken), "example", "Real-world case study", _minutes(heading, 5), True, {"cases": [{"tag": "Case study", "title": "Real-world case study", "body_md": body}]}))
            continue
        title = _title(heading)
        minutes = _minutes(heading, 5)
        if low.startswith("opening hook"):
            title = "Opening hook"
        parts = _split_think(body)
        n_think = sum(1 for kind, _ in parts if kind == "think")
        for index, (kind, value) in enumerate(parts):
            if kind == "text":
                suffix = "" if index == 0 else " (continued)"
                steps.append(_teach(_slug(title + suffix, taken), title + suffix, minutes if not n_think else max(2, minutes // 2), value))
            else:
                steps.append(_step(_slug("think-" + title, taken), "think", "Think about it", 3, True,
                                   {"scenario_md": value["question"], "question_md": DERIVED_TEXT["think"]}, private={"reveal_md": value["reveal"]}))
    if vocabulary is None:
        raise DayStructureError(f"Day {day}: vocabulary not found")
    steps.append(_step(_slug("vocabulary-and-whats-next", taken), "teach", "Vocabulary and what's next", 3, True,
                       {"blocks": _vocabulary_blocks(vocabulary, summary, connection, connection_title)}))
    return steps, objectives


# -- lab Days --------------------------------------------------------------------------------------------------------------------


def _numbered_points(text: str) -> List[str]:
    return re.findall(r"^\d+\.\s+(.+)$", text, re.M)


def _predictions_from(text: str) -> List[str]:
    """The authored predictions, verbatim: numbered items, or the sentences the learner completes."""
    items = [i.strip() for i in re.findall(r"^\d+\.\s+(.+)$", text, re.M)]
    if not items:
        raise DayStructureError("authored predictions not found")
    return items


def _lab(day: int, header: str, sections: List[Tuple[str, str]], spec: dict) -> Tuple[List[dict], List[str]]:
    steps: List[dict] = []
    taken: set = set()
    by = {h.split("(")[0].strip().lower(): (h, b) for h, b in sections}

    def get(prefix: str) -> Optional[Tuple[str, str]]:
        for key, value in by.items():
            if key.startswith(prefix.lower()):
                return value
        return None

    objective = get("learning objective")
    if objective is None:
        raise DayStructureError(f"Day {day}: learning objective not found")
    objectives = re.findall(r"^- (.+)$", objective[1], re.M)
    if len(objectives) < 3:
        raise DayStructureError(f"Day {day}: expected the authored learning objectives")
    intro = [header]
    for prefix in ("problem", "learning objective", "required prior knowledge", "materials"):
        found = get(prefix)
        if found:
            intro.append(f"**{found[0].split('(')[0].strip()}**\n\n{found[1]}")
    steps.append(_teach(_slug("the-problem", taken), "The problem and what you'll do", 5, "\n\n".join(p for p in intro if p.strip())))

    handled = {"problem", "learning objective", "required prior knowledge", "materials", "evidence requirements"}
    kit_day = day in DAY_KITS
    explain = get("explain-back")
    stretch = get("optional stretch")
    completion = get("completion criteria")
    # The experiment / build material, in authored order, as reading steps (verbatim). A kit-backed Day carries its
    # PREDICTION into the Lab Kit (checked verbatim by the content-preservation test) instead of a reading step.
    for heading, body in sections:
        name = heading.split("(")[0].strip().lower()
        if any(name.startswith(h) for h in handled) or name.startswith("explain-back") or name.startswith("optional stretch") or name.startswith("completion criteria"):
            continue
        if any(name.startswith(x.lower()) for x in EXCLUDED_HEADINGS):
            continue
        if name.startswith("prediction") and kit_day:
            continue
        title = _title(heading).title() if heading.isupper() or heading.split("(")[0].strip().isupper() else _title(heading)
        steps.append(_teach(_slug(title, taken), title[:160], _minutes(heading, 10), body))

    engine = spec.get("engine_binding")
    if kit_day:
        kit_key, scenario_key = DAY_KITS[day]
        kit = get_kit(kit_key)["lab_kit"]
        independent = [s["key"] for s in kit["scenarios"] if s["mode"] == "independent"]
        guided = next(s for s in kit["scenarios"] if s["key"] == scenario_key)
        steps.append(_step(_slug("guided-lab", taken), "lab", "Run the lab", 30, True,
                           {"problem_md": f"**{guided['title']}.** {guided['objective_md']}"},
                           binding={"kind": "personal_lab", "engine": "personal_lab_experiment", "kit": {"kit_key": kit_key, "scenario_key": scenario_key}}))
    else:
        steps.append(_step(_slug("build-it-in-the-lab", taken), "lab", "Build it in the lab", 30, False,
                           {"problem_md": "Open the lab for this Day. It runs on the existing platform engine; your work there is recorded by that engine, and you return here to reflect."},
                           binding={"kind": "personal_lab", "engine": engine}))
    if explain:
        points = _numbered_points(explain[1])
        if len(points) < 1:
            raise DayStructureError(f"Day {day}: explain-back questions not found")
        key = spec.get("assessment_definition_key")
        step = _step(_slug("explain-back", taken), "explain_back", "Explain what you found", 10, False,
                     {"prompt_md": explain[1], "points": [{"key": f"point-{i}", "label": (p if len(p) <= 200 else p[:197] + "…")} for i, p in enumerate(points[:8], start=1)]})
        if key:
            step["binding"] = {"kind": "assessment", "definition_key": key}
        steps.append(step)
    if kit_day:
        steps.append(_step(_slug("practice-this", taken), "practice", "Practice this concept", 15, False,
                           {"prompt_md": "Try it again on your own with a fresh case. Less guidance this time: you decide what to run, what to change, and what it tells you. Hints are available, retries are fine, and nothing here is graded."},
                           binding={"kind": "academy_lab_kit", "kit_key": DAY_KITS[day][0], "scenario_keys": independent}))
    if completion:
        steps.append(_teach(_slug("what-counts-as-done", taken), "What counts as done", 2, completion[1]))
    if stretch:
        steps.append(_step(_slug("stretch-challenge", taken), "reflect", "Stretch challenge", 15, False, {"prompt_md": stretch[1], "min_chars": 8}))
    return steps, objectives


# -- public API -------------------------------------------------------------------------------------------------------------------


def build_day_structure(document: str, day: int, spec: Optional[dict] = None) -> Dict[str, Any]:
    """{"steps", "objectives", "source_sha256"} for Day ``day`` (2-20) from the authored curriculum document."""
    if not FIRST_DAY <= day <= LAST_DAY:
        raise DayStructureError(f"Day {day} has no converter here")
    section = day_section(document, day)
    title, header, sections = split_sections(section)
    lab = "— LAB:" in title or "– LAB:" in title or " LAB:" in title
    if lab != (day in LAB_DAY_NUMBERS):
        raise DayStructureError(f"Day {day}: authored Day type does not match the curriculum map")
    steps, objectives = _lab(day, header, sections, spec or {}) if lab else _lecture(day, header, sections)
    return {"steps": steps, "objectives": objectives, "source_sha256": hashlib.sha256(section.encode("utf-8")).hexdigest(), "lab": lab}
