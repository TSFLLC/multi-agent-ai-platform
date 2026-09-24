# AIL.3C status (backend and API layer only)

The frontend is **not** implemented. AIL.3C is not complete.

## Implemented and tested

Every item below has an executable test in `tests/test_ail3c_learning_api.py` (37 tests) or `tests/test_ail3c_migration.py` (3 tests).

- **Concept binding.** `LabService.create_experiment` and `PUT /lab/experiments/{id}/concept` resolve the current ConceptVersion via `ConceptGraphService.get_current_version` and persist `concept_id` and `concept_version_id`. A Concept is never created, and reassignment is refused with 409 once experiment-backed evidence exists.
- **Legacy experiments.** A pre-AIL.3C experiment with a `concept_id` but NULL `concept_version_id` reports `CONCEPT_REBIND_REQUIRED`. No version is invented; the owner re-binds explicitly.
- **Qualification** (`GET .../learning-qualification`), reusing AIL.3B's execution and evaluation state rather than a second model:
  - the required executions are the `ExperimentTaskRun` slots whose Task Run COMPLETED;
  - evaluation must be configured (`evaluation_definition_version_id`);
  - the latest `EvaluationRun` on that definition version per slot Agent Run is authoritative, and its criterion results must cover every criterion of the definition version;
  - status codes: `READY`, `ALREADY_COUNTED`, `MISSING_CONCEPT`, `CONCEPT_REBIND_REQUIRED`, `EXPERIMENT_INCOMPLETE`, `EVALUATION_NOT_CONFIGURED`, `EVALUATION_PENDING`, `EVALUATION_FAILED`, `EVALUATION_INCOMPLETE`, `EVIDENCE_DID_NOT_PASS`.
- **Pass derivation** comes only from MA6 findings: no NOT_MET or PARTIAL anywhere, and at least one MET. `Experiment.status == COMPLETED` alone is never a pass, and no score is invented.
- **Count Toward Learning** (`POST .../count-toward-learning`) is an explicit, authenticated action that appends one LAB / DETERMINISTIC / `passed=True` / `ref_type=EXPERIMENT` evidence row against the frozen ConceptVersion. The response includes the learner state derived by `LearnerStateService`. It never sets PRACTICED or DEMONSTRATED directly.
- **Exactly-once.** A partial UNIQUE index `uq_learning_evidence_experiment_ref` on `(user_id, ref_id) WHERE ref_type='experiment'` is declared on the model and created by the migration. On `IntegrityError` the service rolls back and re-reads the canonical row. A threaded race, a forced lost-race path, and a raw duplicate insert are all tested.
- **Human conclusion** (`PUT .../conclusion`). The types are `no_meaningful_difference`, `tradeoff`, `inconclusive`, `more_testing_needed` and `custom`; there is no winner value. It is allowed only once results exist (AIL.3B overall status COMPLETED, PARTIAL, FAILED or EVALUATION_FAILED). It touches only the three conclusion columns and is exposed on the experiment read contract. Conclusion validation is enforced in the schema and service; there is no DB CHECK on `conclusion_type`.
- **Authorization.** The authenticated user is authoritative. Request models forbid extra fields, so a client `user_id` is a 422. Any other user gets 404 on every route.
- **Radar and Learning Plan** rows are unchanged by conclusion and count actions (asserted by table snapshots).
- **Migration** `ail3c_experiment_conclusion`:
  - it adds `experiments.concept_version_id` (real FK to `concept_versions.id`) plus the three conclusion columns;
  - it widens `learning_evidence`'s `ref_type` CHECK to allow `'experiment'`. This was missing before and would have rejected every experiment-backed evidence row on a migrated database;
  - it creates the partial unique index;
  - the downgrade refuses while experiment-backed evidence exists.
  Tested on disposable databases: populated upgrade, downgrade, re-upgrade, `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, and a single head.

## Known product limitation

The pass rule requires no NOT_MET or PARTIAL finding across all evaluated candidates. A model face-off where one model fails a criterion therefore does not qualify as passing LAB evidence. This is kept as specified and needs a product decision before UAT.

## Deferred

- Personal Lab frontend (conclusion editor, Count Toward Learning UI, qualification explanations, Concept picker, Radar-origin readback).
- Professor: no canonical implementation exists.
- Learning Plan proposal from an experiment.
- Frontend tests.
