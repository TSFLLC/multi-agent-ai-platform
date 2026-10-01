"""AIL.5D.5 — guided and independent practice over the governed engine, end to end through the HTTP surface."""

import json

import pytest

from app import academy_lab_kits as kits
from app.auth import get_current_user
from app.db.enums import EvidenceType, PracticeInstanceStatus
from app.main import app as fastapi_app
from app.models.academy import AcademyPracticeInstance, AcademyPracticeRun, AcademyStepProgress
from app.models.learner import LearningEvidence
from app.models.tasks import TaskRun
from app.services.independence_policy import is_practice
from tests.ail5d5_factories import GUIDED, INDEPENDENT, KIT, LAB_STEP, PRACTICE_STEP, finish_run, make_lab_day

P = "/academy/practice"
REVEAL_FRAGMENT = "the lens served 1891-1974"
PREDICTIONS = {"accuracy": "I predict the grounded model will be much more accurate.", "unknown": "FALSE - models often guess instead of admitting it.", "errors": "At least one error in five grounded answers."}
NOTE = "The grounded answer quoted the document; the ungrounded one guessed."


@pytest.fixture()
def day(db, bootstrap):
    return make_lab_day(db)


def _create(client, headers, day, step=LAB_STEP):
    return client.post(P, headers=headers, json={"learning_item_id": day.id, "step_key": step})


def _predict(client, headers, iid, which=None):
    for pid, text in PREDICTIONS.items():
        if which in (None, pid):
            r = client.post(f"{P}/{iid}/responses", headers=headers, json={"kind": "prediction", "prediction_id": pid, "text": text})
            assert r.status_code == 200, r.text


def _run(client, headers, iid, **variables):
    return client.post(f"{P}/{iid}/runs", headers=headers, json={"variables": variables})


def _last_run_id(body):
    return body["runs"][-1]["run_id"]


def _guided_to_completion(client, headers, db, tmp_path, day):
    iid = _create(client, headers, day).json()["instance_id"]
    _predict(client, headers, iid)
    a = _run(client, headers, iid, source="none", question="q-opened").json()
    finish_run(db, tmp_path, _last_run_id(a), "I think 1970.")
    b = _run(client, headers, iid, source="document", question="q-opened").json()
    finish_run(db, tmp_path, _last_run_id(b), "1968, per the document.")
    runs = [r["run_id"] for r in client.get(f"{P}/{iid}", headers=headers).json()["runs"]]
    assert client.post(f"{P}/{iid}/responses", headers=headers, json={"kind": "observation", "text": NOTE, "run_ids": runs}).status_code == 200
    assert client.post(f"{P}/{iid}/responses", headers=headers, json={"kind": "comparison", "text": NOTE + " Mechanism: the source.", "run_ids": runs}).status_code == 200
    final = client.post(f"{P}/{iid}/responses", headers=headers, json={"kind": "reflection", "text": "Grounding ties the answer to supplied text."})
    assert final.status_code == 200
    return iid, final.json()


# -- guided lifecycle -------------------------------------------------------------------------------------------------------------


def test_a_guided_practice_walks_predict_run_observe_change_rerun_compare_reflect_and_completes(client, auth_headers, db, tmp_path, day):
    created = _create(client, auth_headers, day)
    assert created.status_code == 201
    body = created.json()
    assert body["mode"] == "guided" and body["status"] == "created" and body["next_required"][0] == "prediction"
    iid = body["instance_id"]

    assert _run(client, auth_headers, iid, source="none", question="q-opened").status_code == 409   # predict first
    _predict(client, auth_headers, iid, "accuracy")
    assert client.get(f"{P}/{iid}", headers=auth_headers).json()["status"] == "created"             # one of two predictions
    _predict(client, auth_headers, iid, "unknown")
    assert client.get(f"{P}/{iid}", headers=auth_headers).json()["status"] == "created"             # two of three
    _predict(client, auth_headers, iid, "errors")
    assert client.get(f"{P}/{iid}", headers=auth_headers).json()["status"] == "predicted"

    first = _run(client, auth_headers, iid, source="none", question="q-opened")
    assert first.status_code == 201 and first.json()["status"] == "running" and first.json()["runs"][0]["state"] == "running"
    blocked = _run(client, auth_headers, iid, source="document", question="q-opened")
    assert blocked.status_code == 409 and blocked.json()["error"]["detail"]["code"] == "run_in_progress"
    finish_run(db, tmp_path, _last_run_id(first.json()), "I think 1970.")
    seen = client.get(f"{P}/{iid}", headers=auth_headers).json()
    assert seen["status"] == "observing" and seen["runs"][0]["output"] == "I think 1970."

    second = _run(client, auth_headers, iid, source="document", question="q-opened")
    finish_run(db, tmp_path, _last_run_id(second.json()), "1968, per the document.")
    seen = client.get(f"{P}/{iid}", headers=auth_headers).json()
    assert seen["status"] == "comparing" and seen["comparison_view"]["changed"] == ["source"] and "lab_answer_md" not in seen
    ids = [r["run_id"] for r in seen["runs"]]

    assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "reflection", "text": "Too early to reflect on this."}).status_code == 409
    assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "observation", "text": NOTE, "run_ids": ids}).status_code == 200
    assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "comparison", "text": NOTE + " Why: a source.", "run_ids": ids}).status_code == 200
    assert client.get(f"{P}/{iid}", headers=auth_headers).json()["status"] == "reflecting"
    done = client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "reflection", "text": "Grounding ties the answer to supplied text."}).json()
    assert done["status"] == "completed" and done["next_required"] == [] and REVEAL_FRAGMENT in done["lab_answer_md"]
    assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "reflection", "text": "Another reflection afterwards."}).status_code == 409


def test_completion_completes_the_day_step_and_records_exactly_one_practice_evidence_row(client, auth_headers, db, tmp_path, day, bootstrap):
    iid, done = _guided_to_completion(client, auth_headers, db, tmp_path, day)
    assert done["evidence_recorded"] is True
    row = db.query(AcademyStepProgress).filter_by(user_id=bootstrap.user.id, step_key=LAB_STEP).one()
    assert row.status.value == "completed" and row.completion_basis["kind"] == "practice_completed" and row.completion_basis["reference"] == iid
    evidence = db.query(LearningEvidence).filter_by(user_id=bootstrap.user.id, evidence_type=EvidenceType.LAB).all()
    assert len(evidence) == 1 and is_practice(evidence[0]) and evidence[0].score["practice_instance_id"] == iid
    client.get(f"{P}/{iid}", headers=auth_headers)           # reading again never writes a second row
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LAB).count() == 1


def test_a_failed_run_does_not_count_and_the_learner_can_run_again(client, auth_headers, db, tmp_path, day):
    from tests.ail5d5_factories import fail_run

    iid = _create(client, auth_headers, day).json()["instance_id"]
    _predict(client, auth_headers, iid)
    first = _run(client, auth_headers, iid, source="none", question="q-opened").json()
    fail_run(db, _last_run_id(first))
    seen = client.get(f"{P}/{iid}", headers=auth_headers).json()
    assert seen["runs"][0]["state"] == "failed" and seen["status"] == "running" and seen["limits"]["runs_used"] == 1
    assert _run(client, auth_headers, iid, source="document", question="q-opened").status_code == 201


def test_the_instance_resumes_and_the_return_target_is_built_by_the_server(client, auth_headers, db, day):
    a = _create(client, auth_headers, day).json()
    again = _create(client, auth_headers, day).json()
    assert again["instance_id"] == a["instance_id"]
    back = a["return"]
    assert back == {"day": 5, "learning_item_id": day.id, "step_key": LAB_STEP, "practice_instance_id": a["instance_id"],
                    "path": f"#/academy/level-1/5?step={LAB_STEP}&practice={a['instance_id']}"}
    handoff = client.get(f"{P}/{a['instance_id']}/handoff", headers=auth_headers).json()
    assert handoff["learning_item_id"] == day.id and handoff["learning_item_lineage_id"] == day.lineage_id and handoff["learning_item_version"] == 1
    assert handoff["step_key"] == LAB_STEP and handoff["kit"] == {"key": KIT, "version": 1} and handoff["scenario_key"] == GUIDED
    assert handoff["practice_instance_id"] == a["instance_id"] and handoff["return"] == back
    assert {v["key"] for v in handoff["permitted_variables"]} == {"source", "question"}
    assert REVEAL_FRAGMENT not in json.dumps(handoff) and "reveal_md" not in json.dumps(handoff)


# -- independent practice -----------------------------------------------------------------------------------------------------------


def test_independent_practice_has_less_guidance_is_repeatable_and_rotates_scenarios(client, auth_headers, db, tmp_path, bootstrap):
    from tests.ail5d5_factories import kit_with, lab_steps

    def add(kit):
        extra = json.loads(json.dumps(next(s for s in kit["scenarios"] if s["key"] == INDEPENDENT)))
        extra["key"] = "practice-second-case"
        kit["scenarios"].append(extra)

    kit_with(add, key="rotation-kit")
    try:
        day = make_lab_day(db, steps=lab_steps(kit="rotation-kit", practice_pool=(INDEPENDENT, "practice-second-case")))
        first = _create(client, auth_headers, day, PRACTICE_STEP).json()
        assert first["mode"] == "independent" and first["scenario"]["key"] == INDEPENDENT
        assert first["scenario"]["instructions_md"] and "predictions" not in first["scenario"] or not first["scenario"].get("predictions")
        assert first["next_required"] == ["runs", "change"]
        for variables in ({"source": "none", "question": "q-admission"}, {"source": "document-edited", "question": "q-admission"}):
            body = _run(client, auth_headers, first["instance_id"], **variables).json()
            finish_run(db, tmp_path, _last_run_id(body), "I cannot answer this from the provided document.")
        done = client.get(f"{P}/{first['instance_id']}", headers=auth_headers).json()
        assert done["status"] == "completed" and done["evidence_recorded"] is True           # no predictions, no reflection required
        second = _create(client, auth_headers, day, PRACTICE_STEP).json()                    # "practice again"
        assert second["instance_id"] != first["instance_id"] and second["scenario"]["key"] == "practice-second-case" and second["attempt_no"] == 2
        listing = client.get(P, headers=auth_headers, params={"learning_item_id": day.id, "step_key": PRACTICE_STEP}).json()
        assert [i["status"] for i in listing["instances"]] == ["completed", "created"]
    finally:
        kits.KITS.pop("rotation-kit", None)


def test_practice_again_is_bounded(client, auth_headers, db, day, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "academy_practice_max_attempts_per_step", 3)
    for _ in range(3):
        body = _create(client, auth_headers, day, PRACTICE_STEP).json()
        client.post(f"{P}/{body['instance_id']}/abandon", headers=auth_headers)
    stop = _create(client, auth_headers, day, PRACTICE_STEP)
    assert stop.status_code == 409 and stop.json()["error"]["detail"]["code"] == "practice_attempts_exhausted"


def test_an_abandoned_practice_is_final_and_never_completes(client, auth_headers, db, tmp_path, day, bootstrap):
    iid = _create(client, auth_headers, day).json()["instance_id"]
    assert client.post(f"{P}/{iid}/abandon", headers=auth_headers).json()["status"] == "abandoned"
    assert _run(client, auth_headers, iid, source="none", question="q-opened").status_code == 409
    assert client.post(f"{P}/{iid}/responses", headers=auth_headers, json={"kind": "prediction", "prediction_id": "accuracy", "text": PREDICTIONS["accuracy"]}).status_code == 409
    assert db.query(LearningEvidence).filter_by(evidence_type=EvidenceType.LAB).count() == 0


def test_a_practice_step_completes_only_from_a_completed_independent_instance(client, auth_headers, db, tmp_path, day, bootstrap):
    url = f"/academy/level-1/items/{day.id}/steps/{PRACTICE_STEP}/complete"
    assert client.post(url, headers=auth_headers).status_code == 409
    body = _create(client, auth_headers, day, PRACTICE_STEP).json()
    for variables in ({"source": "none", "question": "q-admission"}, {"source": "document-edited", "question": "q-admission"}):
        r = _run(client, auth_headers, body["instance_id"], **variables).json()
        finish_run(db, tmp_path, _last_run_id(r), "Unknown.")
    client.get(f"{P}/{body['instance_id']}", headers=auth_headers)
    row = db.query(AcademyStepProgress).filter_by(user_id=bootstrap.user.id, step_key=PRACTICE_STEP).one()
    assert row.status.value == "completed" and row.completion_basis["kind"] == "practice_completed"
