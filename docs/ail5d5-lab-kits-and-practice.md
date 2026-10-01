# AIL.5D.5 — Educational Lab Kits, Personal Lab integration and independent practice

Status: implemented on `feature/ail5d5-lab-practice` (base `54ce0abd`, AIL5D.4). Not pushed, merged or deployed.

## 1. What existed (audit) and what was reused

| Existing infrastructure | Finding | D5 decision |
|---|---|---|
| **Test Kit** (`EvalSet` / `EvalSetVersion` / `EvalSetVersionTask`) | User-owned, professional starter kits only (Software Engineer, Code Reviewer). Not an educational concept. | Not extended. An *Educational Lab Kit* is a different thing (authored curriculum, below). |
| **Experiment** (`model_comparison`, `prompt_comparison`, `variance`) | One launch only; models attach at creation; no UI to create one. The old Day 4/5/9 `start-lab` built a `prompt_comparison` Experiment with **no models**, so it could neither launch nor count. | Days 4/5/9 no longer use it (they were unrunnable). The Experiment engine itself is untouched. |
| **Execution** (`TaskService.start_task_run`, Task Run / Agent Run / Artifact, `frozen_task_snapshot`, `model_policy_override`, `budget_id`) | Governed, recorded, budget-aware, model-registry-routed. | **The one execution engine.** Every practice run is an ordinary Task Run. No second engine. |
| **Budget** (`Budget`, `BudgetGovernor`) | Enforced only when a run carries a `budget_id`. | Each learner gets a USER-scope Budget for practice; every practice run carries it. |
| **Curriculum fixtures** (`academy_level1_fixtures._agent/_task`) | Curriculum-owned Agents/Tasks over the same engine. | Reused for the kit agent and task. |
| **AIL.5C / LearningEvidence / independence floor** | Practice marker + `demonstration_floor` already refuse practice as demonstration. | Reused unchanged. |
| **Professor D4** | Step-scoped, server-decided mode and ladder, help ledger. | Extended to lab/practice steps (below). |

## 2. Lab Kit architecture

```
Lab Kit (immutable curriculum)  ->  scenario  ->  Practice Instance (the learner's)  ->  governed Task Runs
app/academy_lab_kits.py             authored       academy_practice_instances            existing engine
```

* A **kit** is authored curriculum in `app/academy_lab_kits.py` (validated at import by `app/academy_lab_kit.py`). It is in the repository, reviewed like all curriculum, and has **no write path at runtime**: no table, no endpoint, no learner action can change it.
* A **scenario** authors the objective, instructions, prompt template, the *only* variables a learner may change (`text`, `choice`, or an authored `model`), required predictions, minimum runs, whether a change / comparison / observation / reflection is required, the run cap, the permitted models, and an optional private `reveal_md` lab answer.
* A **Practice Instance** is a learner's private copy of one scenario. It **freezes** the scenario (and its SHA-256) at creation, so a later kit revision never alters work in progress, and binds the exact Day version and step.
* No per-Day hard-coding: Days attach to a kit by **step binding** (`lab` with `binding.kit`, or a `practice` step). Only the legacy un-structured Days 4/5/9 use the small `DAY_KITS` map, because they have no steps yet.

Reference kits (drawn from the authored Day 4/5/9 labs, not rewriting them): `day4-ai-behavior-lab`, `day5-grounding-lab`, `day9-prompt-structure-lab`, each with a guided scenario and an independent practice scenario.

## 3. Guided contract

Predict → Run → Observe → Change one variable → Run again → Compare → Reflect. Guided scenarios require ≥ 1 prediction, an observation, and (per scenario) a change, comparison and reflection. The server enforces the order from recorded facts: predictions only before the first run; observations/comparisons must name the learner's own **completed** runs; a comparison needs a real change between runs; a reflection comes after the work. The Professor coaches under the D4 guided rules.

## 4. Independent contract and the step-type decision

**`practice` is a distinct step type** (not a mode on `lab`). Rationale: it is first-class in the Day (a learner can "Practice this concept" and repeat), it has different completion semantics (a completed *independent* instance, repeatable, drawn from a pool of scenarios), and the Professor mode is a property of the step type (`practice` ⇒ independent). The contract change is additive: step schema stays v1; `lab` gains an optional `binding.kit`; a kit-bound lab takes its predictions from the scenario.

Independent scenarios carry less guidance (no predictions or prompting questions), are safe to fail, and may be repeated. "Practice again" draws the scenario the learner has practised least, and attempts per step are capped (`academy_practice_max_attempts_per_step`, default 10). Hints are progressive (clarify → hint → stronger hint, never a full explanation on a lab), and every hint is recorded.

## 5. Instance lifecycle

`created → predicted → running → observing → comparing → reflecting → completed` (plus `abandoned`). Status is **server-owned, forward-only, derived from facts** (`academy_lab_kit.evaluate`): saved predictions, runs, completed runs, saved responses. No request carries a status. `completed` finalizes once: stamps completion, snapshots the canonical assistance, writes ONE practice evidence row, and completes the Day step (structured Days).

## 6. Handoff and return

`GET /academy/practice/{id}/handoff` returns every identifier the server holds (learner, Program/Day, LearningItem id/lineage/version, step key, kit/scenario, instance, permitted variables, run endpoint) and a **server-built return target** `#/academy/level-1/{day}?step={step}&practice={id}` that restores the exact Day, step and instance. A client-supplied return URL is rejected (`extra=forbid`) and never echoed. The Personal Lab *surface* consuming this contract is D6 UI work; the contract and the engine are done.

## 7. Persistence

* **Execution results** stay in the existing execution system (Task Run → Agent Run → Artifact). `academy_practice_runs` stores only the learner's validated variables and the link.
* **Learner writing** reuses `academy_step_responses` (kinds `prediction`, `observation`, `reflection`; a comparison is an `observation` with key `cmp`), keyed `{instance}:…`, referencing `practice_run` / `practice_instance` through `ref_type`/`ref_id`. Append-only, revisioned.
* **Professor help** stays in the separate `academy_professor_help` audit table (not mirrored into `academy_step_response`).

## 8. Professor and assistance

Lab and practice steps work with the D4 step-scoped Professor. The learner's own predictions, runs (variables and a bounded output preview) and notes are context; the authored lab answer never is (`solution_eligible` is false for lab/practice, and `reveal_md` is not in any Professor surface even after completion). A practice instance's assistance is **derived from canonical Professor-help records** for the step created during that instance (max H-level, hints used, whether a solution was shown). A learner cannot supply it (unknown fields are rejected). A legacy un-structured lab has no step-scoped ledger, so its practice evidence is recorded as H3 with `assistance_tracked: false` rather than claimed as independent. Learner-facing "Hints used" presentation is D6; the data is exposed in `assistance`.

## 9. Practice ≠ demonstration

Completion records one LAB evidence row with `score.practice = true` and `PLATFORM_VERIFIED`. `demonstration_floor` rejects it (`practice`), so it can contribute to PRACTICED and never to DEMONSTRATED; AIL.5C readiness, Grader, mode guard and evidence rules are untouched.

## 10. Budget and run controls (all server-side)

Per-scenario `max_runs` (copied to the instance; failed runs count) • per-learner daily cap (`academy_practice_daily_run_cap`, default 30) • per-learner USER-scope Budget (`academy_practice_budget_usd`, default 2.00) checked **before** any Task Run is created (`409 practice_budget_exhausted`) and carried on every run so the engine's reservation also enforces it • models only from the authored allow-list, pinned via a manual model policy; an authored model the registry lacks fails closed (`practice_model_unavailable`) • one run in flight at a time • no unlimited "Practice again" (attempt cap).

## 11. API

`POST /academy/practice` (create or resume) • `GET /academy/practice?learning_item_id&step_key` • `GET /academy/practice/{id}` • `POST …/{id}/responses` • `POST …/{id}/runs` • `GET …/{id}/handoff` • `POST …/{id}/abandon`. Bodies are `extra=forbid`. `POST /academy/level-1/days/{4|5|9}/start-lab` now returns `engine: academy_lab_kit` with `practice_instance_id`.

## 12. Schema

Migration `academy_practice_instances` (on `academy_professor_help`): `academy_practice_instances`, `academy_practice_runs`. Additive; reversible; refuses downgrade while practice rows exist.

## 13. Deferred to AIL5D.6

Production Workspace UI, hints-used presentation, the Personal Lab surface consuming the handoff, Program Overview UI, Days 2–30 conversion, Capstone, responsive/mobile, full UAT. Execution is asynchronous (the existing worker); a client polls `GET /academy/practice/{id}`.
