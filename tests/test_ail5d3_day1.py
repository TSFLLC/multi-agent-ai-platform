"""AIL.5D.3 — Day 1 as the reference structured Day.

Day 1 is converted by an explicit, versioned authoring step from the authored curriculum document. These tests prove the
conversion is lossless (nothing instructional is silently dropped), that private material stays private, that the legacy
version is preserved, and that the whole Day works end to end through the server-verified workflow.
"""

import json
import re

import pytest

from app.academy_day1_structure import (
    ASSESSMENT_KEY, DERIVED_TEXT, EXCLUDED_ASSESSMENT_HEADINGS, STAGE_DIRECTIONS, build_day1_structure, day1_section, legacy_body_md,
)
from app.academy_steps import is_structured, public_steps, validate_structured_spec
from app.assessment_curriculum import LEVEL1_EXPLAIN_BACK_ASSESSMENTS
from app.db.enums import ConceptKind, ConceptLevel, EvidenceType, LearningItemType
from app.errors import ConflictError
from app.models.academy import AcademyStepResponse
from app.models.concepts import LearningItem
from app.models.learner import LearningEvidence
from app.services.academy_level1_service import (
    AcademyLevel1Service, CANONICAL_SLUGS, EXISTING_ASSESSMENT_BINDINGS, _authored_knowledge_checks, _curriculum_text, _day_body,
)
from app.services.academy_structured_authoring import author_day_structure
from app.services.concept_graph_service import ConceptGraphService
from tests.ail1a_factories import make_concept, make_published_version, make_user

L1 = "/academy/level-1"
EXPECTED_MAPPING = [
    ("day-overview", "teach", True), ("baseline", "reflect", False), ("what-is-ai", "teach", True), ("not-one-technology", "teach", True),
    ("traditional-vs-ai", "teach", True), ("think-traditional-or-ai", "think", True), ("what-ai-can-do", "teach", True),
    ("what-ai-isnt", "teach", True), ("think-three-claims", "think", True), ("real-world-cases", "example", True),
    ("ai-or-not-ai", "check", True), ("explain-ai", "explain_back", False), ("takeaways", "teach", True), ("compare-answers", "reflection", False),
]


def _legacy_day1(db):
    """Day 1 exactly as routine provisioning creates it today: a non-structured Learning Item."""
    concept = make_concept(db, slug="what-ai-is-and-isnt", name="What AI Is and Isn't")
    make_published_version(db, concept=concept)
    spec = {
        "academy_key": "level1-v2-day-1", "curriculum": "ail5-level1-practical-ai-foundations-v2", "day": 1, "week": 1, "kind": "lecture",
        "objectives": ["Complete the authored Day 1 learning activity and produce its evidence."],
        "sections": ["objectives", "prerequisite", "teaching_or_setup", "guided_practice", "knowledge_check", "explain_back", "recap", "evidence", "next_activity"],
        "execution": "academy_lecture", "capstone_stage": None, "engine_binding": None,
        "explain_back": {"required": True, "assessment_definition_key": ASSESSMENT_KEY, "binding_status": "bound"},
        "assessment_definition_key": ASSESSMENT_KEY, "capability_boundary": None,
        "body_source": "docs/ail5-level1-practical-ai-foundations-curriculum-v2.md", "knowledge_check": _authored_knowledge_checks(1),
    }
    return ConceptGraphService(db).create_learning_item(
        concept_id=concept.id, item_type=LearningItemType.RESOURCE, title="Day 1: What AI Is and Isn't", body_md=_day_body(1), spec=spec, reviewed=True, est_minutes=60,
    )


@pytest.fixture()
def legacy(db, bootstrap):
    return _legacy_day1(db)


@pytest.fixture()
def day1(db, legacy):
    author_day_structure(db, 1)
    return next(r for r in AcademyLevel1Service(db)._current_academy_items() if r.spec["day"] == 1)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _norm(text):
    return re.sub(r"\s+", " ", text).strip()


# -- the explicit, versioned conversion -------------------------------------------------------------------------------------------------


def test_authoring_publishes_the_next_version_and_preserves_the_legacy_version(db, legacy):
    before = json.dumps({"spec": legacy.spec, "body": legacy.body_md, "title": legacy.title, "v": legacy.version}, sort_keys=True)
    result = author_day_structure(db, 1)
    assert result["authored"] is True and result["version"] == legacy.version + 1 and result["previous_item_id"] == legacy.id and result["steps"] == 14
    new = db.get(LearningItem, result["item_id"])
    assert new.lineage_id == legacy.lineage_id and new.version == 2 and is_structured(new.spec) and new.spec["academy_key"] == "level1-v2-day-1"
    db.refresh(legacy)
    assert json.dumps({"spec": legacy.spec, "body": legacy.body_md, "title": legacy.title, "v": legacy.version}, sort_keys=True) == before  # history untouched
    assert not is_structured(legacy.spec)


def test_authoring_is_idempotent_and_only_day_1_has_a_mapping(db, legacy):
    first = author_day_structure(db, 1)
    second = author_day_structure(db, 1)
    assert second["authored"] is False and second["item_id"] == first["item_id"] and db.query(LearningItem).filter_by(lineage_id=legacy.lineage_id).count() == 2
    for day in (2, 3, 30):
        with pytest.raises(ConflictError) as err:
            author_day_structure(db, day)
        assert err.value.detail["code"] == "not_authored"


def test_the_day_1_step_mapping_is_exactly_the_documented_one(day1):
    steps = validate_structured_spec(day1.spec)
    assert [(s["key"], s["type"], s["required"]) for s in steps] == EXPECTED_MAPPING
    assert sum(1 for s in steps if s["required"]) == 11 and sum(1 for s in steps if not s["required"]) == 3
    assert day1.spec["objectives"][0] == "Describe AI as a broad category of technologies, not a single thing" and len(day1.spec["objectives"]) == 6
    by_key = {s["key"]: s for s in steps}
    assert by_key["explain-ai"]["binding"] == {"kind": "assessment", "definition_key": ASSESSMENT_KEY} == {"kind": "assessment", "definition_key": EXISTING_ASSESSMENT_BINDINGS[1]}
    assert by_key["ai-or-not-ai"]["binding"] == {"kind": "item_knowledge_check"} and len(day1.spec["knowledge_check"]) == 8
    assert day1.spec["assessment_definition_key"] == ASSESSMENT_KEY and day1.spec["authoring"]["converter"]


def test_the_contract_and_converter_are_deterministic():
    document = _curriculum_text()
    a, b = build_day1_structure(document), build_day1_structure(document)
    assert a == b and a["source_sha256"] == b["source_sha256"]


# -- content preservation: nothing instructional is silently dropped ----------------------------------------------------------------------


def _collect(spec):
    return _norm("\n".join(_strings({"steps": spec["steps"], "kc": [{"prompt": q["prompt"], "explanation": q["explanation"]} for q in spec["knowledge_check"]], "o": spec["objectives"]})))


def _uncovered(spec):
    """Authored Day 1 lines that appear nowhere in the converted spec and are not explicitly excluded."""
    section = day1_section(_curriculum_text())
    blob = _collect(spec)
    lines = [ln.rstrip() for ln in section.split("\n")]
    # the explicitly excluded AIL.5C block: from the Grader reference points to the end of the explain-back section
    start = next(i for i, ln in enumerate(lines) if ln.strip() == EXCLUDED_ASSESSMENT_HEADINGS[0])
    end = next(i for i, ln in enumerate(lines) if i > start and ln.strip() == "---")
    excluded_block = {i for i in range(start, end)}
    connective = {"**Learner prompt:**", "Your explanation should:"}
    uncovered = []
    for i, line in enumerate(lines):
        text = line.strip()
        if (not text or text == "---" or text.startswith("#") or i in excluded_block or text in STAGE_DIRECTIONS or text in connective
                or re.fullmatch(r"\|[-| ]+\|", text)):
            continue
        norm = _norm(text)
        unnumbered = re.sub(r"^\d+\. ", "", norm)
        candidates = [norm, unnumbered, unnumbered.strip('"'), norm.lstrip("→ ").strip(), norm.strip('"'), norm.replace(" Your explanation should:", ""), text.strip("*:").replace('"', "")]
        ok = any(c and c in blob for c in candidates)
        if not ok and text.startswith("|"):
            cells = [c.strip() for c in text.strip("|").split("|")]
            ok = all(_norm(c) in blob for c in cells)
        if not ok and text.startswith("**Scenario"):
            ok = _norm(re.search(r'"(.+)"', text).group(1)) in blob
        if not ok and text.startswith("**Case study"):
            tag, title = re.match(r"\*\*(Case study [A-Z]) — (.+)\*\*", text).groups()
            ok = _norm(tag) in blob and _norm(title) in blob
        if not ok and re.match(r"^\d+\. (DEPENDS / NEEDS CONTEXT|TRUE|FALSE)\. ", text):
            label, why = re.match(r"^\d+\. (DEPENDS / NEEDS CONTEXT|TRUE|FALSE)\. (.+)$", text).groups()
            ok = _norm(why) in blob
        if not ok and text.startswith('"A hospital'):
            q = json.dumps(next(s for s in spec["steps"] if s["key"] == "think-traditional-or-ai")["content"])
            ok = all(part in q for part in ("A hospital wants", "Should this be traditional software or AI? Why?"))
        if not ok:
            uncovered.append(text)
    return uncovered


def test_the_conversion_is_lossless_every_authored_line_is_carried_or_explicitly_excluded(day1):
    assert _uncovered(day1.spec) == []


def test_the_preservation_check_really_detects_dropped_material(day1):
    import copy

    for key, mutate in (
        ("what-ai-isnt", lambda c: c["blocks"].pop(2)),                                  # a misconception paragraph
        ("real-world-cases", lambda c: c["cases"].pop()),                                # a whole case study
        ("takeaways", lambda c: c["blocks"][3]["rows"].pop()),                           # a vocabulary row
        ("explain-ai", lambda c: c["points"].pop()),                                     # an explain-back point
    ):
        spec = copy.deepcopy(day1.spec)
        mutate(next(s for s in spec["steps"] if s["key"] == key)["content"])
        assert _uncovered(spec), key
    spec = copy.deepcopy(day1.spec)
    spec["knowledge_check"][0]["explanation"] = "changed"                                  # a classification rationale
    assert _uncovered(spec)
    spec = copy.deepcopy(day1.spec)
    next(s for s in spec["steps"] if s["key"] == "think-traditional-or-ai")["private"]["reveal_md"] = "changed"   # an authored reveal
    assert _uncovered(spec)


def test_only_stage_directions_and_ail5c_material_are_excluded_from_learner_content(day1):
    learner_visible = _norm("\n".join(_strings({"steps": public_steps(day1.spec), "body": day1.body_md, "o": day1.spec["objectives"]})))
    for direction in STAGE_DIRECTIONS:
        assert _norm(direction) not in learner_visible, direction
    for phrase in ("Authored reference points for the Grader Agent", "A satisfactory response will include", "INSUFFICIENT",
                   "PASS / NEEDS_REVISION", "Evidence generated", "rubric graded by Grader Agent"):
        assert phrase not in learner_visible, phrase
    # derived text is the only learner text that is not from the document, and it is minimal and explicit
    assert set(DERIVED_TEXT) == {'Mark each statement TRUE, FALSE, or "DEPENDS / NEEDS CONTEXT".', "For each of the 8 scenarios:"}


def test_the_ail5c_rubric_points_never_appear_in_learner_content(day1):
    rubric = LEVEL1_EXPLAIN_BACK_ASSESSMENTS[0]
    assert rubric["key"] == ASSESSMENT_KEY
    learner_visible = _norm("\n".join(_strings({"steps": public_steps(day1.spec), "body": day1.body_md})))
    for point in rubric["points"]:
        assert _norm(point) not in learner_visible


# -- private material stays private -----------------------------------------------------------------------------------------------------------


def test_reveals_rationales_and_the_rubric_reach_no_learner_surface_before_the_learner_earns_them(client, auth_headers, day1):
    secrets = [
        'An AI that "usually" triggers the reminder',                               # think #1 reveal
        "Confidence of output is not correlated with accuracy",                     # think #2 claim reveal
        "Scoring well on a standardized test is not the same as reliable legal advice",
        'calling a timer "AI" is meaningless',                                      # classification rationale
        "A satisfactory response will include",                                     # AIL.5C rubric
    ]
    surfaces = [
        client.get(f"{L1}/days/1/learning", headers=auth_headers).text,
        client.get(f"{L1}/days/1", headers=auth_headers).text,                      # the pre-Workspace Day endpoint: spec + body_md
        client.get(f"{L1}/items/{day1.id}/steps", headers=auth_headers).text,
        client.get(f"{L1}/days", headers=auth_headers).text,
    ]
    for surface in surfaces:
        for secret in secrets:
            assert secret not in surface, secret
    assert '"private"' not in "".join(surfaces) and "reveal_md" not in "".join(surfaces)


def test_the_legacy_body_of_a_converted_day_is_built_from_public_content_only(day1):
    body = day1.body_md
    assert body == legacy_body_md(public_steps(day1.spec), day1.spec["objectives"])
    assert "What exactly is AI?" in body and "Should this be traditional software or AI? Why?" in body
    assert "An AI that \"usually\" triggers" not in body and "Traditional software. The rule is exact" not in body
    # the pre-conversion body leaked the reveals and the rubric; the converted one does not
    assert "Traditional software. The rule is exact" in _day_body(1) and "A satisfactory response will include" in _day_body(1)


# -- the whole Day, end to end through the server-verified workflow ------------------------------------------------------------------------------


def _answers(day1):
    return {q["id"]: q["answer"] for q in day1.spec["knowledge_check"]}


def test_day_1_end_to_end(client, auth_headers, db, bootstrap, day1):
    def step_url(key, tail):
        return f"{L1}/items/{day1.id}/steps/{key}/{tail}"

    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    assert view["structured"] and view["version"] == 2 and [s["key"] for s in view["steps"]] == [m[0] for m in EXPECTED_MAPPING]
    assert view["current_step_key"] == "day-overview" and view["learning_complete"] is False and view["demonstrated"] is False
    assert (view["required_total"], view["optional_total"]) == (11, 3)
    # opening the Day and every step completes nothing and writes no evidence
    client.post(f"{L1}/items/{day1.id}/open", headers=auth_headers)
    for step in view["steps"]:
        client.post(step_url(step["key"], "open"), headers=auth_headers)
    assert db.query(LearningEvidence).count() == 0

    # optional baseline, skipped-or-answered, then the reading steps
    assert client.post(step_url("what-is-ai", "skip"), headers=auth_headers).status_code == 409        # required
    assert client.put(step_url("baseline", "response"), json={"text": "AI is when computers think like humans."}, headers=auth_headers).status_code == 200
    assert client.post(step_url("baseline", "complete"), headers=auth_headers).status_code == 200
    for key in ("day-overview", "what-is-ai", "not-one-technology", "traditional-vs-ai"):
        assert client.post(step_url(key, "complete"), headers=auth_headers).status_code == 200

    # Think #1: commit, then reveal
    assert client.post(step_url("think-traditional-or-ai", "reveal"), headers=auth_headers).status_code == 409
    assert client.put(step_url("think-traditional-or-ai", "response"), json={"text": "Traditional software: the rule is exact and safety matters."}, headers=auth_headers).status_code == 200
    reveal = client.post(step_url("think-traditional-or-ai", "reveal"), headers=auth_headers).json()
    assert reveal["reveal_md"].startswith("Traditional software. The rule is exact")
    assert client.post(step_url("think-traditional-or-ai", "complete"), headers=auth_headers).status_code == 200
    for key in ("what-ai-can-do", "what-ai-isnt"):
        assert client.post(step_url(key, "complete"), headers=auth_headers).status_code == 200

    # Think #2: three statements
    claims = {"claim-1": {"choice": "depends"}, "claim-2": {"choice": "false", "reasoning": "confidence is not accuracy"}, "claim-3": {"choice": "depends"}}
    assert client.put(step_url("think-three-claims", "response"), json={"statements": claims}, headers=auth_headers).status_code == 200
    revealed = client.post(step_url("think-three-claims", "reveal"), headers=auth_headers).json()["claims"]
    assert [revealed[k]["matched"] for k in ("claim-1", "claim-2", "claim-3")] == [True, True, True]
    assert client.post(step_url("think-three-claims", "complete"), headers=auth_headers).status_code == 200
    assert client.post(step_url("real-world-cases", "complete"), headers=auth_headers).status_code == 200

    # Knowledge check through the EXISTING endpoint; complete only from its evidence
    assert client.post(step_url("ai-or-not-ai", "complete"), headers=auth_headers).status_code == 409
    wrong = {k: {"system": "ai", "claim": "realistic"} for k in _answers(day1)}
    failed = client.post(f"{L1}/items/{day1.id}/knowledge-check", json={"answers": wrong}, headers=auth_headers).json()
    assert failed["passed"] is False and all("explanation" not in r for r in failed["results"])
    passed = client.post(f"{L1}/items/{day1.id}/knowledge-check", json={"answers": _answers(day1)}, headers=auth_headers).json()
    assert passed["passed"] is True and len(passed["results"]) == 8 and "timer" in passed["results"][0]["explanation"]
    assert client.post(step_url("ai-or-not-ai", "complete"), headers=auth_headers).status_code == 200

    # one required step left (takeaways): the Day is not learning-complete yet, and no lesson_completed exists
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LESSON_COMPLETED).count() == 0
    assert client.post(step_url("takeaways", "complete"), headers=auth_headers).status_code == 200
    view = client.get(f"{L1}/days/1/learning", headers=auth_headers).json()
    assert view["learning_complete"] is True and view["demonstrated"] is False and view["required_completed"] == 11
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LESSON_COMPLETED).count() == 1
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.KNOWLEDGE_CHECK).count() == 2   # the failed and the passed submission
    # the optional explain-back and reflection remain available; the explain-back only points at AIL.5C
    assert view["current_step_key"] == "explain-ai"
    explain = next(s for s in view["steps"] if s["key"] == "explain-ai")
    assert explain["binding"]["definition_key"] == ASSESSMENT_KEY and explain["required"] is False
    assert db.query(LearningEvidence).filter(LearningEvidence.evidence_type.in_([EvidenceType.EXPLAIN_BACK])).count() == 0
    # completing Day 1's learning never demonstrates it: that needs AIL.5C (explain-back) plus the passed check
    assert next(d for d in client.get(f"{L1}/days", headers=auth_headers).json() if d["day"] == 1)["demonstrated"] is False


def test_routine_reprovisioning_leaves_the_structured_day_untouched(db, bootstrap):
    graph = ConceptGraphService(db)
    for slug in CANONICAL_SLUGS:
        concept = graph.create_concept(slug=slug, name=slug, level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept=concept)
    user = make_user(db)
    service = AcademyLevel1Service(db)
    service.provision(user)
    result = author_day_structure(db, 1)
    day1 = db.get(LearningItem, result["item_id"])
    snapshot = json.dumps({"id": day1.id, "spec": day1.spec, "body": day1.body_md, "title": day1.title}, sort_keys=True)
    service.provision(user)
    service.provision(user)
    db.refresh(day1)
    assert json.dumps({"id": day1.id, "spec": day1.spec, "body": day1.body_md, "title": day1.title}, sort_keys=True) == snapshot
    current = next(r for r in service._current_academy_items() if r.spec["day"] == 1)
    assert current.id == day1.id and is_structured(current.spec)
    assert all(not is_structured(r.spec) for r in service._current_academy_items() if r.spec["day"] != 1)   # Days 2-30 are untouched
    # the published program schedules the structured version of Day 1
    from app.models.academy import AcademyProgramItem
    assert db.query(AcademyProgramItem).filter_by(learning_item_id=day1.id).count() == 1
    assert db.query(AcademyProgramItem).filter_by(learning_item_id=result["previous_item_id"]).count() == 0


def test_converting_day_1_writes_no_learner_data(db, legacy, bootstrap):
    author_day_structure(db, 1)
    assert db.query(AcademyStepResponse).count() == 0 and db.query(LearningEvidence).count() == 0
