"""AIL.5D.6 — the structural conversion of Days 2-20 is a MAPPING: deterministic, content-preserving, private where it must be.

Day 1 has its own reference conversion (test_ail5d3_day1). These tests pin the generic converter over the authored document:
every authored line reaches a step verbatim (except the documented exclusions), reveals and guidance stay private, existing
knowledge checks / AIL.5C bindings / Lab Kits are bound rather than re-implemented, and re-authoring is idempotent."""

import json
import re

import pytest

from app.academy_day_structure import (
    DERIVED_TEXT, EXCLUDED_HEADINGS, KIT_PREDICTION_DAYS, REVEAL_HEAD_RE, STAGE_DIRECTION_RE, THINK_HEAD_RE, DayStructureError, build_day_structure,
    day_section, split_sections,
)
from app.academy_lab_kits import DAY_KITS, KITS
from app.academy_steps import StepType, is_structured, public_steps, validate_structured_spec
from app.db.enums import ConceptKind, ConceptLevel
from app.models.concepts import LearningItem
from app.services.academy_level1_service import AcademyLevel1Service, CANONICAL_SLUGS, EXISTING_ASSESSMENT_BINDINGS, _curriculum_text
from app.services.academy_structured_authoring import AUTHORED_DAYS, author_all_days, author_day_structure
from app.services.concept_graph_service import ConceptGraphService
from tests.ail1a_factories import make_published_version, make_user

DAYS = range(2, 21)
LAB_DAYS = (4, 5, 9, 10, 14, 15, 19, 20)
ENGINE = {4: "personal_lab_experiment", 5: "personal_lab_experiment", 9: "personal_lab_experiment", 10: "agent_version", 14: "agent_version",
          15: "bounded_manual_grounding", 19: "ma7_workflow", 20: "evaluation_or_personal_lab"}
DOC = _curriculum_text()
norm = lambda t: re.sub(r"\s+", " ", t).strip()  # noqa: E731


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def _spec(day, built):
    """The Day spec as provisioning creates it, plus the built steps."""
    lab = day in LAB_DAYS
    spec = {"academy_key": f"level1-v2-day-{day}", "day": day, "week": (day - 1) // 5 + 1, "kind": "lab" if lab else "lecture",
            "engine_binding": ENGINE.get(day), "assessment_definition_key": EXISTING_ASSESSMENT_BINDINGS.get(day),
            "knowledge_check": [{"id": "q"}] if any(s["type"] == "check" for s in built["steps"]) else [],
            "step_schema_version": 1, "steps": built["steps"]}
    return spec


# -- pure: the mapping ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("day", DAYS)
def test_every_authored_line_of_the_day_reaches_a_step_verbatim_except_the_documented_exclusions(day):
    built = build_day_structure(DOC, day, {"engine_binding": ENGINE.get(day)})
    blob = norm(" ".join(_strings(built["steps"])))
    _title, header, sections = split_sections(day_section(DOC, day))
    lines = list(header.split("\n"))
    for heading, body in sections:
        low = heading.lower()
        if any(low.startswith(x.lower()) for x in EXCLUDED_HEADINGS) or low.startswith("knowledge check"):
            continue
        if low.startswith("prediction") and day in KIT_PREDICTION_DAYS:
            continue                                  # carried by the Lab Kit (pinned verbatim below)
        lines += body.split("\n")
    missing = []
    for line in lines:
        t = line.strip()
        if not t or t == "---" or STAGE_DIRECTION_RE.match(t) or THINK_HEAD_RE.match(t) or REVEAL_HEAD_RE.match(t):
            continue
        if t.startswith("|"):
            cells = [c.strip() for c in t.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells) or all(norm(c) in blob for c in cells if c):
                continue
        if norm(t) not in blob:
            missing.append(t[:100])
    assert missing == [], f"Day {day} dropped authored content: {missing[:5]}"


@pytest.mark.parametrize("day", DAYS)
def test_the_mapping_is_deterministic_and_forms_a_valid_structured_day(day):
    a = build_day_structure(DOC, day, {"engine_binding": ENGINE.get(day)})
    b = build_day_structure(DOC, day, {"engine_binding": ENGINE.get(day)})
    assert a == b and len(a["source_sha256"]) == 64
    steps = validate_structured_spec(_spec(day, a))
    keys = [s["key"] for s in steps]
    assert len(keys) == len(set(keys)) and sum(1 for s in steps if s["required"]) >= 1
    assert len(a["objectives"]) >= 3


@pytest.mark.parametrize("day", DAYS)
def test_private_material_never_reaches_the_public_view_and_a_think_needs_its_authored_reveal(day):
    built = build_day_structure(DOC, day, {"engine_binding": ENGINE.get(day)})
    spec = _spec(day, built)
    public = json.dumps(public_steps(spec))
    for step in built["steps"]:
        if step["type"] == "think":
            assert step["private"]["reveal_md"].strip() and step["private"]["reveal_md"] not in public
            assert step["content"]["question_md"] == DERIVED_TEXT["think"]
        else:
            assert "private" not in step
    assert '"private"' not in public and "reveal_md" not in public


def test_the_authored_think_about_its_are_found_where_the_document_has_them():
    thinks = {d: [s for s in build_day_structure(DOC, d, {"engine_binding": ENGINE.get(d)})["steps"] if s["type"] == "think"] for d in DAYS}
    assert {d for d, t in thinks.items() if t} == {2, 3, 11}
    assert "Partially" in thinks[2][0]["private"]["reveal_md"] and "No single right answer" in thinks[3][0]["private"]["reveal_md"]
    assert "Expected components" in thinks[11][0]["private"]["reveal_md"]


@pytest.mark.parametrize("day", [d for d in DAYS if d not in LAB_DAYS])
def test_a_lecture_keeps_the_authored_sequence_and_binds_the_existing_knowledge_check(day):
    steps = build_day_structure(DOC, day, {})["steps"]
    types = [s["type"] for s in steps]
    assert types[0] == "teach" and steps[0]["title"] == "What you'll learn today"
    assert types.count("check") == 1 and steps[types.index("check")]["binding"] == {"kind": "item_knowledge_check"}
    assert steps[-1]["title"] == "Vocabulary and what's next" and any(b["kind"] == "compare" for b in steps[-1]["content"]["blocks"])
    assert not any(t in ("lab", "practice") for t in types)               # no artificial labs where the curriculum has no experiment
    titles = [s["title"] for s in steps]
    assert "Opening hook" in titles


@pytest.mark.parametrize("day", LAB_DAYS)
def test_a_lab_day_is_learn_run_explain_with_its_authored_bindings(day):
    steps = build_day_structure(DOC, day, {"engine_binding": ENGINE[day], "assessment_definition_key": EXISTING_ASSESSMENT_BINDINGS.get(day)})["steps"]
    by = {s["type"]: s for s in steps}
    lab = by["lab"]
    assert lab["binding"]["engine"] == ENGINE[day]
    explain = by["explain_back"]
    assert explain["binding"] == {"kind": "assessment", "definition_key": EXISTING_ASSESSMENT_BINDINGS[day]}
    assert [s["type"] for s in steps].index("lab") > 0 and "check" not in by
    if day in DAY_KITS:
        assert lab["binding"]["kit"] == {"kit_key": DAY_KITS[day][0], "scenario_key": DAY_KITS[day][1]} and lab["required"] is True
        practice = by["practice"]
        assert practice["binding"]["kit_key"] == DAY_KITS[day][0] and practice["required"] is False
        assert all(KITS[DAY_KITS[day][0]]["lab_kit"]["scenarios"][i]["mode"] for i in range(2))
    else:
        assert "practice" not in by and "kit" not in lab["binding"] and lab["required"] is False   # an engine launcher, not a kit


@pytest.mark.parametrize("day", KIT_PREDICTION_DAYS)
def test_the_lab_kit_carries_the_authored_predictions_verbatim(day):
    _title, _header, sections = split_sections(day_section(DOC, day))
    body = next(b for h, b in sections if h.lower().startswith("prediction"))
    unquote = lambda t: t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t  # noqa: E731
    authored = [norm(unquote(m.strip())) for m in re.findall(r"^\d+\.\s+(.+)$", body, re.M)]
    kit = KITS[DAY_KITS[day][0]]["lab_kit"]
    guided = next(s for s in kit["scenarios"] if s["mode"] == "guided")
    assert [norm(p["text"]) for p in guided["predictions"]] == authored


def test_the_converter_fails_loudly_rather_than_dropping_content():
    with pytest.raises(DayStructureError):
        build_day_structure(DOC, 1, {})                       # Day 1 has its own reference conversion
    with pytest.raises(DayStructureError):
        build_day_structure("### DAY 2 — LECTURE: X\n\n**Learning objectives:**\n- a\n", 2, {})
    with pytest.raises(DayStructureError):
        build_day_structure(DOC.replace("### DAY 4 — LAB:", "### DAY 4 — LECTURE:"), 4, {})


# -- database: authoring through the real service ---------------------------------------------------------------------------------------------------


@pytest.fixture()
def level1(db):
    graph = ConceptGraphService(db)
    for slug in CANONICAL_SLUGS:
        concept = graph.create_concept(slug=slug, name=slug, level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept=concept)
    service = AcademyLevel1Service(db)
    service.provision(make_user(db))
    return service


def test_authoring_every_mapped_day_publishes_immutable_versions_keeps_the_legacy_ones_and_is_idempotent(db, level1):
    before = {i.spec["day"]: i for i in level1._current_academy_items()}
    results = author_all_days(db)
    assert set(results) == set(AUTHORED_DAYS) and all(r["authored"] for r in results.values())
    after = {i.spec["day"]: i for i in level1._current_academy_items()}
    for day in AUTHORED_DAYS:
        assert is_structured(after[day].spec) and after[day].lineage_id == before[day].lineage_id and after[day].version == before[day].version + 1
        assert not is_structured(db.get(LearningItem, before[day].id).spec)                 # the legacy version is untouched history
        assert after[day].spec["authoring"]["source_sha256"] and "reveal_md" not in json.dumps(public_steps(after[day].spec))
        assert after[day].spec["knowledge_check"] == before[day].spec["knowledge_check"] or day == 1
    again = author_all_days(db)
    assert not any(r["authored"] for r in again.values())
    assert {i.spec["day"]: i.id for i in level1._current_academy_items()} == {d: i.id for d, i in after.items()}
    assert not is_structured(after.get(21).spec if 21 in after else {})                      # the Capstone is not converted here


def test_the_learning_view_of_a_converted_lab_day_offers_the_kit_lab_and_never_a_lab_answer(client, auth_headers, db, level1, bootstrap):
    author_all_days(db)
    for day, kit in DAY_KITS.items():
        body = client.get(f"/academy/level-1/days/{day}/learning", headers=auth_headers).json()
        assert body["structured"] is True
        lab = next(s for s in body["steps"] if s["type"] == "lab")
        assert lab["binding"]["kit"]["kit_key"] == kit[0] and lab["practice"]["instances"] == []
        assert any(s["type"] == "practice" for s in body["steps"])
        text = json.dumps(body)
        assert "reveal_md" not in text and "authored_check_md" not in text
    day10 = client.get("/academy/level-1/days/10/learning", headers=auth_headers).json()
    assert next(s for s in day10["steps"] if s["type"] == "lab")["binding"]["engine"] == "agent_version"
    assert day10["assessment"]["definition_key"] == EXISTING_ASSESSMENT_BINDINGS[10] or day10["assessment"] is None


def test_a_lecture_day_never_completes_by_being_opened(client, auth_headers, db, level1, bootstrap):
    author_day_structure(db, 2)
    body = client.get("/academy/level-1/days/2/learning", headers=auth_headers).json()
    item_id = body["item_id"]
    assert client.post(f"/academy/level-1/items/{item_id}/open", headers=auth_headers).json()["evidence_recorded"] is False
    after = client.get("/academy/level-1/days/2/learning", headers=auth_headers).json()
    assert after["learning_complete"] is False and after["demonstrated"] is False and after["required_completed"] == 0
