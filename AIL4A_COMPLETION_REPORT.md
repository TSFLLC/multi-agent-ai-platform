# AIL.4A status

Personal Stay-Ahead Today: a read-only `GET /stay-ahead/today` and a "Stay ahead" block on Today. Everything is derived on read from existing records. There is no schema change, no write, no dismiss state, no score, no ranking and no generated text.

Signals: `USED_MODEL_CHANGED`, `EXPERIMENT_MAY_BE_STALE`, `WATCHED_DEVELOPMENT_CHANGED`, `CONCEPT_CHANGED`, and the `WORTH_REVISITING` rollup that absorbs concept-linked signals so nothing is shown twice. Every signal carries reason codes and evidence references. The default window is 30 days (maximum 90).

## Personal versus shared usage

Two facts are kept apart and never blurred:

- **`PERSONAL_USAGE`**: a completed run of a Personal Lab experiment owned by the authenticated user. Only this is ever worded as "you used".
- **`OPTED_IN_PROJECT_USAGE`**: a successful ModelCall in a project that already has `ail_evidence_opt_in` and that the user can access. The repository does not record who caused a ModelCall or Task Run (`tasks.created_by` is the template's author; `task_runs`, `agent_runs` and `model_calls` carry no user). Personal ownership therefore cannot be established from project membership and is never inferred. This is shared project evidence and says so: "This model was used in an opted-in project you can access."

Re-use suppression follows the same line. Only the authenticated user's own Personal Lab usage after a catalog change suppresses an `EXPERIMENT_MAY_BE_STALE` signal. Another member's later project call never does.

## Experiment completion

An experiment is fully executed only by the canonical definition: `ExperimentExecutionService._execution_state` (read-only) must report `COMPLETED`, and every slot's Agent Run(s) must be `AgentRunStatus.COMPLETED` with a recorded model snapshot. The engine claims the Agent Run `COMPLETED` before it completes the Task Run. `Experiment.status` is not consulted because it is settled lazily by a service whose read path writes.

## Deferred limitations

- **`triage_decisions.revisit_condition` is not supported.** The repository validates only its `kind` and defines no shape or evaluation semantics for it (no verification level, no attention threshold, no date field), and nothing tests any. AIL.4A evaluates only the deterministic `revisit_at` timestamp. A `revisit_condition` is never evaluated, never inferred and never fires a signal. This is not full `revisit_condition` compatibility; it is deferred until the shape is defined and tested.
- No dismiss or acknowledge state: a signal remains until the model is re-used or the window passes.
- A merged Development is not a trigger: developments store no merge time, so it cannot be windowed.
- If a catalog change after a run is later reverted, the net difference against the run's baseline is empty and no signal fires.
- No API toggles `ail_evidence_opt_in`, so the shared project source is dormant outside seeded data. Personal Lab is the live source.
- No Concept detail page exists; concept cards show the change note inline.
- The block has not been checked visually in a browser.
