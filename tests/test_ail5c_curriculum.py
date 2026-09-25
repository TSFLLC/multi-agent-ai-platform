"""AIL.5C authored content, seeding and the capstone assessment."""

from datetime import datetime, timedelta, timezone

import pytest

from app.assessment_curriculum import FOUNDATION_ASSESSMENTS, seed_foundation_assessments
from app.db.enums import AssessmentKind, AssessmentOutcome, AssistanceLevel, DemonstrationEffect, ProjectAttemptStatus
from app.models.academy import ProjectAttempt, ProjectTemplate
from app.models.assessment import AssessmentDefinition
from app.services.assessment_definition_service import AssessmentDefinitionService
from app.services.assessment_service import AssessmentService
from app.services.build_with_me_service import ProjectTemplateService
from app.services.learner_state_service import DEMONSTRATED, LearnerStateService
from app.services.system_project_service import ensure_ail_system_project
from tests.ail5c_factories import (
    ScriptedGrader,
    add_milestone_evidence,
    make_evaluation_run,
    make_owned_run,
    make_project_attempt,
    setup_free_models,
)
from tests.ail1a_factories import make_concept, make_published_version

# The capstone row includes a judged criterion, so it is AI-graded: on its own it can never
# demonstrate. A real capstone Concept pairs it with deterministic, verified milestone evidence.
REQ_CAPSTONE = {"requires_all": [
    {"evidence_type": "lab", "min_passed": 1, "min_verification": "platform_verified"},
    {"evidence_type": "project_assessment", "min_passed": 1},
]}


def _all_slugs():
    return sorted({slug for spec in FOUNDATION_ASSESSMENTS for slug in spec["concepts"]})


def _graph(db, requirements=None):
    """The Concept Graph slugs the authored content references, plus the four 5B project templates."""
    versions = {}
    from app.academy_curriculum import BUILD_WITH_ME_PROJECTS

    slugs = set(_all_slugs()) | {s for p in BUILD_WITH_ME_PROJECTS for s in p["concepts"]}
    for slug in sorted(slugs):
        concept = make_concept(db, slug=slug, name=slug.replace("-", " ").title())
        versions[slug] = (concept, make_published_version(db, concept, evidence_requirements=requirements))
    return versions


@pytest.fixture()
def seeded(db, bootstrap):
    versions = _graph(db)
    ProjectTemplateService.seed_foundation_projects(db, bootstrap.user.id)
    created = seed_foundation_assessments(db, bootstrap.user.id)
    return versions, created


def test_the_authored_set_publishes_and_is_hash_frozen(seeded, db):
    _versions, created = seeded
    assert {d.definition_key for d in created} == {s["key"] for s in FOUNDATION_ASSESSMENTS}
    service = AssessmentDefinitionService(db)
    kinds = {d.assessment_kind for d in created}
    assert kinds == {AssessmentKind.KNOWLEDGE_CHECK, AssessmentKind.EXPLAIN_BACK, AssessmentKind.MODIFICATION,
                     AssessmentKind.PROJECT, AssessmentKind.CAPSTONE}
    for defn in created:
        assert service.verify_hash(defn) and defn.version == 1
        for link in service.links(defn.id):
            assert link.concept_version_id  # pinned, never "latest"
    capstone = next(d for d in created if d.assessment_kind == AssessmentKind.CAPSTONE)
    assert capstone.independence_policy["fresh_required"] == "always" and capstone.project_template_id
    det = [c for c in capstone.criteria if c["method"] == "deterministic"]
    assert len(det) / len(capstone.criteria) >= 0.7


def test_seeding_is_idempotent_and_never_republishes(seeded, db, bootstrap):
    assert seed_foundation_assessments(db, bootstrap.user.id) == []
    assert db.query(AssessmentDefinition).count() == len(FOUNDATION_ASSESSMENTS)


def test_seeding_refuses_when_a_concept_or_template_is_missing_and_creates_nothing(db, bootstrap):
    make_concept(db, slug="structured-output", name="Structured Output")  # one concept, no version, no templates
    db.commit()
    with pytest.raises(ValueError) as exc:
        seed_foundation_assessments(db, bootstrap.user.id)
    assert "hallucination-grounding" in str(exc.value) and "p2-structured-extractor" in str(exc.value)
    assert db.query(AssessmentDefinition).count() == 0


@pytest.mark.parametrize("spec", [s for s in FOUNDATION_ASSESSMENTS if s["kind"] == AssessmentKind.KNOWLEDGE_CHECK], ids=lambda s: s["key"])
def test_knowledge_check_content_is_well_formed(spec):
    pool = spec["challenge"]["pool"]
    assert len(pool) >= 3 * spec["challenge"]["draw_size"]
    positions = set()
    for item in pool:
        assert len(item["options"]) == len(set(item["options"])) >= 3
        assert item["answer_key"] and all(0 <= k < len(item["options"]) for k in item["answer_key"])
        positions.add(item["answer_key"][0])
        assert item["prompt"].endswith(("?", "..."))
    assert len(positions) >= 3, "the correct answer must not sit in the same position every time"


def test_every_judged_criterion_has_authored_reference_points_and_a_remediation_or_default():
    for spec in FOUNDATION_ASSESSMENTS:
        for c in spec["criteria"]:
            if c["method"] == "grader":
                assert c["reference_points"] and all(p.strip() for p in c["reference_points"]), (spec["key"], c["key"])
    for spec in FOUNDATION_ASSESSMENTS:
        assert spec["kind"] != AssessmentKind.KNOWLEDGE_CHECK or all(c["method"] == "deterministic" for c in spec["criteria"])


# -- capstone --------------------------------------------------------------------------------------------------


READMEE = "## Problem\nExtract fields.\n\n## Evidence\nEvaluation met all criteria.\n\n## Limits\nOnly tested on invoices.\n"


def _capstone(db, bootstrap, *, levels=(AssistanceLevel.H1,)):
    versions = _graph(db, requirements=REQ_CAPSTONE)
    ProjectTemplateService.seed_foundation_projects(db, bootstrap.user.id)
    seed_foundation_assessments(db, bootstrap.user.id)
    setup_free_models(db, count=2)
    user = bootstrap.user
    template = db.query(ProjectTemplate).filter_by(template_key="p4-project-direction").one()
    pa = make_project_attempt(db, user, template, levels=levels)
    concept, version = versions["evaluation"]
    add_milestone_evidence(db, user, pa, concept, version)
    return user, template, pa, versions


def _fill(attempt, run, evaluation, *, readme=READMEE):
    responses = {i["entry_key"]: {"text": f"In my project I chose this deliberately and verified it by rerunning ({i['entry_key']})."}
                 for i in attempt.challenge_instance["items"] if i.get("fixed")}
    return {"evaluation_run_ids": [evaluation.id], "run_ids": [run.id], "fields": {"readme": readme}, "responses": responses}


def test_capstone_needs_a_fresh_challenge_and_passes_with_verified_work(db, bootstrap):
    user, _t, pa, versions = _capstone(db, bootstrap)
    adapter = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    report = svc.readiness(user, "capstone-foundations", project_attempt_id=pa.id)
    assert report["ready"] and report["fresh_required"] and report["fresh_reason"] == "policy_always"
    attempt = svc.start(user, "capstone-foundations", project_attempt_id=pa.id)
    fixed = [i for i in attempt.challenge_instance["items"] if i.get("fixed")]
    assert len(fixed) == 3 and any(not i.get("fixed") for i in attempt.challenge_instance["items"])
    needle = attempt.challenge_instance["parameters"]["input_text"]
    run = make_owned_run(db, user, project=ensure_ail_system_project(db, user), description=f"Rerun on {needle}",
                         created_at=datetime.now(timezone.utc) + timedelta(minutes=2))
    svc.save_draft(user.id, attempt.id, _fill(attempt, run, make_evaluation_run(db, user, ["met", "met"])))
    done = svc.submit(user, attempt.id, {"declaration": "no_external_help"})
    final = svc.effective_result(done.id)
    assert final.outcome == AssessmentOutcome.PASSED
    assert final.demonstration_effect == DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED
    assert db.get(ProjectAttempt, pa.id).status == ProjectAttemptStatus.PASSED
    assert len(adapter.requests) == 2  # capstone always cross-checks its judged criterion
    # the explain-back set was issued to the learner and reached the Grader as data
    assert "In my project I chose this deliberately" in adapter.requests[0].user_prompt
    concept, _v = versions["evaluation"]
    assert LearnerStateService(db).state(user.id, concept.id).ladder == DEMONSTRATED
    # ...and the same capstone row ALONE (no deterministic leg) would not have demonstrated
    from app.db.enums import EvidenceType
    from app.models.learner import LearningEvidence

    for row in db.query(LearningEvidence).filter_by(user_id=user.id, evidence_type=EvidenceType.LAB).all():
        row.superseded_by_id = db.query(LearningEvidence).filter_by(user_id=user.id, evidence_type=EvidenceType.PROJECT_ASSESSMENT).first().id
    db.commit()
    assert LearnerStateService(db).state(user.id, concept.id).ladder != DEMONSTRATED


def test_capstone_missing_a_required_section_is_needs_work_without_grading(db, bootstrap):
    user, _t, pa, _versions = _capstone(db, bootstrap)
    adapter = ScriptedGrader()
    svc = AssessmentService(db, adapter_factory=lambda _d, _p: adapter)
    attempt = svc.start(user, "capstone-foundations", project_attempt_id=pa.id)
    needle = attempt.challenge_instance["parameters"]["input_text"]
    run = make_owned_run(db, user, project=ensure_ail_system_project(db, user), description=needle,
                         created_at=datetime.now(timezone.utc) + timedelta(minutes=2))
    svc.save_draft(user.id, attempt.id, _fill(attempt, run, make_evaluation_run(db, user, ["met"]), readme="## Problem\nOnly one section."))
    final = svc.effective_result(svc.submit(user, attempt.id, {"declaration": "no_external_help"}).id)
    assert final.outcome == AssessmentOutcome.NEEDS_WORK and adapter.requests == []
    assert db.get(ProjectAttempt, pa.id).status == ProjectAttemptStatus.NEEDS_WORK
    assert any(g["criterion_key"] == "readme" for g in final.gaps)


def test_capstone_on_h5_source_work_still_needs_the_fresh_challenge_to_qualify(db, bootstrap):
    user, _t, pa, _versions = _capstone(db, bootstrap, levels=(AssistanceLevel.H5,))
    svc = AssessmentService(db)
    attempt = svc.start(user, "capstone-foundations", project_attempt_id=pa.id)
    assert attempt.fresh_required and attempt.challenge_instance["items"]
    assert attempt.input_manifest["source_work"]["levels"] == ["h5"]  # the help history stays visible
