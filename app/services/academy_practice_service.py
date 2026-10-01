"""AIL.5D.5 — Educational Lab Kit Practice Instances over the existing governed execution engine.

    authored Lab Kit scenario (immutable curriculum)  ->  the learner's own Practice Instance  ->  governed Task Runs

There is NO second execution engine here. A run is an ordinary Task Run / Agent Run created by ``TaskService`` (so it uses the
platform's model registry, Flight Recorder, artifacts and ``BudgetGovernor``); this service decides only WHAT may be run
(the validated variables of an authored scenario), HOW MUCH (run caps and a per-learner Budget), and what the recorded facts
mean (the forward-only lifecycle). It trusts nothing from a client except the learner's own text and the variable values
the scenario permits:

* ownership comes from the authenticated learner; an instance of another learner does not exist for you (404);
* the model, the agent, the task, the prompt template, the caps and the budget are all server-side;
* completion is derived from persisted facts (saved responses + completed governed runs) and cannot be asserted;
* a return location is built by the server from the instance - a client-supplied URL is never accepted or echoed;
* the authored lab answer (``reveal_md``) is served only once the instance is completed.

Practice is not an assessment. Completing an instance records ONE practice-marked LAB evidence row that can contribute to
PRACTICED and, by the platform floor (``independence_policy``), can never contribute to DEMONSTRATED.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import academy_lab_kit as K
from app.academy_lab_kits import DAY_KITS, get_kit, get_scenario
from app.academy_steps import StepType, is_structured, public_steps
from app.config import settings
from app.db.enums import (
    AcademyStepResponseKind, AgentRunStatus, AssistanceLevel, BudgetScope, EvidenceRefType, EvidenceType, ExecutionMode,
    ExecutionVerification, GradingMode, PracticeInstanceStatus, PracticeMode,
)
from app.db.mixins import utcnow
from app.errors import ConflictError, InvalidResponseError, NotFoundError
from app.models.academy import AcademyPracticeInstance, AcademyPracticeRun, AcademyProfessorHelp, AcademyStepResponse
from app.models.agents import AgentVersion
from app.models.artifacts_eval import Artifact
from app.models.concepts import LearningItem
from app.models.governance import Budget
from app.models.identity import User
from app.models.providers import Model, ProviderModel
from app.models.tasks import AgentRun, TaskRun
from app.services.academy_level1_fixtures import _agent, _task
from app.services.academy_step_service import AcademyStepService, VerifiedCompletion, _Part
from app.services.budget_service import BudgetGovernor
from app.services.concept_graph_service import ConceptGraphService
from app.services.independence_policy import assistance_rank, with_practice_marker
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.system_project_service import ensure_ail_system_project

PRACTICE_NOTE = (
    "Practice is safe to fail and can be repeated. It builds skill and is recorded as practice - it is not an assessment and "
    "does not by itself demonstrate that you know this."
)
MIN_TEXT = 8
MAX_TEXT = 8000
OUTPUT_PREVIEW_CHARS = 6000
FINAL = (PracticeInstanceStatus.COMPLETED, PracticeInstanceStatus.ABANDONED)
_TERMINAL_FAILURES = (AgentRunStatus.FAILED, AgentRunStatus.STOPPED)
_RESPONSE_SUFFIX = {"observation": "obs", "comparison": "cmp", "reflection": "ref"}


def _conflict(code: str, message: str, **extra: Any) -> ConflictError:
    return ConflictError(message, detail={"code": code, **extra})


@dataclass(frozen=True)
class Target:
    """What a Day step (or a legacy lab Day) offers: which kit and which scenarios, in which mode."""

    step_key: str
    mode: PracticeMode
    kit_key: str
    scenario_keys: List[str]
    fingerprint: str
    structured: bool


class AcademyPracticeService:
    def __init__(self, db: Session, *, clock: Callable[[], datetime] = utcnow):
        self.db = db
        self._clock = clock
        self._graph = ConceptGraphService(db)
        self._steps = AcademyStepService(db, clock=clock)

    # -- resolution ----------------------------------------------------------------------------------------------------

    def _day(self, item_id: str) -> LearningItem:
        item = self.db.get(LearningItem, item_id)
        if item is None or not (item.spec or {}).get("academy_key"):
            raise NotFoundError("Academy learning item not found")
        current = self._graph.get_current_learning_item(item.lineage_id)
        if current is None or current.id != item.id:
            raise _conflict("stale_item_version", "This lesson has been updated. Reload to continue on the current version.",
                            current_item_id=current.id if current else None, current_version=current.version if current else None)
        return item

    def _target(self, item: LearningItem, step_key: str) -> Target:
        spec = item.spec or {}
        if is_structured(spec):
            step = next((s for s in public_steps(spec) if s["key"] == step_key), None)
            if step is None:
                raise NotFoundError("Step not found on this lesson")
            binding = step.get("binding") or {}
            if step["type"] == StepType.LAB.value and "kit" in binding:
                kit = binding["kit"]
                target = Target(step_key, PracticeMode.GUIDED, kit["kit_key"], [kit["scenario_key"]], step["fingerprint"], True)
            elif step["type"] == StepType.PRACTICE.value:
                target = Target(step_key, PracticeMode.INDEPENDENT, binding["kit_key"], list(binding["scenario_keys"]), step["fingerprint"], True)
            else:
                raise _conflict("step_not_practice", "This step is not a lab or practice step.", step_type=step["type"])
        else:
            day = spec.get("day")
            if spec.get("kind") != "lab" or day not in DAY_KITS or step_key not in ("lab", "practice"):
                raise NotFoundError("This lesson has no practice lab")
            kit_key, guided_key = DAY_KITS[day]
            kit = get_kit(kit_key)
            independent = [s["key"] for s in kit["lab_kit"]["scenarios"] if s["mode"] == "independent"]
            keys, mode = ([guided_key], PracticeMode.GUIDED) if step_key == "lab" else (independent, PracticeMode.INDEPENDENT)
            fingerprint = hashlib.sha256(f"legacy:{item.lineage_id}:{step_key}:{kit_key}:{','.join(keys)}".encode()).hexdigest()
            target = Target(step_key, mode, kit_key, keys, fingerprint, False)
        for key in target.scenario_keys:             # an authored binding to a kit that is not registered is a content error
            scenario = get_scenario(target.kit_key, key)
            if scenario is None or scenario["mode"] != target.mode.value:
                raise _conflict("practice_not_configured", "This practice is not available.")
        return target

    def _instance(self, user_id: str, instance_id: str) -> AcademyPracticeInstance:
        row = self.db.get(AcademyPracticeInstance, instance_id)
        if row is None or row.user_id != user_id:
            raise NotFoundError("Practice not found")
        return row

    def _item_of(self, inst: AcademyPracticeInstance) -> LearningItem:
        return self.db.get(LearningItem, inst.learning_item_id)

    def _current_item_of(self, inst: AcademyPracticeInstance) -> LearningItem:
        """The instance's Day item, which must still be the CURRENT version for anything that changes state."""
        return self._day(inst.learning_item_id)

    # -- budget --------------------------------------------------------------------------------------------------------

    def _budget(self, user: User) -> Budget:
        project = ensure_ail_system_project(self.db, user)
        budget = self.db.execute(select(Budget).where(
            Budget.project_id == project.id, Budget.scope == BudgetScope.USER, Budget.scope_ref_id == user.id)).scalars().first()
        if budget is None:
            budget = Budget(project_id=project.id, scope=BudgetScope.USER, scope_ref_id=user.id,
                            limit_amount=Decimal(str(settings.academy_practice_budget_usd)), currency="USD")
            self.db.add(budget)
            self.db.flush()
        return budget

    # -- creation ------------------------------------------------------------------------------------------------------

    def create(self, user: User, item_id: str, step_key: str) -> Dict[str, Any]:
        """The learner's practice for this step: the one already in progress, or a new instance. A new instance of an
        independent step draws the scenario this learner has practised least (so "practice again" is a fresh case)."""
        item = self._day(item_id)
        target = self._target(item, step_key)
        mine = list(self.db.execute(select(AcademyPracticeInstance).where(
            AcademyPracticeInstance.user_id == user.id, AcademyPracticeInstance.lineage_id == item.lineage_id,
            AcademyPracticeInstance.step_key == step_key).order_by(AcademyPracticeInstance.created_at)).scalars())
        active = next((r for r in reversed(mine) if r.status not in FINAL), None)
        if active is not None and active.scenario_key in target.scenario_keys and active.kit_key == target.kit_key:
            return self.view(user.id, active.id)
        if len(mine) >= settings.academy_practice_max_attempts_per_step:
            raise _conflict("practice_attempts_exhausted", "You have used all the practice attempts for this step.",
                            max_attempts=settings.academy_practice_max_attempts_per_step)
        used = {key: sum(1 for r in mine if r.scenario_key == key) for key in target.scenario_keys}
        scenario_key = min(target.scenario_keys, key=lambda k: (used[k], target.scenario_keys.index(k)))
        kit = get_kit(target.kit_key)
        scenario = get_scenario(target.kit_key, scenario_key)
        budget = self._budget(user)
        inst = AcademyPracticeInstance(
            user_id=user.id, learning_item_id=item.id, lineage_id=item.lineage_id, step_key=step_key, mode=target.mode,
            kit_key=target.kit_key, kit_version=kit["lab_kit"]["version"], scenario_key=scenario_key,
            scenario_sha256=K.scenario_fingerprint(scenario), scenario=json.loads(json.dumps(scenario)),
            status=PracticeInstanceStatus.CREATED, attempt_no=len(mine) + 1, max_runs=scenario["limits"]["max_runs"], budget_id=budget.id,
        )
        self.db.add(inst)
        self.db.commit()
        return self.view(user.id, inst.id)

    # -- facts ---------------------------------------------------------------------------------------------------------

    def _runs(self, inst: AcademyPracticeInstance) -> List[AcademyPracticeRun]:
        return list(self.db.execute(select(AcademyPracticeRun).where(AcademyPracticeRun.instance_id == inst.id)
                                    .order_by(AcademyPracticeRun.seq)).scalars())

    def _run_state(self, run: AcademyPracticeRun) -> Dict[str, Any]:
        agent_run = self.db.get(AgentRun, run.agent_run_id)
        status = agent_run.status if agent_run is not None else AgentRunStatus.FAILED
        output = None
        if status == AgentRunStatus.COMPLETED:
            artifact = self.db.execute(select(Artifact).where(Artifact.agent_run_id == run.agent_run_id)
                                       .order_by(Artifact.created_at.desc())).scalars().first()
            if artifact is not None and artifact.storage_ref and Path(artifact.storage_ref).exists():
                text = Path(artifact.storage_ref).read_text(encoding="utf-8", errors="replace")
                output = text[:OUTPUT_PREVIEW_CHARS] if text.strip() else None
        state = "completed" if status == AgentRunStatus.COMPLETED and output is not None else \
            "failed" if status in _TERMINAL_FAILURES or (status == AgentRunStatus.COMPLETED and output is None) else "running"
        return {"seq": run.seq, "run_id": run.id, "variables": dict(run.variables), "state": state, "output": output,
                "model": run.model_canonical_id, "created_at": run.created_at.isoformat(),
                "agent_run_id": run.agent_run_id, "task_run_id": run.task_run_id}

    def _responses(self, inst: AcademyPracticeInstance) -> Dict[str, Any]:
        prefix = inst.id.replace("-", "") + ":"
        rows = self.db.execute(select(AcademyStepResponse).where(
            AcademyStepResponse.user_id == inst.user_id, AcademyStepResponse.learning_item_id == inst.learning_item_id,
            AcademyStepResponse.step_key == inst.step_key, AcademyStepResponse.response_key.like(prefix + "%"))
            .order_by(AcademyStepResponse.created_at, AcademyStepResponse.revision)).scalars().all()
        latest: Dict[str, AcademyStepResponse] = {}
        for row in rows:
            latest[row.response_key[len(prefix):]] = row
        return {"predictions": {k[5:]: dict(v.content) for k, v in latest.items() if k.startswith("pred-")},
                **{name: dict(latest[suffix].content) for name, suffix in _RESPONSE_SUFFIX.items() if suffix in latest}}

    def _facts(self, inst: AcademyPracticeInstance, runs: List[Dict[str, Any]], responses: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "predictions": set(responses["predictions"]), "runs_started": len(runs),
            "completed_runs": [r["variables"] for r in runs if r["state"] == "completed"],
            "observation": "observation" in responses, "comparison": "comparison" in responses, "reflection": "reflection" in responses,
        }

    # -- assistance (canonical: derived from Professor help records, never client-supplied) ------------------------------

    def assistance(self, inst: AcademyPracticeInstance) -> Dict[str, Any]:
        item = self._item_of(inst)
        tracked = is_structured(item.spec or {})
        until = inst.completed_at or inst.abandoned_at or self._clock()
        rows: List[AcademyProfessorHelp] = list(self.db.execute(select(AcademyProfessorHelp).where(
            AcademyProfessorHelp.user_id == inst.user_id, AcademyProfessorHelp.lineage_id == inst.lineage_id,
            AcademyProfessorHelp.step_key == inst.step_key, AcademyProfessorHelp.created_at >= inst.created_at,
            AcademyProfessorHelp.created_at <= until).order_by(AcademyProfessorHelp.created_at)).scalars()) if tracked else []
        levels = [r.assistance_level for r in rows if r.assistance_level is not None]
        top = max(levels, key=lambda lv: assistance_rank(lv)) if levels else None
        return {
            "tracked": tracked, "hints_used": sum(1 for r in rows if r.request_kind.value == "hint"),
            "help_count": len(rows), "max_level": top.value if top else ("h0" if tracked else None),
            "revealed_solution": any(r.revealed_solution for r in rows),
        }

    # -- the learner's view --------------------------------------------------------------------------------------------

    def _limits(self, user_id: str, inst: AcademyPracticeInstance, runs_started: int) -> Dict[str, Any]:
        since = self._clock().replace(hour=0, minute=0, second=0, microsecond=0)
        today = self.db.execute(select(func.count()).select_from(AcademyPracticeRun).where(
            AcademyPracticeRun.user_id == user_id, AcademyPracticeRun.created_at >= since)).scalar() or 0
        budget = self.db.get(Budget, inst.budget_id) if inst.budget_id else None
        state = BudgetGovernor(self.db).threshold_state(budget) if budget is not None else "ok"
        return {"max_runs": inst.max_runs, "runs_used": runs_started, "runs_left": max(inst.max_runs - runs_started, 0),
                "daily_run_cap": settings.academy_practice_daily_run_cap, "daily_runs_used": today,
                "budget_state": state, "budget_exhausted": state == "hard"}

    def return_target(self, item: LearningItem, inst: AcademyPracticeInstance) -> Dict[str, Any]:
        """Where "back to the lesson" goes. Built here from the instance; no client value is ever used."""
        day = (item.spec or {}).get("day")
        path = f"#/academy/level-1/{day}?step={inst.step_key}&practice={inst.id}"
        return {"day": day, "learning_item_id": item.id, "step_key": inst.step_key, "practice_instance_id": inst.id, "path": path}

    def view(self, user_id: str, instance_id: str, *, sync: bool = True) -> Dict[str, Any]:
        inst = self._instance(user_id, instance_id)
        item = self._item_of(inst)
        scenario = inst.scenario
        runs = [self._run_state(r) for r in self._runs(inst)]
        responses = self._responses(inst)
        evaluation = K.evaluate(scenario, self._facts(inst, runs, responses))
        if sync and inst.status not in FINAL:
            self._advance(inst, evaluation["status"])
        completed = inst.status == PracticeInstanceStatus.COMPLETED
        public = K.public_scenario(scenario)
        out = {
            "instance_id": inst.id, "mode": inst.mode.value, "status": inst.status.value, "attempt_no": inst.attempt_no,
            "kit": {"key": inst.kit_key, "version": inst.kit_version}, "scenario": public,
            "step_key": inst.step_key, "learning_item_id": inst.learning_item_id, "lineage_id": inst.lineage_id,
            "met": evaluation["met"], "next_required": evaluation["next_required"],
            "runs": runs, "responses": responses, "limits": self._limits(user_id, inst, len(runs)),
            "assistance": self.assistance(inst), "return": self.return_target(item, inst),
            "completed_at": inst.completed_at.isoformat() if inst.completed_at else None,
            "evidence_recorded": inst.evidence_id is not None, "note": PRACTICE_NOTE,
        }
        if len(runs) >= 2:
            out["comparison_view"] = self.compare(runs)
        if completed and scenario.get("reveal_md"):
            out["lab_answer_md"] = scenario["reveal_md"]
        return out

    @staticmethod
    def compare(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """A side-by-side of the first and latest completed runs: which variables differ, and the two outputs."""
        done = [r for r in runs if r["state"] == "completed"]
        if len(done) < 2:
            return {"available": False}
        a, b = done[0], done[-1]
        return {"available": True, "changed": sorted(k for k in b["variables"] if b["variables"].get(k) != a["variables"].get(k)),
                "first": {"seq": a["seq"], "variables": a["variables"], "output": a["output"]},
                "latest": {"seq": b["seq"], "variables": b["variables"], "output": b["output"]}}

    def for_step(self, user_id: str, item_id: str, step_key: str) -> Dict[str, Any]:
        item = self.db.get(LearningItem, item_id)
        if item is None or not (item.spec or {}).get("academy_key"):
            raise NotFoundError("Academy learning item not found")
        rows = self.db.execute(select(AcademyPracticeInstance).where(
            AcademyPracticeInstance.user_id == user_id, AcademyPracticeInstance.lineage_id == item.lineage_id,
            AcademyPracticeInstance.step_key == step_key).order_by(AcademyPracticeInstance.created_at)).scalars().all()
        return {"step_key": step_key, "max_attempts": settings.academy_practice_max_attempts_per_step, "instances": [
            {"instance_id": r.id, "mode": r.mode.value, "status": r.status.value, "scenario_key": r.scenario_key, "attempt_no": r.attempt_no,
             "completed_at": r.completed_at.isoformat() if r.completed_at else None} for r in rows]}

    # -- lifecycle -----------------------------------------------------------------------------------------------------

    def _advance(self, inst: AcademyPracticeInstance, target: PracticeInstanceStatus) -> None:
        new = K.advance(inst.status, target)
        if new == inst.status:
            return
        inst.status = new
        if new == PracticeInstanceStatus.COMPLETED:
            self._finalize(inst)
        self.db.commit()

    def _finalize(self, inst: AcademyPracticeInstance) -> None:
        """Completion: stamp it, snapshot the canonical assistance, record ONE practice evidence row, and complete the
        Day step (structured Days only). Idempotent: an instance with evidence is never written twice."""
        inst.completed_at = self._clock()
        inst.assistance = self.assistance(inst)
        item = self._item_of(inst)
        runs = [r for r in self._runs(inst) if self._run_state(r)["state"] == "completed"]
        if inst.evidence_id is None and runs:
            version = self._graph.get_current_version(item.concept_id)
            if version is not None:
                tracked = inst.assistance["tracked"]
                level = AssistanceLevel(inst.assistance["max_level"]) if tracked else AssistanceLevel.H3   # untracked help is never claimed as H0
                evidence = LearningEvidenceService(self.db).record_evidence(
                    user_id=inst.user_id, concept_id=item.concept_id, concept_version_id=version.id, learning_item_id=item.id,
                    evidence_type=EvidenceType.LAB, grader=GradingMode.DETERMINISTIC, passed=True,
                    ref_type=EvidenceRefType.AGENT_RUN, ref_id=runs[-1].agent_run_id,
                    score=with_practice_marker({"practice_instance_id": inst.id, "kit_key": inst.kit_key, "scenario_key": inst.scenario_key,
                                                "practice_mode": inst.mode.value, "runs": len(runs), "assistance_tracked": tracked}, True),
                    assistance_level=level, execution_verification=ExecutionVerification.PLATFORM_VERIFIED, commit=False)
                inst.evidence_id = evidence.id
        self.db.flush()
        if is_structured(item.spec or {}):
            self.db.commit()
            self._steps.complete_step(inst.user_id, item.id, inst.step_key, verified=VerifiedCompletion("practice_completed", inst.id))

    def abandon(self, user_id: str, instance_id: str) -> Dict[str, Any]:
        inst = self._instance(user_id, instance_id)
        if inst.status not in FINAL:
            inst.status = PracticeInstanceStatus.ABANDONED
            inst.abandoned_at = self._clock()
            self.db.commit()
        return self.view(user_id, instance_id, sync=False)

    # -- responses -----------------------------------------------------------------------------------------------------

    def _text(self, payload: Any, what: str) -> str:
        if not isinstance(payload, dict):
            raise InvalidResponseError("a response is required")
        extra = sorted(set(payload) - {"text", "prediction_id", "run_ids"})
        if extra:
            raise InvalidResponseError(f"unexpected field(s): {', '.join(extra)}")
        text = payload.get("text")
        if not isinstance(text, str) or len(text.strip()) < MIN_TEXT:
            raise InvalidResponseError(f"{what} needs at least {MIN_TEXT} characters")
        if len(text.strip()) > MAX_TEXT:
            raise InvalidResponseError(f"{what} must be at most {MAX_TEXT} characters")
        return text.strip()

    def respond(self, user: User, instance_id: str, kind: str, payload: Any) -> Dict[str, Any]:
        """Save a prediction, observation, comparison or reflection. Ordering is enforced from recorded facts: predictions
        come before the first run, observations need a completed run, comparisons need two (and name them), reflections come
        after the work they reflect on."""
        inst = self._instance(user.id, instance_id)
        item = self._current_item_of(inst)
        if inst.status in FINAL:
            raise _conflict("practice_not_active", "This practice is finished.")
        scenario = inst.scenario
        runs = [self._run_state(r) for r in self._runs(inst)]
        responses = self._responses(inst)
        facts = self._facts(inst, runs, responses)
        met = K.evaluate(scenario, facts)["met"]
        done = {r["run_id"]: r for r in runs if r["state"] == "completed"}
        text = self._text(payload, "your response")
        ref_id: Optional[str] = None
        if kind == "prediction":
            pid = payload.get("prediction_id")
            if pid not in {p["id"] for p in scenario.get("predictions", [])}:
                raise InvalidResponseError("that is not a prediction of this practice")
            if runs:
                raise _conflict("prediction_after_run", "A prediction must be made before you run anything.")
            part = _Part(AcademyStepResponseKind.PREDICTION, f"{inst.id.replace('-', '')}:pred-{pid}", {"text": text, "prediction_id": pid})
        elif kind in ("observation", "comparison"):
            ids = payload.get("run_ids")
            need = 2 if kind == "comparison" else 1
            if not isinstance(ids, list) or len(ids) < need or len(set(ids)) != len(ids) or len(ids) > inst.max_runs or not all(isinstance(i, str) for i in ids):
                raise InvalidResponseError(f"name the {need} or more of your own completed runs this refers to")
            if any(i not in done for i in ids):
                raise InvalidResponseError("those runs are not completed runs of this practice")
            if kind == "comparison" and not met["change"]:
                raise _conflict("change_required", "Change one thing between runs before you compare them.")
            part = _Part(AcademyStepResponseKind.OBSERVATION, f"{inst.id.replace('-', '')}:{_RESPONSE_SUFFIX[kind]}", {"text": text, "run_ids": ids})
            ref_id = ids[-1]
        elif kind == "reflection":
            if not met["runs"] or not met["change"]:
                raise _conflict("work_required", "Finish your runs before you reflect.")
            if scenario["requirements"].get("require_observation") and not facts["observation"]:
                raise _conflict("observation_required", "Record your observation before you reflect.")
            if scenario["requirements"].get("require_comparison") and not facts["comparison"]:
                raise _conflict("comparison_required", "Compare your runs before you reflect.")
            part = _Part(AcademyStepResponseKind.REFLECTION, f"{inst.id.replace('-', '')}:ref", {"text": text})
        else:
            raise InvalidResponseError("kind must be prediction, observation, comparison or reflection")
        step = {"key": inst.step_key, "fingerprint": inst.scenario_sha256 if not is_structured(item.spec or {}) else self._target(item, inst.step_key).fingerprint}
        self._steps._append(user.id, item, step, [part], ref_type="practice_run" if ref_id else "practice_instance", ref_id=ref_id or inst.id)
        return self.view(user.id, instance_id)

    # -- runs ----------------------------------------------------------------------------------------------------------

    def _resolve_model(self, scenario: dict, variables: Dict[str, str]) -> Optional[dict]:
        """The server's model policy for a run, or None (the kit agent's own auto policy). Only an authored model, chosen
        through an authored model variable or the scenario's allow-list, can ever be pinned."""
        declared = {v["key"]: v for v in scenario.get("variables", [])}
        chosen = next((variables[k] for k, v in declared.items() if v["kind"] == "model"), None)
        allowed = scenario.get("allowed_models") or []
        canonical = chosen or (allowed[0] if allowed else None)
        if canonical is None:
            return None
        pm = self.db.execute(select(ProviderModel).join(Model, Model.id == ProviderModel.model_id)
                             .where(Model.canonical_model_id == canonical)).scalars().first()
        if pm is None:
            raise _conflict("practice_model_unavailable", "The model for this practice is not available right now.")
        return {"mode": "manual", "manual_provider_model_id": pm.id}, canonical

    def start_run(self, user: User, instance_id: str, variables: Any) -> Dict[str, Any]:
        inst = self._instance(user.id, instance_id)
        item = self._current_item_of(inst)
        if inst.status in FINAL:
            raise _conflict("practice_not_active", "This practice is finished.")
        scenario = inst.scenario
        existing = self._runs(inst)
        states = [self._run_state(r) for r in existing]
        responses = self._responses(inst)
        if scenario.get("predictions") and not {p["id"] for p in scenario["predictions"]} <= set(responses["predictions"]):
            raise _conflict("predictions_required", "Make your predictions before you run anything.")
        if any(s["state"] == "running" for s in states):
            raise _conflict("run_in_progress", "Wait for your current run to finish.")
        if len(existing) >= inst.max_runs:
            raise _conflict("practice_run_limit", "You have used every run of this practice.", max_runs=inst.max_runs)
        limits = self._limits(user.id, inst, len(existing))
        if limits["daily_runs_used"] >= settings.academy_practice_daily_run_cap:
            raise _conflict("practice_daily_limit", "You have reached today's practice run limit. Try again tomorrow.",
                            daily_run_cap=settings.academy_practice_daily_run_cap)
        if limits["budget_exhausted"]:
            raise _conflict("practice_budget_exhausted", "Your practice budget is used up.")
        try:
            values = K.validate_variables(scenario, variables)
            prompt = K.render_prompt(scenario, values)
        except K.VariableError as exc:
            raise InvalidResponseError(str(exc)) from exc
        resolved = self._resolve_model(scenario, values)
        policy, canonical = resolved if resolved else (None, None)

        kit = get_kit(inst.kit_key)["lab_kit"]
        agent_spec = kit["agent"]
        agent_version: AgentVersion = _agent(self.db, user, name=agent_spec["name"], role=agent_spec["role"],
                                             description=f"Academy Lab Kit agent ({kit['title']}); immutable curriculum fixture.", prompt=agent_spec["prompt"])
        project = ensure_ail_system_project(self.db, user)
        task = _task(self.db, user, project_id=project.id, title=f"Academy Lab Kit: {kit['title']}", fixture=f"academy-lab-kit:{kit['key']}",
                     requirements={"kit_key": kit["key"], "kit_version": kit["version"]}, mode=ExecutionMode.SINGLE_AGENT)
        from app.services.task_service import TaskService

        seq = len(existing) + 1
        snapshot = {"title": f"{kit['title']} - run {seq}", "description": prompt, "requirements": None, "practice": True,
                    "practice_instance_id": inst.id, "academy_learning_item_id": item.id, "kit_key": kit["key"], "scenario_key": inst.scenario_key}
        task_run: TaskRun = TaskService(self.db).start_task_run(
            task_id=task.id, agent_version_id=agent_version.id, budget_id=inst.budget_id, model_policy_override=policy, frozen_task_snapshot=snapshot)
        run = AcademyPracticeRun(instance_id=inst.id, user_id=user.id, seq=seq, variables=values,
                                 prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(), model_canonical_id=canonical,
                                 task_run_id=task_run.id, agent_run_id=task_run.agent_runs[0].id)
        self.db.add(run)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise _conflict("run_in_progress", "Another run was started at the same moment. Reload and try again.")
        return self.view(user.id, instance_id)

    # -- handoff (Academy <-> Personal Lab) ----------------------------------------------------------------------------------

    def handoff(self, user_id: str, instance_id: str) -> Dict[str, Any]:
        """What the Personal Lab surface needs to run THIS practice and to bring the learner back to it: every identifier the
        server holds for the instance, the variables the learner may change, and the server-built return target. Nothing here
        is accepted back from a client."""
        inst = self._instance(user_id, instance_id)
        item = self._item_of(inst)
        scenario = K.public_scenario(inst.scenario)
        return {
            "engine": "personal_lab_task_run", "learner_id": user_id, "program": "practical-ai-foundations",
            "day": (item.spec or {}).get("day"), "learning_item_id": item.id, "learning_item_lineage_id": item.lineage_id,
            "learning_item_version": item.version, "step_key": inst.step_key, "kit": {"key": inst.kit_key, "version": inst.kit_version},
            "scenario_key": inst.scenario_key, "practice_instance_id": inst.id, "mode": inst.mode.value,
            "permitted_variables": scenario.get("variables", []),
            "max_runs": inst.max_runs, "run_endpoint": f"/academy/practice/{inst.id}/runs", "return": self.return_target(item, inst),
        }

    # -- Professor context (guided coaching / independent hints) -----------------------------------------------------------

    def professor_state(self, user_id: str, item: LearningItem, step_key: str) -> Optional[Dict[str, Any]]:
        """The learner's current practice on this step for the step-scoped Professor: what they predicted, ran and wrote.
        Never the authored lab answer (it is not in ``view`` until the instance is completed, and the Professor does not
        receive it at all)."""
        inst = self.db.execute(select(AcademyPracticeInstance).where(
            AcademyPracticeInstance.user_id == user_id, AcademyPracticeInstance.lineage_id == item.lineage_id,
            AcademyPracticeInstance.step_key == step_key).order_by(AcademyPracticeInstance.created_at.desc())).scalars().first()
        if inst is None:
            return None
        view = self.view(user_id, inst.id, sync=False)
        return {"mode": view["mode"], "status": view["status"], "next_required": view["next_required"],
                "objective_md": view["scenario"]["objective_md"], "instructions_md": view["scenario"]["instructions_md"],
                "predictions": view["responses"]["predictions"],
                "runs": [{"seq": r["seq"], "variables": r["variables"], "state": r["state"], "output": (r["output"] or "")[:1200]} for r in view["runs"]],
                "observation": view["responses"].get("observation"), "comparison": view["responses"].get("comparison"),
                "reflection": view["responses"].get("reflection")}

    # -- step verification (used by AcademyStepService) ---------------------------------------------------------------------

    def verified_completion(self, user_id: str, item: LearningItem, step: dict) -> Optional[VerifiedCompletion]:
        want_mode = PracticeMode.GUIDED if step["type"] == StepType.LAB.value else PracticeMode.INDEPENDENT
        row = self.db.execute(select(AcademyPracticeInstance).where(
            AcademyPracticeInstance.user_id == user_id, AcademyPracticeInstance.lineage_id == item.lineage_id,
            AcademyPracticeInstance.step_key == step["key"], AcademyPracticeInstance.mode == want_mode,
            AcademyPracticeInstance.status == PracticeInstanceStatus.COMPLETED).order_by(AcademyPracticeInstance.completed_at.desc())).scalars().first()
        return VerifiedCompletion("practice_completed", row.id) if row is not None else None
