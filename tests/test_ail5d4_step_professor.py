"""AIL.5D.4 — the step-scoped AI Professor, validated on the structured Day 1 reference.

The Professor knows where the learner is (Day, step, progress, outline) and helps with the current step, but it is never an
answer or reveal channel: private material reaches it only once the learner has earned it, answer keys and AIL.5C grading
material never reach it, and it is held by an open assessment exactly like every other Professor request.
"""

import json
from decimal import Decimal

import pytest

from app.academy_steps import public_steps
from app.assessment_curriculum import LEVEL1_EXPLAIN_BACK_ASSESSMENTS
from app.config import settings
from app.db.enums import EvidenceType
from app.errors import ConflictError
from app.models.academy import AcademyProfessorHelp, AcademyStepResponse
from app.models.concepts import Concept
from app.models.learner import LearningEvidence
from app.schemas.professor import ProfessorContextRequest, ProfessorIntent, ProfessorTarget, ProfessorTargetType
from app.services.academy_level1_service import AcademyLevel1Service
from app.services.academy_step_service import AcademyStepService
from app.services.academy_structured_authoring import author_day_structure
from app.services.assessment_mode_guard import AssessmentModeActive
from app.services.assessment_service import AssessmentService
from app.services.professor_context_service import ProfessorContextAssembler
from app.services.professor_execution_service import ProfessorExecutionService
from tests.ail1a_factories import make_user
from tests.ail5c_factories import kc_definition
from tests.ail5d1_factories import new_version
from tests.conftest import make_model, make_provider, make_provider_model
from tests.test_ail5d3_day1 import _legacy_day1

THINK1 = "think-traditional-or-ai"
THINK2 = "think-three-claims"
CHECK = "ai-or-not-ai"
L1 = "/academy/level-1"

# Material the Professor must not receive until the learner has earned it (or ever).
REVEAL1 = "You want auditability"
CLAIM_WHYS = ("Scoring well on a standardized test is not the same as reliable legal advice", "Confidence of output is not correlated with accuracy",
              "Some limitations are being actively reduced")
RATIONALES = ("Mortgage calculation is arithmetic", "emotionally responsive text", "Quality depends on language pair and domain")
RUBRIC = tuple(LEVEL1_EXPLAIN_BACK_ASSESSMENTS[0]["points"]) + ("A satisfactory response will include", "INSUFFICIENT", "PASS / NEEDS_REVISION")


@pytest.fixture()
def day1(db, bootstrap):
    _legacy_day1(db)
    author_day_structure(db, 1)
    return next(r for r in AcademyLevel1Service(db)._current_academy_items() if r.spec["day"] == 1)


@pytest.fixture()
def user(bootstrap):
    return bootstrap.user


def _request(item, key, *, help=None, question=None):
    return ProfessorContextRequest(
        intent=ProfessorIntent.EXPLAIN_THIS, question=question, help=help,
        target=ProfessorTarget(type=ProfessorTargetType.ACADEMY_STEP, id=item.id, step_key=key),
    )


def _context(db, user, item, key, **kw):
    return ProfessorContextAssembler(db).assemble(user.id, _request(item, key, **kw))


def _blob(context):
    return json.dumps(context.model_dump(mode="json"), default=str)


def _assistance(context):
    return context.deterministic_facts["assistance"]


def _step_data(context):
    return next(r for r in context.records if r.ref_type == "academy_step").data


def _roles(context):
    return {r.role for r in context.records}


def _kc_answers(item):
    return {q["id"]: q["answer"] for q in item.spec["knowledge_check"]}


def _wrong_answers(item):
    return {q["id"]: {"system": "ai", "claim": "realistic"} for q in item.spec["knowledge_check"]}


# -- context architecture: what the Professor knows ----------------------------------------------------------------------------------------


def test_the_step_context_carries_where_the_learner_is_and_the_public_step(db, user, day1):
    context = _context(db, user, day1, "what-is-ai")
    facts = context.deterministic_facts["academy_step"]
    assert facts["program"] == "30-Day Practical AI Foundations" and facts["day"] == 1 and facts["week"] == 1 and facts["item_version"] == day1.version
    assert facts["step"] == {"key": "what-is-ai", "type": "teach", "title": "What exactly is AI?", "required": True, "position": 3, "status": "not_started"}
    assert [o["key"] for o in facts["outline"]][:3] == ["day-overview", "baseline", "what-is-ai"] and len(facts["outline"]) == 14
    assert facts["progress"]["required_total"] == 11 and facts["progress"]["completed_keys"] == []
    assert _roles(context) >= {"concept", "current_concept_version", "day_outline", "current_step"}
    step = _step_data(context)["step"]
    assert step["key"] == "what-is-ai" and step["content"]["blocks"] and "private" not in step
    assert context.target.step_key == "what-is-ai" and context.intent == ProfessorIntent.EXPLAIN_THIS


def test_teaching_steps_get_normal_explanations_and_carry_no_learner_responses(db, user, day1):
    for key in ("what-is-ai", "real-world-cases"):
        a = _assistance(_context(db, user, day1, key))
        assert a["mode"] == "teaching" and a["level"] == "explain" and a["request"] == "ask" and a["solution_eligible"] is True
    assert "learner_response_to_this_step" not in _roles(_context(db, user, day1, "what-is-ai"))


def test_the_context_changes_as_the_learner_advances(db, user, day1):
    svc = AcademyStepService(db)
    first = _context(db, user, day1, "what-is-ai")
    for key in ("day-overview", "what-is-ai", "not-one-technology"):
        svc.complete_step(user.id, day1.id, key)
    later = _context(db, user, day1, "traditional-vs-ai")
    facts = later.deterministic_facts["academy_step"]
    assert facts["progress"]["completed_keys"] == ["day-overview", "what-is-ai", "not-one-technology"] and facts["progress"]["current_step_key"] == "baseline"
    assert facts["step"]["key"] == "traditional-vs-ai" and _step_data(later)["step"]["key"] == "traditional-vs-ai"
    assert _step_data(first)["step"]["key"] == "what-is-ai" and _step_data(first)["learner"]["status"] == "not_started"
    statuses = {o["key"]: o["status"] for o in facts["outline"]}
    assert statuses["what-is-ai"] == "completed" and statuses["traditional-vs-ai"] == "not_started"
    # a different step is a different mode and a different record
    assert _assistance(_context(db, user, day1, THINK1))["mode"] == "guided" and _roles(first) == _roles(later)


def test_the_context_is_small_and_public_vocabulary_is_available_where_the_day_has_it(db, user, day1):
    context = _context(db, user, day1, "takeaways")
    assert len(_blob(context)) < settings.professor_max_context_chars
    vocabulary = next(r for r in context.records if r.ref_type == "academy_day").data["vocabulary"]
    assert {"term": "Hallucination", "definition": "When an AI system generates incorrect, fabricated, or unsupported information with apparent confidence"} in vocabulary
    assert len(vocabulary) == 8


# -- think: private reveal, eligibility ---------------------------------------------------------------------------------------------------------


def test_a_think_step_before_the_commit_has_no_reveal_no_answer_and_stops_below_explain(db, user, day1):
    for help_ in ("ask", "hint"):
        context = _context(db, user, day1, THINK1, help=help_)
        blob = _blob(context)
        assert REVEAL1 not in blob and "reveal_md" not in blob and "academy_reveal" not in blob and "private" not in blob
        a = _assistance(context)
        assert a["mode"] == "guided" and a["solution_eligible"] is False and a["revealed_solution"] is False
        assert a["level"] == ("clarify" if help_ == "ask" else "hint")
        assert "do not reveal, confirm or rule out the answer" in a["instructions"]
    step = _step_data(_context(db, user, day1, THINK1))["step"]
    assert step["content"]["question_md"] == "Should this be traditional software or AI? Why?"


def test_a_committed_answer_is_shared_but_the_reveal_stays_out_until_the_learner_has_been_served_it(db, user, day1):
    svc = AcademyStepService(db)
    svc.respond(user.id, day1.id, THINK1, {"text": "Traditional software: the rule is exact and safety matters."})
    context = _context(db, user, day1, THINK1, help="hint")
    assert REVEAL1 not in _blob(context) and "academy_reveal" not in {r.ref_type for r in context.records}
    response = next(r for r in context.records if r.ref_type == "academy_step_response")
    assert response.provenance_kind.value == "user_authored_conclusion" and response.data["response"] == {"text": "Traditional software: the rule is exact and safety matters."}
    a = _assistance(context)
    assert a["solution_eligible"] is True and a["revealed_solution"] is False and a["level"] == "hint"
    svc.reveal(user.id, day1.id, THINK1)
    after = _context(db, user, day1, THINK1)
    reveal = next(r for r in after.records if r.ref_type == "academy_reveal")
    assert REVEAL1 in json.dumps(reveal.data) and _assistance(after)["revealed_solution"] is True


def test_the_three_claims_reveal_follows_the_same_rule(db, user, day1):
    svc = AcademyStepService(db)
    claims = {"claim-1": {"choice": "depends"}, "claim-2": {"choice": "true"}, "claim-3": {"choice": "depends"}}
    assert not any(w in _blob(_context(db, user, day1, THINK2)) for w in CLAIM_WHYS)
    svc.respond(user.id, day1.id, THINK2, {"statements": claims})
    assert not any(w in _blob(_context(db, user, day1, THINK2, help="hint")) for w in CLAIM_WHYS)
    svc.reveal(user.id, day1.id, THINK2)
    assert CLAIM_WHYS[1] in _blob(_context(db, user, day1, THINK2))


# -- check: no answer keys, rationale only after a pass -------------------------------------------------------------------------------------------


def test_a_knowledge_check_never_exposes_answer_keys_and_releases_the_rationale_only_after_a_pass(db, user, day1):
    before = _blob(_context(db, user, day1, CHECK))
    assert "\"answer\"" not in before and "pass_criteria" not in before and "rationale" not in before and not any(r in before for r in RATIONALES)
    AcademyLevel1Service(db).knowledge_check(user.id, day1.id, _wrong_answers(day1))
    failed = _context(db, user, day1, CHECK, help="hint")
    assert not any(r in _blob(failed) for r in RATIONALES) and "\"answer\"" not in _blob(failed)
    learner = _step_data(failed)["learner"]
    assert learner["attempts"] == 1 and learner["passed"] is False and all("passed" in i for i in learner["last_attempt_items"])
    assert _assistance(failed)["solution_eligible"] is False and _assistance(failed)["level"] == "hint"
    questions = _step_data(failed)["step"]["questions"]
    assert len(questions) == 8 and all(set(q) <= {"id", "prompt", "choices"} for q in questions)
    AcademyLevel1Service(db).knowledge_check(user.id, day1.id, _kc_answers(day1))
    passed = _context(db, user, day1, CHECK, help="hint")
    assert any(r in _blob(passed) for r in RATIONALES) and _assistance(passed)["solution_eligible"] is True and _assistance(passed)["revealed_solution"] is True
    assert "\"answer\":" not in json.dumps(next(r for r in passed.records if r.ref_type == "academy_step").data["step"]["questions"])


# -- reflection and explain-back preparation ---------------------------------------------------------------------------------------------------------------


def test_a_reflection_sees_only_the_earlier_step_it_compares_to_and_no_other_responses(db, user, day1):
    svc = AcademyStepService(db)
    svc.respond(user.id, day1.id, "baseline", {"text": "BASELINE-THOUGHT AI is when computers think like humans."})
    svc.respond(user.id, day1.id, THINK1, {"text": "UNRELATED-ANSWER traditional software, exact rule."})
    svc.respond(user.id, day1.id, "explain-ai", {"points": {k: f"UNRELATED-OUTLINE {k} text" for k in ("what-ai-is", "realistic-capability", "limitation-or-overclaim", "rule-based-better")}})
    context = _context(db, user, day1, "compare-answers")
    blob = _blob(context)
    assert "BASELINE-THOUGHT" in blob and "UNRELATED-ANSWER" not in blob and "UNRELATED-OUTLINE" not in blob
    earlier = next(r for r in context.records if r.role == "earlier_response_this_step_compares_to")
    assert earlier.data["step_key"] == "baseline"
    assert _assistance(context)["mode"] == "guided" and _assistance(context)["solution_eligible"] is True


def test_explain_back_preparation_shares_the_outline_but_never_the_rubric_or_a_drafted_answer(db, user, day1):
    svc = AcademyStepService(db)
    outline = {"what-ai-is": "It finds patterns in data.", "realistic-capability": "Translation.", "limitation-or-overclaim": "It can hallucinate.", "rule-based-better": "Payroll."}
    svc.respond(user.id, day1.id, "explain-ai", {"points": outline})
    context = _context(db, user, day1, "explain-ai", help="hint")
    blob = _blob(context)
    assert not any(point in blob for point in RUBRIC)
    response = next(r for r in context.records if r.ref_type == "academy_step_response")
    assert response.data["response"] == {"points": outline}
    data = _step_data(context)
    assert data["step"]["binding"] == {"kind": "assessment", "definition_key": "level1-day-01-explain-ai"}
    assert "Never write the learner's own response" in _assistance(context)["instructions"]


def test_no_ail5c_grading_material_is_in_any_step_context_of_day_1(db, user, day1):
    for step in public_steps(day1.spec):
        for help_ in ("ask", "hint"):
            blob = _blob(_context(db, user, day1, step["key"], help=help_))
            assert not any(point in blob for point in RUBRIC), (step["key"], help_)
            assert "private" not in blob and "\"reveal_md\"" not in blob


# -- isolation, versions ----------------------------------------------------------------------------------------------------------------------------------------


def test_another_learners_responses_and_help_never_reach_the_context(db, user, day1):
    other = make_user(db, org=None, email="second@example.com")
    db.commit()
    svc = AcademyStepService(db)
    svc.respond(other.id, day1.id, "baseline", {"text": "OTHER-LEARNER-SECRET thought"})
    svc.respond(other.id, day1.id, THINK1, {"text": "OTHER-LEARNER-ANSWER committed"})
    svc.reveal(other.id, day1.id, THINK1)
    db.add(AcademyProfessorHelp(user_id=other.id, learning_item_id=day1.id, lineage_id=day1.lineage_id, step_key=THINK1,
                                step_fingerprint=next(s for s in public_steps(day1.spec) if s["key"] == THINK1)["fingerprint"],
                                mode=__import__("app.academy_professor", fromlist=["ProfessorMode"]).ProfessorMode.GUIDED,
                                request_kind=__import__("app.academy_professor", fromlist=["HelpRequest"]).HelpRequest.HINT,
                                help_level=__import__("app.academy_professor", fromlist=["HelpLevel"]).HelpLevel.HINT, interaction_id="x" * 36, context_sha256="0" * 64))
    db.commit()
    for key in ("baseline", THINK1, "compare-answers"):
        context = _context(db, user, day1, key, help="hint")
        blob = _blob(context)
        assert "OTHER-LEARNER" not in blob and REVEAL1 not in blob
        assert _assistance(context)["prior_hints"] == 0 and context.user_id == user.id
    assert _assistance(_context(db, other, day1, THINK1, help="hint"))["prior_hints"] == 1


def test_a_stale_item_version_provides_no_context_and_responses_carry_only_for_unchanged_steps(db, user, day1):
    svc = AcademyStepService(db)
    svc.respond(user.id, day1.id, "baseline", {"text": "STALE-CANDIDATE written on version two"})
    svc.respond(user.id, day1.id, THINK1, {"text": "Traditional software: the exact rule."})
    svc.reveal(user.id, day1.id, THINK1)
    spec = json.loads(json.dumps(day1.spec))
    next(s for s in spec["steps"] if s["key"] == THINK1)["content"]["question_md"] = "A reworded question?"
    v3 = new_version(db, day1, spec=spec)
    with pytest.raises(ConflictError) as err:
        _context(db, user, day1, "what-is-ai")
    assert err.value.detail["code"] == "stale_item_version" and err.value.detail["current_item_id"] == v3.id
    with pytest.raises(ConflictError):
        ProfessorExecutionService(db).step_help_status(user, day1.id, "what-is-ai")
    fresh = _context(db, user, v3, "baseline")
    assert "STALE-CANDIDATE" in _blob(fresh)                                   # an unchanged step carries
    revised = _context(db, user, v3, THINK1)
    assert REVEAL1 not in _blob(revised) and "Traditional software: the exact rule." not in _blob(revised)   # a revised step starts fresh
    assert _assistance(revised)["solution_eligible"] is False


def test_unknown_steps_and_misused_targets_are_refused(db, user, day1):
    with pytest.raises(Exception):
        _context(db, user, day1, "no-such-step")
    with pytest.raises(ValueError):
        ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=day1.concept_id, step_key="what-is-ai")     # step_key only with a step target
    with pytest.raises(ValueError):
        ProfessorTarget(type=ProfessorTargetType.ACADEMY_STEP, id=day1.id)                                 # a step target needs its step
    with pytest.raises(ValueError):
        ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, help="hint", target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=day1.concept_id))
    with pytest.raises(ValueError):
        ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, help="solution", target=ProfessorTarget(type=ProfessorTargetType.ACADEMY_STEP, id=day1.id, step_key="what-is-ai"))
    with pytest.raises(Exception):
        ProfessorContextAssembler(db).assemble(user.id, ProfessorContextRequest(
            intent=ProfessorIntent.WHY_DOES_THIS_MATTER, target=ProfessorTarget(type=ProfessorTargetType.ACADEMY_STEP, id=day1.id, step_key="what-is-ai")))


# -- the legacy Concept context no longer hands the Professor a Day body with reveals and the rubric ----------------------------------------------------------------


def test_legacy_academy_day_bodies_are_not_given_to_the_professor_but_structured_bodies_are(db, user, bootstrap):
    from app.services.academy_level1_service import _day_body

    legacy = _legacy_day1(db)
    assert "A satisfactory response will include" in legacy.body_md and "Traditional software. The rule is exact" in legacy.body_md   # the old leak
    request = ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, target=ProfessorTarget(type=ProfessorTargetType.CONCEPT, id=legacy.concept_id))
    blob = _blob(ProfessorContextAssembler(db).assemble(user.id, request))
    assert "A satisfactory response will include" not in blob and "Traditional software. The rule is exact" not in blob and REVEAL1 not in blob
    author_day_structure(db, 1)
    after = _blob(ProfessorContextAssembler(db).assemble(user.id, request))
    assert "What exactly is AI?" in after and "A satisfactory response will include" not in after and REVEAL1 not in after and _day_body(1)


# -- execution: a real Professor interaction, end to end with a fake provider -------------------------------------------------------------------------------------------------


class CapturingAdapter:
    def __init__(self):
        self.calls = []

    def invoke(self, request):
        from app.providers.base import InvokeResponse

        self.calls.append(request)
        body = {"intent": "EXPLAIN_THIS", "direct_answer": "Think about what makes a rule exact, then decide.", "explanation": "A hint, not the answer.",
                "evidence": [], "grounded_assertions": [{"assertion_kind": "ai_explanation", "text": "A hint, not the answer.", "references": []}],
                "uncertainties": [], "suggested_next_actions": [], "attachment_references": []}
        return InvokeResponse(text=json.dumps(body), tokens_in=100, tokens_out=40, tokens_total=140, latency_ms=3, provider_http_status=200, finish_reason="stop")


@pytest.fixture()
def professor(db, bootstrap):
    model = make_model(db, canonical_model_id="free/step-professor", structured_output_support=True)
    provider = make_provider(db)
    make_provider_model(db, model=model, provider=provider, cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()
    adapter = CapturingAdapter()
    return adapter, (lambda user, request: ProfessorExecutionService(db, adapter_factory=lambda _d, _p: adapter).create_and_execute(user, request))


def _prompt(adapter, index=-1):
    call = adapter.calls[index]
    return (call.system_prompt or "") + "\n" + call.user_prompt


def test_a_hint_on_a_think_step_reaches_the_model_without_the_reveal_or_grading_material(db, user, day1, professor):
    adapter, ask = professor
    result = ask(user, _request(day1, THINK1, help="hint", question="I'm stuck, can I have a hint?"))
    assert result.status == "complete" and result.assistance == {"mode": "guided", "level": "hint", "request": "hint", "step_key": THINK1, "prior_hints": 0}
    prompt = _prompt(adapter)
    assert "Should this be traditional software or AI? Why?" in prompt and "I'm stuck, can I have a hint?" in prompt
    assert "assistance.mode" in prompt and "\"mode\":\"guided\"" in prompt.replace(" ", "")
    for secret in (REVEAL1, *CLAIM_WHYS, *RATIONALES, *RUBRIC, "reveal_md", "\"private\""):
        assert secret not in prompt, secret
    assert "instructions" not in json.dumps(result.assistance)


def test_help_is_tracked_on_the_existing_assistance_scale_and_the_ladder_climbs_with_each_hint(db, user, day1, professor):
    adapter, ask = professor
    levels = []
    for _ in range(4):
        result = ask(user, _request(day1, THINK1, help="hint"))
        assert result.status == "complete"
        levels.append(result.assistance["level"])
    # not eligible to see a full explanation of an unanswered exercise, so the ladder stops at the stronger hint
    assert levels == ["hint", "stronger_hint", "stronger_hint", "stronger_hint"]
    rows = db.query(AcademyProfessorHelp).order_by(AcademyProfessorHelp.created_at).all()
    assert [r.assistance_level.value for r in rows] == ["h2", "h3", "h3", "h3"] and {r.mode.value for r in rows} == {"guided"}
    assert all(r.user_id == user.id and r.lineage_id == day1.lineage_id and r.step_key == THINK1 and not r.revealed_solution for r in rows)
    assert all(len(r.context_sha256) == 64 and r.interaction_id for r in rows) and len({r.interaction_id for r in rows}) == 4


def test_after_committing_and_seeing_the_reveal_help_may_explain_and_is_recorded_as_solution_shown(db, user, day1, professor):
    adapter, ask = professor
    svc = AcademyStepService(db)
    svc.respond(user.id, day1.id, THINK1, {"text": "Traditional software: the rule is exact."})
    svc.reveal(user.id, day1.id, THINK1)
    ask(user, _request(day1, THINK1, help="hint"))
    assert REVEAL1 in _prompt(adapter)                                        # earned: the learner has seen it
    row = db.query(AcademyProfessorHelp).one()
    assert row.revealed_solution is True and row.assistance_level.value == "h5" and row.help_level.value == "hint"


def test_a_free_question_is_clarification_and_a_teaching_question_is_not_assistance(db, user, day1, professor):
    adapter, ask = professor
    ask(user, _request(day1, THINK1, question="What does 'deterministic' mean?"))
    ask(user, _request(day1, "what-is-ai", question="Explain this differently."))
    guided, teaching = db.query(AcademyProfessorHelp).order_by(AcademyProfessorHelp.created_at).all()
    assert (guided.mode.value, guided.request_kind.value, guided.help_level.value, guided.assistance_level.value) == ("guided", "ask", "clarify", "h1")
    assert (teaching.mode.value, teaching.help_level.value, teaching.assistance_level) == ("teaching", "explain", None)
    assert "Explain this differently." in _prompt(adapter)


def test_using_the_professor_creates_no_evidence_and_changes_no_progress(db, user, day1, professor):
    adapter, ask = professor
    before = db.query(LearningEvidence).count()
    for key, help_ in (("what-is-ai", None), (THINK1, "hint"), (CHECK, "hint"), ("explain-ai", "hint")):
        ask(user, _request(day1, key, help=help_))
    assert db.query(LearningEvidence).count() == before == 0
    assert db.query(AcademyStepResponse).count() == 0
    view = AcademyStepService(db).learning_view(user.id, day1.id)
    assert view["required_completed"] == 0 and view["learning_complete"] is False and view["demonstrated"] is False
    assert {s["status"] for s in view["steps"]} == {"not_started"}


def test_a_failed_provider_call_delivers_no_help_and_records_none(db, user, day1, bootstrap):
    from app.providers.base import ProviderConnectionError

    class Broken:
        def invoke(self, request):
            raise ProviderConnectionError("unavailable", status_code=503)

    model = make_model(db, canonical_model_id="free/step-broken", structured_output_support=True)
    make_provider_model(db, model=model, provider=make_provider(db), cost_input_per_mtok=Decimal(0), cost_output_per_mtok=Decimal(0))
    db.commit()
    result = ProfessorExecutionService(db, adapter_factory=lambda _d, _p: Broken()).create_and_execute(user, _request(day1, THINK1, help="hint"))
    assert result.status != "complete" and db.query(AcademyProfessorHelp).count() == 0


def test_step_help_cannot_be_continued_from_a_stored_context(db, user, day1, professor):
    adapter, ask = professor
    first = ask(user, _request(day1, THINK1, help="hint"))
    with pytest.raises(ConflictError) as err:
        ProfessorExecutionService(db, adapter_factory=lambda _d, _p: adapter).continue_interaction(
            user, first.interaction_id, ProfessorContextRequest(intent=ProfessorIntent.EXPLAIN_THIS, question="more"))
    assert err.value.detail["code"] == "step_help_not_continuable"


def test_the_status_for_the_learner_says_what_the_professor_is_and_can_see(db, user, day1):
    status = ProfessorExecutionService(db).step_help_status(user, day1.id, THINK1)
    assert status["mode"] == "guided" and status["next_hint_level"] == "hint" and status["available"] is True and status["solution_eligible"] is False
    assert "current step" in status["knows"] and "day outline" in status["knows"]
    assert "the assessment's grading material" in status["cannot_see"] and "answers you have not earned yet" in status["cannot_see"]
    assert "instructions" not in json.dumps(status) and REVEAL1 not in json.dumps(status)


# -- AIL.5C assessment mode: the existing restriction is preserved -----------------------------------------------------------------------------------------------------------


def test_an_open_assessment_on_the_days_concept_pauses_the_step_professor_exactly_like_concept_help(db, user, day1, professor):
    adapter, ask = professor
    concept = db.get(Concept, day1.concept_id)
    definition = kc_definition(db, user, concept, key="day1-lock")
    attempt = AssessmentService(db).start(user, definition.definition_key)          # Assessment Mode begins
    with pytest.raises(AssessmentModeActive):
        ask(user, _request(day1, THINK1, help="hint"))
    with pytest.raises(AssessmentModeActive):
        ask(user, _request(day1, "what-is-ai", question="explain"))                   # teaching steps are held too: same Concept
    status = ProfessorExecutionService(db).step_help_status(user, day1.id, THINK1)
    assert status["available"] is False and status["paused_reason"]
    assert adapter.calls == [] and db.query(AcademyProfessorHelp).count() == 0         # no model call, no tracked help
    AssessmentService(db).abandon(user, attempt.id)                                   # leaving the assessment lifts the pause
    assert ask(user, _request(day1, THINK1, help="hint")).status == "complete"


def test_the_assessment_lock_is_learner_scoped_for_steps(db, user, day1, professor):
    adapter, ask = professor
    definition = kc_definition(db, user, db.get(Concept, day1.concept_id), key="day1-lock-other")
    AssessmentService(db).start(user, definition.definition_key)
    other = make_user(db, org=None, email="third@example.com")
    db.commit()
    from app.services.assessment_mode_guard import AssessmentModeGuard

    AssessmentModeGuard(db).assert_professor_available(other.id, concept_ids=[day1.concept_id])   # someone else's attempt locks nothing for them
    with pytest.raises(AssessmentModeActive):
        ask(user, _request(day1, THINK1, help="hint"))


def test_the_professor_prompt_keeps_its_contract_and_adds_the_step_clause():
    prompt = ProfessorExecutionService._professor_prompt()
    assert "Return ONLY that object" in prompt and "deterministic_facts.assistance" in prompt and "never write the learner's own response" in prompt
