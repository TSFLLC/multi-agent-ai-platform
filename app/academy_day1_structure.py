"""AIL.5D.3 — the structural conversion of authored Day 1 into the structured Day/Step contract.

This is a MAPPING, not a rewrite. Every instructional sentence of Day 1 in
``docs/ail5-level1-practical-ai-foundations-curriculum-v2.md`` is carried into a step verbatim, in authored order:

* learner-visible text goes into the step's public ``content`` (markdown blocks, verbatim);
* authored reveals (the two "Think about it" explanations) go into the step's ``private`` section, served only after the
  learner commits an answer;
* the eight classification rationales become the existing ``knowledge_check[].explanation`` (revealed only after a pass);
* the four points of the learner explain-back prompt become the step's ``points``.

What is deliberately NOT carried into learner content, and why (the content-preservation test pins this list):

* stage directions addressed to the Professor/system, not to the learner (``STAGE_DIRECTIONS``);
* the Grader's reference points, the PASS / NEEDS_REVISION process and the "Evidence generated" list: they are AIL.5C
  assessment material (the rubric lives in the existing AIL.5C definition) or platform metadata, never learner content.

The converter fails loudly (``Day1StructureError``) if the authored document no longer has the expected shape, so a silent
content loss is impossible.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Tuple

CONVERTER = "ail5d3-day1-v1"

# Lines that direct the Professor or the system rather than address the learner.
STAGE_DIRECTIONS = (
    "*Professor opens with:*",
    "*[Learner writes a brief free-text response. This is not graded — it is a baseline capture that the learner will compare against their Day 1 exit activity.]*",
    "*The Professor presents this scenario:*",
    "*[Learner writes their answer. Professor then reveals and explains:]*",
    '*The Professor presents these three statements and asks the learner to mark each TRUE, FALSE, or "DEPENDS / NEEDS CONTEXT":*',
    "*[Learner writes their classifications and brief reasoning.]*",
    "*Professor reveals authoritative classifications:*",
    "*The Professor presents 8 brief scenarios. For each, the learner must:*",
)

# Learner-facing instructions that the step TYPE itself conveys; derived, not curriculum content.
DERIVED_TEXT = (
    'Mark each statement TRUE, FALSE, or "DEPENDS / NEEDS CONTEXT".',
    "For each of the 8 scenarios:",
)

# Sections of the authored Day that belong to AIL.5C, not to learner content.
EXCLUDED_ASSESSMENT_HEADINGS = ("**Authored reference points for the Grader Agent:**", "**Evidence generated:**")

ASSESSMENT_KEY = "level1-day-01-explain-ai"


class Day1StructureError(ValueError):
    """The authored Day 1 document does not have the shape this converter expects."""


# -- parsing helpers ---------------------------------------------------------------------------------------------------------


def day1_section(document: str) -> str:
    match = re.search(r"^### DAY 1\b.*?(?=^### DAY 2\b)", document, re.M | re.S)
    if not match:
        raise Day1StructureError("Day 1 section not found in the curriculum document")
    return match.group(0)


def _sections(section: str) -> Tuple[str, Dict[str, str]]:
    """(header text before the first timed heading, {heading text: body text})."""
    parts = re.split(r"^#### (.+)$", section, flags=re.M)
    header, body = parts[0], {}
    for heading, text in zip(parts[1::2], parts[2::2]):
        body[heading.strip()] = text
    return header, body


def _strip_rules(text: str) -> str:
    text = re.sub(r"^---\s*$", "", text, flags=re.M)
    return text.strip()


def _find(bodies: Dict[str, str], prefix: str) -> str:
    for heading, text in bodies.items():
        if heading.startswith(prefix):
            return _strip_rules(text)
    raise Day1StructureError(f"authored heading not found: {prefix!r}")


def _drop(text: str, lines: Tuple[str, ...]) -> str:
    kept = [ln for ln in text.split("\n") if ln.strip() not in lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _paragraphs(text: str) -> List[str]:
    """Blank-line separated paragraphs, never splitting inside a fenced code block."""
    paragraphs, current, fenced = [], [], False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            fenced = not fenced
        if not line.strip() and not fenced:
            if current:
                paragraphs.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def _blocks(text: str, *, limit: int = 3500) -> List[dict]:
    """Verbatim markdown blocks: a new block starts at a bold lead-in paragraph or when a block gets long."""
    blocks, current = [], []
    size = 0
    for paragraph in _paragraphs(text):
        starts_new = paragraph.startswith("**") and current
        if current and (starts_new or size + len(paragraph) > limit):
            blocks.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph)
    if current:
        blocks.append("\n\n".join(current))
    return [{"kind": "md", "text": b} for b in blocks]


def _step(key: str, type_: str, title: str, minutes: int, required: bool, content: dict, **extra: Any) -> dict:
    step = {"key": key, "type": type_, "title": title, "estimated_minutes": minutes, "required": required, "content": content}
    step.update(extra)
    return step


# -- the mapping -----------------------------------------------------------------------------------------------------------------


def build_day1_structure(document: str) -> Dict[str, Any]:
    """{"steps", "objectives", "explanations", "source_sha256"} for Day 1 from the authored curriculum document."""
    section = day1_section(document)
    header, bodies = _sections(section)

    # objectives + day overview (everything before the first timed heading, less the document title line)
    overview = _strip_rules("\n".join(header.split("\n")[1:]))
    objectives = re.findall(r"^- (.+)$", overview.split("**Prerequisites / review:**")[0], re.M)
    if len(objectives) != 6:
        raise Day1StructureError("expected six authored learning objectives")

    opening = _find(bodies, "0–5 min")
    what_is = _find(bodies, "5–12 min")
    not_one = _find(bodies, "12–20 min")
    traditional = _find(bodies, "20–28 min")
    realistic = _find(bodies, "28–36 min")
    isnt = _find(bodies, "36–44 min")
    cases = _find(bodies, "44–50 min")
    classify = _find(bodies, "50–55 min")
    explain = _find(bodies, "55–60 min")
    summary = _find(bodies, "Summary / Key Takeaways")
    vocabulary = _find(bodies, "Vocabulary")
    connection = _find(bodies, "Connection to Day 2")

    # -- opening -> baseline reflect (prompt) + closing line reused by the reflection step
    opening_clean = _drop(opening, STAGE_DIRECTIONS)
    quoted = [p for p in _paragraphs(opening_clean) if p.startswith('"')]
    closing = next((p for p in quoted if p.startswith('"We\'re going to come back')), None)
    if closing is None or len(quoted) < 2:
        raise Day1StructureError("opening prompt not found")
    prompt_paragraphs = [p for p in _paragraphs(opening_clean) if p != closing]
    baseline_prompt = "\n\n".join(prompt_paragraphs + [closing])

    # -- traditional vs AI -> teach (before the think) + think #1
    marker = '**"Think about it" #1:**'
    if marker not in traditional:
        raise Day1StructureError("Think about it #1 not found")
    before, after = traditional.split(marker, 1)
    scenario_quote = re.search(r'^"(A hospital wants.*?)"\s*$', after, re.M)
    reveal = after.split(STAGE_DIRECTIONS[3], 1)
    if not scenario_quote or len(reveal) != 2:
        raise Day1StructureError("Think about it #1 scenario or reveal not found")
    scenario_text = scenario_quote.group(1)
    split_at = scenario_text.index("Should this")
    scenario_md, question_md = scenario_text[:split_at].strip(), scenario_text[split_at:].strip()
    reveal_md = reveal[1].strip()

    # -- misconceptions -> teach + think #2
    marker2 = '**"Think about it" #2:**'
    if marker2 not in isnt:
        raise Day1StructureError("Think about it #2 not found")
    isnt_before, isnt_after = isnt.split(marker2, 1)
    statements_text, reveals_text = isnt_after.split(STAGE_DIRECTIONS[6], 1)
    statements = re.findall(r'^\d+\. "(.+)"\s*$', statements_text, re.M)
    claim_reveals = re.findall(r"^(\d+)\. (DEPENDS / NEEDS CONTEXT|TRUE|FALSE)\. (.+)$", reveals_text, re.M)
    if len(statements) != 3 or len(claim_reveals) != 3:
        raise Day1StructureError("expected three statements with three authored classifications")
    claim_answers = {"DEPENDS / NEEDS CONTEXT": "depends", "TRUE": "true", "FALSE": "false"}
    statement_items = [{"id": f"claim-{i}", "text": t} for i, t in enumerate(statements, start=1)]
    claims = {f"claim-{n}": {"answer": claim_answers[label], "why_md": why} for n, label, why in claim_reveals}

    # -- real-world case studies -> example
    case_parts = re.split(r"^\*\*Case study ([A-Z]) — (.+)\*\*\s*$", cases, flags=re.M)
    case_items = []
    for letter, title, body in zip(case_parts[1::3], case_parts[2::3], case_parts[3::3]):
        case_items.append({"tag": f"Case study {letter}", "title": title.strip(), "body_md": body.strip()})
    if len(case_items) != 2:
        raise Day1StructureError("expected two authored case studies")

    # -- classification -> check (intro) + explanations for the existing knowledge check
    intro_lines = [ln for ln in classify.split("\n") if ln.startswith("*(a)") or ln.startswith("*(b)")]
    if len(intro_lines) != 2:
        raise Day1StructureError("classification instructions not found")
    check_intro = DERIVED_TEXT[1] + "\n\n" + "\n".join(intro_lines)
    explanations = {}
    for number, rationale in re.findall(r'^\*\*Scenario (\d+):\*\*[^\n]*\n→ (.+)$', classify, re.M):
        explanations[int(number)] = rationale.strip()
    if sorted(explanations) != list(range(1, 9)):
        raise Day1StructureError("expected eight scenario rationales")

    # -- explain-back -> explain_back (learner prompt only; rubric and process are AIL.5C's)
    learner_prompt = explain.split("**Learner prompt:**", 1)[1].split(EXCLUDED_ASSESSMENT_HEADINGS[0], 1)[0].strip()
    prompt_head, rest = learner_prompt.split("Your explanation should:", 1)
    point_texts = re.findall(r"^\d+\. (.+)$", rest, re.M)
    closing_paragraph = rest.split(point_texts[-1], 1)[1].strip() if point_texts else ""
    if len(point_texts) != 4 or not closing_paragraph:
        raise Day1StructureError("expected four authored explain-back points")
    disclosure = explain.split("\n", 1)[0].strip()
    if not disclosure.startswith("*This is a genuine free-text activity"):
        raise Day1StructureError("explain-back disclosure not found")
    point_keys = ("what-ai-is", "realistic-capability", "limitation-or-overclaim", "rule-based-better")
    explain_prompt = "\n\n".join([disclosure, prompt_head.strip(), closing_paragraph])

    # -- summary / vocabulary / connection -> one required reading step
    rows = re.findall(r"^\| (?!Term)(?!-)(.+?) \| (.+?) \|$", vocabulary, re.M)
    if len(rows) != 8:
        raise Day1StructureError("expected eight vocabulary rows")
    takeaway_blocks = (
        [{"kind": "heading", "text": "Summary / Key Takeaways"}, {"kind": "md", "text": summary},
         {"kind": "heading", "text": "Vocabulary"}, {"kind": "compare", "head": ["Term", "Plain-language definition"], "rows": [list(r) for r in rows]},
         {"kind": "heading", "text": "Connection to Day 2"}, {"kind": "md", "text": connection}]
    )

    steps = [
        _step("day-overview", "teach", "What you'll learn today", 2, True, {"blocks": _blocks(overview)}),
        _step("baseline", "reflect", "You already use AI", 5, False, {"prompt_md": baseline_prompt, "min_chars": 8}),
        _step("what-is-ai", "teach", "What exactly is AI?", 7, True, {"blocks": _blocks(what_is)}),
        _step("not-one-technology", "teach", "AI is not one technology", 8, True, {"blocks": _blocks(not_one)}),
        _step("traditional-vs-ai", "teach", "Traditional software vs. AI", 5, True, {"blocks": _blocks(before.strip())}),
        _step("think-traditional-or-ai", "think", 'Think about it #1', 3, True, {"scenario_md": scenario_md, "question_md": question_md}, private={"reveal_md": reveal_md}),
        _step("what-ai-can-do", "teach", "What AI can realistically do", 8, True, {"blocks": _blocks(realistic)}),
        _step("what-ai-isnt", "teach", "What AI isn't — common misconceptions", 5, True, {"blocks": _blocks(isnt_before.strip())}),
        _step("think-three-claims", "think", 'Think about it #2', 3, True, {"scenario_md": DERIVED_TEXT[0], "statements": statement_items}, private={"claims": claims}),
        _step("real-world-cases", "example", "Real-world case studies", 6, True, {"cases": case_items}),
        _step("ai-or-not-ai", "check", "AI or not AI? Capable or overclaimed?", 5, True, {"intro_md": check_intro}, binding={"kind": "item_knowledge_check"}),
        _step("explain-ai", "explain_back", "Explain AI in your own words", 5, False,
              {"prompt_md": explain_prompt, "points": [{"key": k, "label": t} for k, t in zip(point_keys, point_texts)]},
              binding={"kind": "assessment", "definition_key": ASSESSMENT_KEY}),
        _step("takeaways", "teach", "Summary, vocabulary and what's next", 3, True, {"blocks": takeaway_blocks}),
        _step("compare-answers", "reflection", "How has your answer changed?", 2, False, {"prompt_md": closing, "compare_to": "baseline"}),
    ]
    return {
        "steps": steps, "objectives": objectives, "explanations": explanations,
        "source_sha256": hashlib.sha256(section.encode("utf-8")).hexdigest(),
    }


# -- the legacy (non-structured) view of the same public content, so the existing Day page keeps working and never leaks ---------


def legacy_body_md(steps: List[dict], objectives: List[str]) -> str:
    """A markdown body built ONLY from public step content (no reveals, no rubric), for the pre-Workspace Day page."""
    out: List[str] = []
    for step in steps:
        out.append(f"#### {step['title']}")
        c = step["content"]
        t = step["type"]
        if t == "teach":
            for b in c["blocks"]:
                if b["kind"] == "heading":
                    out.append(f"##### {b['text']}")
                elif b["kind"] == "compare":
                    out.append("\n".join(["| " + " | ".join(b["head"]) + " |", "|---|---|"] + ["| " + " | ".join(r) + " |" for r in b["rows"]]))
                else:
                    out.append(b["text"])
        elif t == "example":
            for case in c["cases"]:
                out.append(f"**{case['tag']} — {case['title']}**\n\n{case['body_md']}")
        elif t in ("reflect", "reflection", "explain_back"):
            out.append(c["prompt_md"])
            if t == "explain_back":
                out.append("\n".join(f"{i}. {p['label']}" for i, p in enumerate(c["points"], start=1)))
        elif t == "think":
            out.append(c["scenario_md"])
            out.append(c["question_md"] if "question_md" in c else "\n".join(f"{i}. {s['text']}" for i, s in enumerate(c["statements"], start=1)))
        elif t == "check":
            out.append(c["intro_md"])
        elif t == "lab":
            out.append(c["problem_md"])
        elif t == "practice":
            out.append(c["prompt_md"])
    return "\n\n".join(out)
