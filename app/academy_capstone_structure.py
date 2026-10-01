"""AIL.5D.6 — the structural conversion of the Capstone (Days 21-30) into the structured Day/Step contract.

The Capstone is ONE project built across ten Days, not ten weekly lessons:

    DEFINE -> DESIGN -> BUILD -> TEST -> DIAGNOSE -> IMPROVE -> EVALUATE -> EXPLAIN -> DEMONSTRATE -> REVIEW

so each Day is mapped (verbatim, deterministically, the same rule as Days 1-20) to:

* ``teach``   the Day's authored agenda (Day 21 also carries the authored Capstone overview and the four options);
* ``reflect`` "Your Day N artifact": the learner's own work product (problem statement, design, README, ...), saved privately
              and CARRIED FORWARD - later Days show the artifacts of earlier ones (``AcademyStepService.capstone_progress``);
* ``lab``     (Days 21-28, optional) a launcher into the existing Build With Me capstone project (``start-capstone``);
* ``explain_back`` (Day 28) the five authored explain-back questions as a private preparation outline.

Day 29 (the demonstration) and Day 30 (the review) keep their own existing AIL.5C / Professor-review flows: Day 29's learner
steps are the authored agenda, and the assessment is the existing AIL.5C definition bound to the Day.

NOT carried into learner content (``EXCLUDED``; pinned by the content-preservation test): the Grader's rubric for the Day 29
explain-back (AIL.5C assessment material, never learner content).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from app.academy_day1_structure import _blocks, _step, _strip_rules
from app.academy_day_structure import DayStructureError, _slug

CONVERTER = "ail5d6-capstone-v1"
FIRST_DAY, LAST_DAY = 21, 30
STAGES = {21: "DEFINE", 22: "DESIGN", 23: "BUILD", 24: "TEST", 25: "DIAGNOSE", 26: "IMPROVE", 27: "EVALUATE", 28: "EXPLAIN", 29: "DEMONSTRATE", 30: "REVIEW"}
EXCLUDED = {29: ("**Grader rubric for explain-back:**",)}
LAUNCH_DAYS = tuple(range(21, 29))
ARTIFACT_RE = re.compile(r"^\*\*Day (\d+) artifact:\*\*\s*(.+)$", re.M)


def artifact_key(day: int) -> str:
    return f"day-{day}-artifact"


def capstone_context(document: str) -> Tuple[str, str]:
    """(overview, options): the authored Capstone text that precedes Day 21."""
    match = re.search(r"^### CAPSTONE OVERVIEW\s*\n(.*?)^### CAPSTONE OPTIONS\s*\n(.*?)^### DAY 21\b", document, re.M | re.S)
    if not match:
        raise DayStructureError("the authored Capstone overview and options were not found")
    return _strip_rules(match.group(1)), _strip_rules(match.group(2))


def day_section(document: str, day: int) -> str:
    end = rf"^### DAY {day + 1}\b" if day < LAST_DAY else r"^## "
    match = re.search(rf"^### DAY {day}\b.*?(?={end})", document, re.M | re.S)
    if not match:
        raise DayStructureError(f"Day {day} section not found in the curriculum document")
    return match.group(0)


def _without_excluded(day: int, text: str) -> str:
    """Drop an excluded authored block: the bold heading and the pipe table that follows it. Fails loudly if the shape changed."""
    for heading in EXCLUDED.get(day, ()):
        start = text.find(heading)
        if start < 0:
            raise DayStructureError(f"Day {day}: expected the authored block {heading!r}")
        rest = text[start + len(heading):]
        match = re.match(r"\s*\n(?:\|.*\n?)+", rest)
        if not match:
            raise DayStructureError(f"Day {day}: the excluded block did not have the expected table shape")
        text = text[:start] + rest[match.end():]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _minutes(duration_line: str) -> int:
    numbers = [int(n) for n in re.findall(r"\d+", duration_line)]
    return max(numbers) if numbers else 60


def build_capstone_structure(document: str, day: int, spec: Optional[dict] = None) -> Dict[str, Any]:
    """{"steps", "objectives", "source_sha256", "artifact"} for Capstone Day ``day`` (21-30)."""
    if not FIRST_DAY <= day <= LAST_DAY:
        raise DayStructureError(f"Day {day} is not a Capstone Day")
    section = day_section(document, day)
    lines = section.split("\n")
    body = _strip_rules("\n".join(lines[1:]))
    duration = re.search(r"^\*\*Duration:\*\*\s*(.+)$", body, re.M)
    if duration is None:
        raise DayStructureError(f"Day {day}: duration not found")
    minutes = _minutes(duration.group(1))
    artifact = ARTIFACT_RE.search(body)
    if day < 29 and artifact is None:
        raise DayStructureError(f"Day {day}: the authored artifact line was not found")
    agenda = _without_excluded(day, body)
    stage = STAGES[day].title()
    taken: set = set()
    steps: List[dict] = []
    if day == 21:
        overview, options = capstone_context(document)
        steps.append(_step(_slug("capstone-overview", taken), "teach", "The Capstone: one project, ten Days", 5, True, {"blocks": _blocks(overview)}))
        steps.append(_step(_slug("capstone-options", taken), "teach", "The four Capstone options", 10, True, {"blocks": _blocks(options)}))
    steps.append(_step(_slug("todays-agenda", taken), "teach", f"Day {day} · {stage}: today's agenda", max(5, minutes // 3), True, {"blocks": _blocks(agenda)}))
    if day == 28:
        questions = re.findall(r'^\d+\.\s+("[^\n]+")\s*$', agenda, re.M)
        if len(questions) != 5:
            raise DayStructureError("Day 28: expected the five authored explain-back questions")
        steps.append(_step(_slug("explain-back-preparation", taken), "explain_back", "Prepare your five explain-back answers", 20, False,
                           {"prompt_md": "Prepare honest, specific answers to the five authored questions above. This outline is private; the graded explain-back is part of Day 29.",
                            "points": [{"key": f"question-{i}", "label": (q if len(q) <= 200 else q[:197] + "…")} for i, q in enumerate(questions, start=1)]}))
    if artifact is not None and day < 29:      # Days 29 and 30 produce a platform record (the assessment, the portfolio), not a learner write-up
        steps.append(_step(artifact_key(day), "reflect", f"Your Day {day} artifact", max(10, minutes // 2), True,
                           {"prompt_md": f"**Day {day} artifact:** {artifact.group(2).strip()}\n\nWrite it here. It is saved privately and carried forward to the Days after this one.", "min_chars": 20}))
    if day in LAUNCH_DAYS:
        steps.append(_step(_slug("your-capstone-project", taken), "lab", "Work in your Capstone project", minutes, False,
                           {"problem_md": "Open your Capstone project, the one project you are building across these ten Days. It runs on the existing Build With Me engine; come back here to record your artifact."},
                           binding={"kind": "personal_lab", "engine": "build_with_me_agent"}))
    return {"steps": steps, "source_sha256": hashlib.sha256(section.encode("utf-8")).hexdigest(), "artifact": artifact_key(day) if artifact is not None and day < 29 else None}
