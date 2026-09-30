"""AIL.5D.1 — learner step progress: user scoping, open != complete, forge-resistance, versioning, compatibility."""

import copy

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import AcademyStepStatus, ConceptKind, ConceptLevel, LearningItemType
from app.errors import ConflictError, NotFoundError
from app.models.academy import AcademyStepProgress
from app.models.concepts import LearningItem
from app.models.learner import LearningEvidence
from app.services.academy_step_service import AcademyStepService, VerifiedCompletion
from app.services.concept_graph_service import ConceptGraphService
from tests.ail1a_factories import make_concept, make_published_version, make_user
from tests.ail5d1_factories import make_structured_item, new_version, sample_spec


@pytest.fixture()
def item(db):
    return make_structured_item(db)


@pytest.fixture()
def learner(db):
    user = make_user(db, email="a@example.com")
    db.commit()
    return user


@pytest.fixture()
def other(db, learner):
    user = make_user(db, org=None, email="b@example.com")
    db.commit()
    return user


def _svc(db):
    return AcademyStepService(db)


def _status(db, user, item_id, key):
    return next(s for s in _svc(db).progress(user.id, item_id)["steps"] if s["key"] == key)["status"]


# -- opening / viewing is not completion ------------------------------------------------------------------------------------


def test_opening_a_step_records_a_view_and_never_completes_it(db, item, learner):
    out = _svc(db).open_step(learner.id, item.id, "what-is-ai")
    assert out["step"]["status"] == "opened" and out["step"]["open_count"] == 1
    assert out["day"]["required_completed"] == 0 and out["day"]["learning_complete"] is False
    for _ in range(3):
        out = _svc(db).open_step(learner.id, item.id, "what-is-ai")
    assert out["step"]["status"] == "opened" and out["step"]["open_count"] == 4
    row = db.query(AcademyStepProgress).one()
    assert row.status == AcademyStepStatus.OPENED and row.completed_at is None and row.completion_basis is None


def test_a_step_nobody_opened_is_not_started(db, item, learner):
    progress = _svc(db).progress(learner.id, item.id)
    assert {s["status"] for s in progress["steps"]} == {"not_started"}
    assert db.query(AcademyStepProgress).count() == 0  # reading progress never writes


def test_opening_a_completed_step_does_not_reopen_or_uncomplete_it(db, item, learner):
    _svc(db).complete_step(learner.id, item.id, "what-is-ai")
    out = _svc(db).open_step(learner.id, item.id, "what-is-ai")
    assert out["step"]["status"] == "completed" and out["step"]["open_count"] == 2


# -- completion rules ---------------------------------------------------------------------------------------------------------


def test_a_self_completable_step_completes_on_continue_and_is_idempotent(db, item, learner):
    first = _svc(db).complete_step(learner.id, item.id, "what-is-ai")
    assert first["step"]["status"] == "completed"
    row = db.query(AcademyStepProgress).one()
    stamp, basis = row.completed_at, row.completion_basis
    assert basis == {"kind": "continue"}
    _svc(db).complete_step(learner.id, item.id, "what-is-ai")
    db.refresh(row)
    assert row.completed_at == stamp and db.query(AcademyStepProgress).count() == 1


@pytest.mark.parametrize("key", ["baseline", "bp-reminder", "three-claims", "ai-or-not", "explain-ai", "compare-answers", "lab-run"])
def test_an_interactive_step_cannot_be_completed_by_asking(db, item, learner, key):
    with pytest.raises(ConflictError) as err:
        _svc(db).complete_step(learner.id, item.id, key)
    assert err.value.detail["code"] == "interaction_required"
    assert db.query(AcademyStepProgress).filter_by(step_key=key, status=AcademyStepStatus.COMPLETED).count() == 0


def test_an_interactive_step_completes_only_with_a_server_verified_basis(db, item, learner):
    out = _svc(db).complete_step(learner.id, item.id, "bp-reminder", verified=VerifiedCompletion(kind="committed_answer", reference="r-1"))
    assert out["step"]["status"] == "completed"
    assert db.query(AcademyStepProgress).one().completion_basis == {"kind": "committed_answer", "reference": "r-1"}


def test_completion_writes_no_learning_evidence(db, item, learner):
    before = db.query(LearningEvidence).count()
    for key in ("what-is-ai", "rules-vs-patterns"):
        _svc(db).open_step(learner.id, item.id, key)
        _svc(db).complete_step(learner.id, item.id, key)
    assert db.query(LearningEvidence).count() == before


def test_required_steps_cannot_be_skipped_but_optional_ones_can(db, item, learner):
    with pytest.raises(ConflictError) as err:
        _svc(db).skip_step(learner.id, item.id, "what-is-ai")
    assert err.value.detail["code"] == "required_step"
    out = _svc(db).skip_step(learner.id, item.id, "baseline")
    assert out["step"]["status"] == "skipped"
    assert out["day"]["optional_completed"] == 0  # skipping is not completing


def test_an_optional_step_can_be_completed_after_being_skipped_and_a_completed_one_is_not_skipped(db, item, learner):
    _svc(db).skip_step(learner.id, item.id, "compare-answers")
    out = _svc(db).complete_step(learner.id, item.id, "compare-answers", verified=VerifiedCompletion(kind="saved_reflection"))
    assert out["step"]["status"] == "completed"
    again = _svc(db).skip_step(learner.id, item.id, "compare-answers")
    assert again["step"]["status"] == "completed"
    assert db.query(AcademyStepProgress).filter_by(step_key="compare-answers").one().skipped_at is None


def test_unknown_step_is_not_found(db, item, learner):
    with pytest.raises(NotFoundError):
        _svc(db).open_step(learner.id, item.id, "no-such-step")


# -- required vs optional and "learning complete" ---------------------------------------------------------------------------


def test_learning_complete_needs_every_required_step_and_ignores_optional_ones(db, item, learner):
    svc = _svc(db)
    required = ["what-is-ai", "rules-vs-patterns", "bp-reminder", "three-claims", "ai-or-not", "lab-run"]
    for key in required[:-1]:
        svc.complete_step(learner.id, item.id, key, verified=VerifiedCompletion(kind="test"))
    day = svc.progress(learner.id, item.id)
    assert day["required_total"] == 6 and day["required_completed"] == 5 and day["learning_complete"] is False
    assert day["optional_total"] == 3 and day["optional_completed"] == 0
    svc.complete_step(learner.id, item.id, "lab-run", verified=VerifiedCompletion(kind="test"))
    day = svc.progress(learner.id, item.id)
    assert day["learning_complete"] is True  # with zero optional steps done
    assert "not demonstrated knowledge" in day["note"]


def test_a_skipped_optional_step_never_counts_toward_completion(db, item, learner):
    _svc(db).skip_step(learner.id, item.id, "baseline")
    day = _svc(db).progress(learner.id, item.id)
    assert day["optional_completed"] == 0 and day["required_completed"] == 0


# -- user scoping ------------------------------------------------------------------------------------------------------------


def test_progress_is_scoped_to_the_learner(db, item, learner, other):
    _svc(db).complete_step(learner.id, item.id, "what-is-ai")
    assert _status(db, learner, item.id, "what-is-ai") == "completed"
    assert _status(db, other, item.id, "what-is-ai") == "not_started"
    _svc(db).open_step(other.id, item.id, "what-is-ai")
    assert _status(db, other, item.id, "what-is-ai") == "opened"
    assert _status(db, learner, item.id, "what-is-ai") == "completed"
    assert {r.user_id for r in db.query(AcademyStepProgress)} == {learner.id, other.id}


def test_one_row_per_learner_item_and_step(db, item, learner):
    _svc(db).open_step(learner.id, item.id, "what-is-ai")
    row = db.query(AcademyStepProgress).one()
    db.add(AcademyStepProgress(
        user_id=learner.id, learning_item_id=item.id, lineage_id=item.lineage_id, step_key="what-is-ai", step_fingerprint="f" * 64,
        status=AcademyStepStatus.OPENED, open_count=1, first_opened_at=row.first_opened_at, last_opened_at=row.last_opened_at,
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_the_database_itself_rejects_inconsistent_completion_rows(db, item, learner):
    _svc(db).open_step(learner.id, item.id, "what-is-ai")
    row = db.query(AcademyStepProgress).one()
    row.status = AcademyStepStatus.COMPLETED  # completed without completed_at
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    row = db.query(AcademyStepProgress).one()
    row.open_count = 0
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# -- immutable/versioned Learning Item compatibility -------------------------------------------------------------------------


def test_a_new_version_is_a_new_row_and_a_stale_item_id_is_refused(db, item, learner):
    _svc(db).complete_step(learner.id, item.id, "what-is-ai")
    v2 = new_version(db, item, spec=sample_spec())
    assert v2.version == 2 and v2.lineage_id == item.lineage_id and v2.id != item.id
    with pytest.raises(ConflictError) as err:
        _svc(db).open_step(learner.id, item.id, "what-is-ai")
    assert err.value.detail == {"code": "stale_item_version", "current_item_id": v2.id, "current_version": 2}
    with pytest.raises(ConflictError):
        _svc(db).complete_step(learner.id, item.id, "rules-vs-patterns")
    # the old version's row is preserved, untouched, as history
    old = db.query(AcademyStepProgress).filter_by(learning_item_id=item.id).one()
    assert old.status == AcademyStepStatus.COMPLETED


def test_an_unchanged_step_carries_progress_to_the_new_version_and_a_changed_one_does_not(db, item, learner):
    for key in ("what-is-ai", "rules-vs-patterns"):
        _svc(db).complete_step(learner.id, item.id, key)
    _svc(db).open_step(learner.id, item.id, "bp-reminder")  # opened only: never carries
    spec2 = sample_spec()
    next(s for s in spec2["steps"] if s["key"] == "rules-vs-patterns")["content"]["lead_md"] = "Revised wording."
    v2 = new_version(db, item, spec=spec2)
    steps = {s["key"]: s for s in _svc(db).progress(learner.id, v2.id)["steps"]}
    assert steps["what-is-ai"]["status"] == "completed" and steps["what-is-ai"]["carried_from_version"] == 1
    assert steps["rules-vs-patterns"]["status"] == "not_started" and steps["rules-vs-patterns"]["carried_from_version"] is None
    assert steps["bp-reminder"]["status"] == "not_started"
    assert _svc(db).progress(learner.id, v2.id)["required_completed"] == 1
    # completing the revised step on v2 is recorded against v2 only
    _svc(db).complete_step(learner.id, v2.id, "rules-vs-patterns")
    assert db.query(AcademyStepProgress).filter_by(learning_item_id=v2.id).count() == 1


def test_correcting_a_hidden_answer_in_a_new_version_does_not_invalidate_progress(db, item, learner):
    _svc(db).complete_step(learner.id, item.id, "bp-reminder", verified=VerifiedCompletion(kind="test"))
    spec2 = sample_spec()
    next(s for s in spec2["steps"] if s["key"] == "bp-reminder")["private"]["reveal_md"] = "A corrected explanation."
    v2 = new_version(db, item, spec=spec2)
    assert next(s for s in _svc(db).progress(learner.id, v2.id)["steps"] if s["key"] == "bp-reminder")["status"] == "completed"


def test_carry_over_is_per_learner(db, item, learner, other):
    _svc(db).complete_step(learner.id, item.id, "what-is-ai")
    v2 = new_version(db, item, spec=sample_spec())
    assert _status(db, learner, v2.id, "what-is-ai") == "completed"
    assert _status(db, other, v2.id, "what-is-ai") == "not_started"


def test_a_removed_step_no_longer_appears_and_authored_versions_stay_immutable(db, item, learner):
    v2 = new_version(db, item, spec=sample_spec(steps=[s for s in sample_spec()["steps"] if s["key"] not in ("baseline", "compare-answers")]))
    assert "baseline" not in {s["key"] for s in _svc(db).progress(learner.id, v2.id)["steps"]}
    original = db.get(LearningItem, item.id)
    assert "baseline" in {s["key"] for s in original.spec["steps"]}


# -- backward compatibility --------------------------------------------------------------------------------------------------


def test_a_non_structured_item_has_no_steps_and_no_progress(db, learner):
    concept = make_concept(db, slug="legacy-concept", name="Legacy")
    make_published_version(db, concept=concept)
    legacy = ConceptGraphService(db).create_learning_item(
        concept_id=concept.id, item_type=LearningItemType.RESOURCE, title="Legacy", body_md="body",
        spec={"academy_key": "level1-v2-day-2", "day": 2, "knowledge_check": [{"id": "q1", "answer": "a"}]}, reviewed=True,
    )
    with pytest.raises(NotFoundError):
        _svc(db).open_step(learner.id, legacy.id, "anything")
    with pytest.raises(NotFoundError):
        _svc(db).structure(legacy.id)
    assert db.query(AcademyStepProgress).count() == 0
    assert legacy.spec["knowledge_check"][0]["answer"] == "a"  # authored data untouched


def test_an_item_outside_the_academy_cannot_be_used_for_step_progress(db, learner):
    concept = make_concept(db, slug="not-academy", name="Not academy")
    make_published_version(db, concept=concept)
    spec = sample_spec()
    spec.pop("academy_key")
    stray = ConceptGraphService(db).create_learning_item(concept_id=concept.id, item_type=LearningItemType.RESOURCE, title="Stray", spec=spec, reviewed=True)
    with pytest.raises(NotFoundError):
        _svc(db).open_step(learner.id, stray.id, "what-is-ai")


def test_reprovisioning_a_level1_day_keeps_its_authored_steps(db):
    from app.services.academy_level1_service import AcademyLevel1Service, CANONICAL_SLUGS

    graph = ConceptGraphService(db)
    for slug in CANONICAL_SLUGS:
        concept = graph.create_concept(slug=slug, name=slug, level=ConceptLevel.FOUNDATIONAL, kind=ConceptKind.DEFINITIONAL)
        make_published_version(db, concept=concept)
    user = make_user(db)
    service = AcademyLevel1Service(db)
    service.provision(user)
    day1 = next(r for r in service._current_academy_items() if r.spec["day"] == 1)
    assert "steps" not in day1.spec  # existing items are non-structured until a later slice converts them
    structured = copy.deepcopy(day1.spec)
    structured.update({"step_schema_version": 1, "steps": sample_spec(day=1, kind="lecture", engine_binding=None)["steps"][:6]})
    structured["steps"] = [s for s in structured["steps"] if s["type"] != "lab"]
    day1.spec = structured
    db.commit()
    service.provision(user)
    again = next(r for r in service._current_academy_items() if r.spec["day"] == 1)
    assert again.id == day1.id
    assert [s["key"] for s in again.spec["steps"]] == [s["key"] for s in structured["steps"]]
    assert again.spec["academy_key"] == "level1-v2-day-1" and again.spec["knowledge_check"]
