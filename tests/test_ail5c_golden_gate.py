"""AIL.5C Grader golden-set release gate (design section I).

A candidate Grader configuration is release-ready only if it matches every
expectation of the 2 clear-pass / 2 clear-fail / 2 borderline golden set of every
approved definition. The gate runs through the real packet builder, contract and
cross-check; here the "model" is a deterministic oracle or a deliberately bad
candidate, so the gate itself is what is under test.
"""

import hashlib
import io
import json

import pytest
from sqlalchemy import select

from app.assessment_curriculum import (
    FOUNDATION_ASSESSMENTS,
    LEVEL1_EXPLAIN_BACK_ASSESSMENTS,
    seed_foundation_assessments,
    seed_level1_explain_back_assessments,
)
from app.assessment_golden import (
    BORDERLINE,
    CLEAR_FAIL,
    CLEAR_PASS,
    GoldenSetIncomplete,
    approved_grader_definition_keys,
    golden_cases,
    run_golden_gate,
    validate_golden_set,
)
from app.db.enums import AgentRunRole
from app.models.assessment import AssessmentDefinition
from app.models.learner import LearningEvidence
from app.models.tasks import AgentRun
from app.providers.base import InvokeResponse
from app.services.assessment_definition_service import AssessmentDefinitionService
from app.services.build_with_me_service import ProjectTemplateService
from tests.ail1a_factories import make_concept, make_published_version
from tests.ail5c_factories import (
    _KEYS,
    _RESPONSE_DATA,
    ScriptedGrader,
    default_findings,
    explain_definition,
    judgment,
    make_ail_concept,
    setup_free_models,
)
from tests.test_ail5c_curriculum import _graph


class OracleGrader(ScriptedGrader):
    """A deterministic, well-behaved candidate: judges each criterion by how many of that
    criterion's authored reference points the response actually contains."""

    def __init__(self, points_by_key, confidence="high"):
        super().__init__()
        self.points_by_key = points_by_key
        self.confidence = confidence

    def invoke(self, request):
        self.requests.append(request)
        prompt = request.user_prompt
        response = "\n".join(_RESPONSE_DATA.findall(prompt))
        keys = list(dict.fromkeys(_KEYS.findall(prompt.split("<<<END CRITERIA>>>")[0])))
        rows = []
        for key in keys:
            points = self.points_by_key[key]
            hits = [p for p in points if p.lower() in response.lower()]
            finding = "met" if len(hits) == len(points) else "partial" if hits else "not_met"
            rows.append(judgment(key, hits[0] if hits else "", finding, self.confidence))
        return InvokeResponse(
            text=json.dumps({"criteria": rows}),
            tokens_in=100,
            tokens_out=50,
            tokens_total=150,
            latency_ms=2,
            provider_http_status=200,
            finish_reason="stop",
        )


def _points(definition):
    return {c["key"]: c["reference_points"] for c in definition.criteria if c["method"] == "grader"}


def _setup(db, bootstrap):
    setup_free_models(db, count=2)
    concept, _v = make_ail_concept(db)
    return explain_definition(db, bootstrap.user, concept), bootstrap.user


def _gate(db, user, adapter, defn, **kw):
    return run_golden_gate(
        db, user, adapter_factory=lambda _d, _p: adapter, required_keys=[defn.definition_key], **kw
    )


# -- the golden set itself -------------------------------------------------------------------------------------------


def test_the_golden_set_is_two_clear_pass_two_clear_fail_two_borderline_and_deterministic(db, bootstrap):
    defn, _user = _setup(db, bootstrap)
    cases = golden_cases(defn)
    assert [(c.klass, c.case_id) for c in cases] == [
        (CLEAR_PASS, "pass-1"),
        (CLEAR_PASS, "pass-2"),
        (CLEAR_FAIL, "fail-1"),
        (CLEAR_FAIL, "fail-2"),
        (BORDERLINE, "borderline-1"),
        (BORDERLINE, "borderline-2"),
    ]
    assert cases == golden_cases(defn), "derived from the authored reference points, never generated"
    assert all(c.origin == "derived_from_reference_points" for c in cases)
    points = [p for pts in _points(defn).values() for p in pts]
    assert all(p in cases[0].response for p in points)  # the clear pass covers every authored point
    assert not any(p in cases[2].response for p in points)  # the clear fail covers none
    # each borderline answer omits a criterion's ideas entirely
    assert cases[4].absent_criteria == ("limits",) and cases[5].absent_criteria == ("accuracy",)


def test_a_malformed_golden_set_is_rejected(db, bootstrap):
    from dataclasses import replace

    defn, _user = _setup(db, bootstrap)
    good = golden_cases(defn)
    with pytest.raises(GoldenSetIncomplete, match="2 clear pass"):
        validate_golden_set(defn, good[:5])
    with pytest.raises(GoldenSetIncomplete, match="unique"):
        validate_golden_set(defn, good[:5] + [replace(good[5], case_id="pass-1")])
    with pytest.raises(GoldenSetIncomplete, match="absent_criteria"):
        validate_golden_set(defn, good[:5] + [replace(good[5], absent_criteria=("nope",))])
    with pytest.raises(GoldenSetIncomplete, match="distinct"):
        validate_golden_set(defn, good[:5] + [replace(good[5], response=good[4].response)])
    with pytest.raises(GoldenSetIncomplete, match="empty"):
        validate_golden_set(defn, good[:5] + [replace(good[5], response="   ")])


def test_authored_golden_cases_take_precedence_and_are_validated(db, bootstrap):
    setup_free_models(db, count=2)
    concept, _v = make_ail_concept(db)
    defn = explain_definition(db, bootstrap.user, concept, key="eb-authored")
    kinds = [CLEAR_PASS, CLEAR_PASS, CLEAR_FAIL, CLEAR_FAIL, BORDERLINE, BORDERLINE]
    authored = {
        "eb-authored": [
            {"case_id": f"a-{i}", "klass": k, "response": f"authored {k} {i}"} for i, k in enumerate(kinds)
        ]
    }
    assert {c.origin for c in golden_cases(defn, authored)} == {"authored"}
    with pytest.raises(GoldenSetIncomplete):
        golden_cases(defn, {"eb-authored": authored["eb-authored"][:4]})


def test_a_definition_with_fewer_than_two_reference_points_needs_authored_cases():
    from types import SimpleNamespace

    thin = SimpleNamespace(
        definition_key="eb-thin",
        criteria=[{"key": "only", "method": "grader", "reference_points": ["just one authored point"]}],
    )
    with pytest.raises(GoldenSetIncomplete, match="at least two authored reference points"):
        golden_cases(thin)


# -- the gate ---------------------------------------------------------------------------------------------------------


def test_a_well_behaved_candidate_is_release_ready_and_runs_the_real_machinery(db, bootstrap):
    defn, user = _setup(db, bootstrap)
    adapter = OracleGrader(_points(defn))
    report = _gate(db, user, adapter, defn)
    assert report.release_ready, report.reasons
    assert len(report.cases) == 6 and all(c.passed for c in report.cases)
    assert report.coverage == {
        "required": [defn.definition_key],
        "covered": [defn.definition_key],
        "missing": [],
    }
    assert report.definitions[defn.definition_key]["release_ready"] is True

    # the real allowlisted packet and the real cross-check on a DIFFERENT model
    assert all("ACADEMY GRADER INPUT (grading_contract_v1)" in r.user_prompt for r in adapter.requests)
    assert len(adapter.requests) == 12  # 6 cases x (primary + cross-check)
    assert len({m for c in report.cases for m in c.models}) == 2
    runs = list(db.execute(select(AgentRun)).scalars())
    assert runs and all(r.role == AgentRunRole.GRADER for r in runs)

    # the candidate identity is recorded so a release decision is about something specific
    from app.services.assessment_grader_service import AssessmentGraderService

    body = report.to_json()
    assert (
        body["golden_set_version"] == "golden_set_v1"
        and body["grading_contract_version"] == "grading_contract_v1"
    )
    assert len(body["candidate"]["grader_agent_version_ids"]) == 1
    assert (
        body["candidate"]["prompt_sha256"]
        == hashlib.sha256(AssessmentGraderService._grader_prompt().encode()).hexdigest()
    )
    assert body["report_hash"] and len(body["report_hash"]) == 64


def test_the_gate_writes_no_evidence_and_derives_no_state(db, bootstrap):
    defn, user = _setup(db, bootstrap)
    _gate(db, user, OracleGrader(_points(defn)), defn)
    assert db.query(LearningEvidence).count() == 0


def test_a_lenient_candidate_that_passes_bad_answers_is_not_ready(db, bootstrap):
    defn, user = _setup(db, bootstrap)
    lenient = ScriptedGrader(lambda n, keys, quote: default_findings(keys, quote))  # everything "met"
    report = _gate(db, user, lenient, defn)
    assert report.release_ready is False
    failed = {c.case_id for c in report.cases if not c.passed}
    assert failed == {"fail-1", "fail-2", "borderline-1", "borderline-2"}
    assert any("expected not_met" in r for r in report.reasons)
    assert any("borderline" in r or "wholly absent" in r for r in report.reasons)


def test_a_harsh_candidate_that_fails_good_answers_is_not_ready(db, bootstrap):
    defn, user = _setup(db, bootstrap)
    harsh = ScriptedGrader(lambda n, keys, quote: default_findings(keys, quote, finding="not_met"))
    report = _gate(db, user, harsh, defn)
    assert report.release_ready is False
    assert {c.case_id for c in report.cases if not c.passed} == {"pass-1", "pass-2"}


def test_an_unsure_candidate_is_not_ready_on_clear_cases(db, bootstrap):
    defn, user = _setup(db, bootstrap)
    report = _gate(db, user, OracleGrader(_points(defn), confidence="low"), defn)
    assert report.release_ready is False
    assert any("low confidence" in r for r in report.reasons)


def test_a_candidate_that_breaks_the_grading_contract_is_not_ready(db, bootstrap):
    defn, user = _setup(db, bootstrap)
    broken = ScriptedGrader(lambda n, keys, quote: "this is not json at all")
    report = _gate(db, user, broken, defn)
    assert report.release_ready is False
    assert all(not c.passed and c.grading_status != "complete" for c in report.cases)
    assert any("did not complete cleanly" in r for r in report.reasons)


def test_a_provider_outage_is_not_a_pass(db, bootstrap):
    from app.providers.base import ProviderConnectionError

    defn, user = _setup(db, bootstrap)
    down = ScriptedGrader(lambda n, keys, quote: ProviderConnectionError("unreachable"))
    assert _gate(db, user, down, defn).release_ready is False


def test_a_missing_approved_definition_makes_the_run_not_ready(db, bootstrap):
    defn, user = _setup(db, bootstrap)
    adapter = OracleGrader(_points(defn))
    report = run_golden_gate(
        db,
        user,
        adapter_factory=lambda _d, _p: adapter,
        required_keys=[defn.definition_key, "an-approved-definition-that-is-not-published"],
    )
    assert report.release_ready is False
    assert report.coverage["missing"] == ["an-approved-definition-that-is-not-published"]
    assert any("no published definition" in r for r in report.reasons)


def test_a_narrowed_run_cannot_be_release_ready_while_an_approved_definition_is_uncovered(db, bootstrap):
    defn, user = _setup(db, bootstrap)
    adapter = OracleGrader(_points(defn))
    report = run_golden_gate(
        db,
        user,
        adapter_factory=lambda _d, _p: adapter,
        required_keys=[defn.definition_key, "another-required-definition"],
        definition_keys=[defn.definition_key],
    )
    assert all(c.passed for c in report.cases)
    assert report.release_ready is False
    assert report.coverage["missing"] == ["another-required-definition"]


def test_an_empty_run_is_never_ready(db, bootstrap):
    _defn, user = _setup(db, bootstrap)
    report = run_golden_gate(db, user, adapter_factory=lambda _d, _p: ScriptedGrader(), required_keys=[])
    assert report.cases == [] and report.release_ready is False


# -- every approved curriculum definition ----------------------------------------------------------------------------


def test_the_approved_set_is_every_seeded_definition_with_a_judged_criterion():
    expected = {s["key"] for s in LEVEL1_EXPLAIN_BACK_ASSESSMENTS} | {
        s["key"] for s in FOUNDATION_ASSESSMENTS if any(c["method"] == "grader" for c in s["criteria"])
    }
    assert set(approved_grader_definition_keys()) == expected
    assert {"eb-structured-output", "capstone-foundations"} <= expected and len(expected) == 10


def _seed_everything(db, bootstrap):
    _graph(db)
    for slug in sorted({s["concepts"][0] for s in LEVEL1_EXPLAIN_BACK_ASSESSMENTS}):
        from app.models.concepts import Concept

        if db.query(Concept).filter_by(slug=slug).first() is None:
            make_published_version(db, make_concept(db, slug=slug, name=slug))
    ProjectTemplateService.seed_foundation_projects(db, bootstrap.user.id)
    seed_foundation_assessments(db, bootstrap.user.id)
    seed_level1_explain_back_assessments(db, bootstrap.user.id)


def test_every_approved_definition_has_a_valid_golden_set_and_a_good_candidate_passes_all_of_them(
    db, bootstrap
):
    _seed_everything(db, bootstrap)
    setup_free_models(db, count=2)
    service = AssessmentDefinitionService(db)
    keys = approved_grader_definition_keys()
    assert len(keys) == 10
    for key in keys:
        definition = service.current(key)
        assert definition is not None, key
        assert len(golden_cases(definition)) == 6
        report = run_golden_gate(
            db,
            bootstrap.user,
            adapter_factory=lambda _d, _p, d=definition: OracleGrader(_points(d)),
            required_keys=[key],
            definition_keys=[key],
        )
        assert report.release_ready, (key, report.reasons)
    assert db.query(LearningEvidence).count() == 0
    assert db.query(AssessmentDefinition).filter_by(status="published").count() >= len(keys)


# -- the CLI ---------------------------------------------------------------------------------------------------------------


def test_the_cli_runs_on_a_copy_reports_and_never_touches_the_source(db, bootstrap, db_path, tmp_path):
    from scripts.ail5c_grader_gate import main

    defn, _user = _setup(db, bootstrap)
    db.commit()
    before = db_path.read_bytes()
    out, json_path = io.StringIO(), tmp_path / "report.json"
    adapter = OracleGrader(_points(defn))
    code = main(
        [str(db_path), "--definition", defn.definition_key, "--json", str(json_path)],
        adapter_factory=lambda _d, _p: adapter,
        stdout=out,
    )
    assert db_path.read_bytes() == before, "the source database is opened read-only and never written"
    report = json.loads(json_path.read_text(encoding="utf-8"))
    # a run narrowed to one definition cannot be ready: the other approved definitions are uncovered here
    assert code == 1 and report["release_ready"] is False
    assert report["coverage"]["covered"] == [defn.definition_key]
    assert all(c["passed"] for c in report["cases"])
    assert "NOT READY" in out.getvalue()


def test_the_cli_rejects_a_missing_database(tmp_path):
    from scripts.ail5c_grader_gate import main

    assert main([str(tmp_path / "does-not-exist.db")], stdout=io.StringIO()) == 2
