# AIL.5C — Assessment + Demonstration: completion report

Branch `feature/ail5c-assessment-demonstration`, baseline `9231c8de92cdb1da0356fd8ef5b341d75f055d36`
(AIL.5B final). Local commits only: nothing pushed, no PR, no merge, no deployment, no staging,
production or Railway change. The approved design is preserved at
`docs/ail5c-assessment-demonstration-design-v1.md`.

## What was built

| Area | Where |
|---|---|
| One authoritative assistance / independence policy | `app/services/independence_policy.py` |
| Learner State corrections (platform floor, supersession, v2 constraints, multi-hop CHANGED) | `app/services/learner_state_service.py` |
| Schema: 5 tables + 2 isolated enum migrations | `app/models/assessment.py`, `alembic/versions/ail5c_*` |
| Versioned, immutable, hash-frozen definitions | `app/services/assessment_definition_service.py` |
| Deterministic check catalog (17 checks) | `app/services/assessment_checks.py` |
| Authored, seeded fresh challenges | `app/services/assessment_challenge_service.py` |
| Lifecycle (readiness, start, draft, submit, finalize) | `app/services/assessment_service.py` |
| Outcome aggregation in code | `app/assessment_outcome.py` |
| Academy Grader (blind packet, GRADER role, capped cross-check) | `assessment_grader_service.py`, `assessment_grading_packet.py`, `app/assessment_contract.py` |
| Idempotent evidence writer | `app/services/assessment_evidence_writer.py` |
| Consent-bound human review | `app/services/assessment_review_service.py` |
| Report + Demonstration Record | `app/services/assessment_report_service.py` |
| Assessment Mode lock | `app/services/assessment_mode_guard.py` |
| API | `app/api/routers/assessments.py` |
| Authored AI Foundations content + seed | `app/assessment_curriculum.py` |
| Learner UI | `frontend/assets/js/assessments.js`, `pages/assessments.js` |
| Dry-run report for the Learner State correction | `scripts/ail5c_learner_state_diff.py` |
| Deterministic local UAT | `tests/test_ail5c_uat.py`, `python -m scripts.ail5c_uat` |

## Prerequisite corrections (verified against the repository, not assumed)

| Finding | Verdict |
|---|---|
| 5B `/evidence` trusted client `passed` / `execution_verification` | REQUIRED CORRECTION. Now derived from the cited platform record (evaluation run / experiment / agent run); an uncited claim is `self_reported`, graded `self`, PRACTICED-only. |
| 5B `submit` overwrote the assessment-ready snapshot | REQUIRED CORRECTION. 409 once an assessment finalized it. |
| Learner State ignored assistance, verification, demo data, supersession | REQUIRED CORRECTION (platform floor + supersession). A dry-run diff script is provided; the local dev DB predates the AIL schema, so it reported nothing to diff. |
| CHANGED was single-hop | REQUIRED CORRECTION (multi-hop). One pre-existing 4B test that pinned the documented gap was updated to the corrected behaviour. |
| Shared AIL project exposed one learner's runs/artifacts to every other learner | REQUIRED CORRECTION. Records in a SYSTEM_AIL project are private to their creator (tasks, runs, events, usage, artifacts). |
| Enum changes on `learning_evidence` / `agent_runs` | Isolated, rehearsed migrations (see below). |
| `alembic/env.py` swallowed migration errors | ALREADY CORRECT (`disable_existing_loggers=False`). |

## Known limits (stated, not hidden)

* **External AI use is undetectable and is not detected.** Mitigations are fresh seeded authored
  challenges, record-grounded checks, deterministic legs and the learner's own declaration.
  There is no copy detection, similarity code or proctoring (asserted by AST/source tests on both
  the backend and the frontend).
* **Spec conflict:** `docs/ail5-academy-extension-v1.md` H.5 prescribes token-overlap copy
  detection. The frozen brief forbids it and outranks the doc; it is not implemented.
* **Operational / architectural concepts:** the default requirement sets for those kinds include an
  `observation` leg that nothing in the product writes (design D-7). AIL.5C does not add a
  `record_observation` kind, so such concepts cannot reach DEMONSTRATED until a Concept Version
  declares an explicit requirement set. Publishing new Concept Versions is a content decision and
  was not made here.
* **MA9** is not implemented. Definitions may declare `requires_platform_capability` and are then
  shown as "Available after MA9" and cannot be started.
* Study-Mode variants are free text in 5B (no stable id), so "exclude variants already shown in
  Study Mode" cannot be honoured; Study Mode instead forces a fresh authored challenge.
* The capstone and other AI-judged rows are graded `ai_rubric`; on their own they can never
  establish DEMONSTRATED (a deterministic leg is required, enforced at publish and in Learner State).

## Migration safety

R1 (additive tables), R2 (`learning_evidence` enum CHECKs + idempotency index) and R3
(`agent_runs.role` gains `grader`) are separate revisions. R2/R3 use the established batch rebuild
with FK checks, explicit CHECK dropping, stale-temp recovery, and downgrades that refuse while
assessment data exists. Validated only on disposable temp databases: seeded-row preservation across
each rebuild, database-enforced idempotency, model-vs-migration schema parity, a full
upgrade → three one-step downgrades → re-upgrade round trip (integrity `ok`, zero FK violations).
**Before any deploy:** rehearse on a copy of the staging volume (the AIL.3C incident applies) and
obtain fresh deploy authorization.

## Pre-existing failures (identical on pristine `9231c8d`)

`test_ail3c_migration::test_single_alembic_head`, `test_ail4a_stay_ahead::test_no_schema_change_and_no_later_slice_tables`,
`test_ail4b_migration` (2), `test_ail_wave1_integration` (2), `test_migrations::test_downgrade_removes_all_tables`,
`::test_upgrade_is_reapplicable_after_downgrade`, `test_model_selection::test_model_policy_via_api`.
The head assertions still expect `ail5a_academy_foundation`. They were failing before this work and
were not touched.

## Final validation (code SHA `d887910`)

* Full backend suite: **2199 passed, 10 failed** — the 9 pre-existing failures above plus one
  timing flake in `tests/test_ma7_8b_failed_branch_resume.py`. That file fails intermittently on the
  pristine 5B baseline too (5 alternating runs each: 5C 2 of 5 runs failed, baseline 4 of 5).
  Unrelated to AIL.5C.
* AIL.5C backend tests: 201+ (11 files, incl. 8 UAT scenarios); frontend: 472 pass (36 new).
* Three contracts pinned by older tests were respected rather than loosened: the evidence service's
  two-method write surface (supersession lives in the review service), `create_and_execute`'s
  signature, and — deliberately updated — the Professor intent list (now includes the design-frozen
  `HELP_ME_AFTER_ASSESSMENT`).
* Review triggers: learner dispute, low confidence (learner request on a PROVISIONAL result),
  model disagreement (platform-opened, consent-gated), capstone exception. "Authorized manual
  correction" is a reviewer decision (override) on an open review.

## Correction pass (on top of `38562dd`)

Local only: nothing pushed, no PR, no merge, no deployment, no staging or production change.
The structured-lesson work (spec_json.steps, academy_step_progress, step-scoped Professor, step
UI, curriculum v3) has **not** been started.

| # | Correction | Result |
|---|---|---|
| 1 | Lineage provenance | Revision `ail5d_learning_item_lineage` renamed to **`academy_learning_item_lineage`** (file, revision id, docstring, tests). `down_revision` unchanged (`ail5c_grader_agent_role`); one linear chain, one head. Only safe because the revision was never pushed or deployed: no remote ref contains it, and no local/rehearsal/backup DB carries the old id. Functionality untouched. |
| 2 | ORM / migration drift | The database now enforces `lineage_id NOT NULL` and `version >= 1` on migrated SQLite databases with `BEFORE INSERT/UPDATE` triggers (no rebuild of the referenced `learning_items` table); `create_all` databases keep the model's real constraints; other engines get a real NOT NULL + CHECK. `(lineage_id, version)` uniqueness stays a unique index everywhere. The ORM validates in Python and fails closed. Both schema origins are tested against the same raw-SQL invariants. |
| 3 | Golden-set gate | `app/assessment_golden.py` + `scripts/ail5c_grader_gate.py`. See below. |
| 4 | Curriculum outcomes | `curriculum_outcome()` maps internal outcomes to PASS / NEEDS_REVISION / pending states; result views expose it. "Revise once" is definition policy (`grading_policy.max_revisions`). |
| 5 | Observability | `assessment.deterministic_completed` added; budget-exhaustion pause made explicit and proven. |
| 6 | Migration rollback policy | `docs/deployment/migration-rollback-policy.md` + `tests/test_migration_rollback_policy.py`. |
| 7 | Duplicate Level 1 work in the primary checkout | Reported only; not copied, merged or deleted. |

### Golden-set gate
`run_golden_gate` runs a candidate Grader configuration through the real `GradingPacketBuilder`,
`grading_contract_v1` parsing, and the cross-check, against **2 clear pass / 2 clear fail / 2
borderline** cases per approved definition. Synthetic attempts (deterministic stage assumed met) mean
only the Grader's judgment is measured; nothing writes evidence or derives state. Cases are authored
(`AUTHORED_GOLDEN`) or, absent that, derived deterministically from the definition's authored
reference points (never generated by a model). **Release-ready requires every case to match and
every approved definition to be covered** (default: the 8 Level 1 explain-backs +
`eb-structured-output` + `capstone-foundations`); a narrowed, empty, or partly failing run is *not
ready*. The report records the candidate (Agent Version ids, prompt sha256, model policy, provider
models) and a report hash. The CLI copies the database read-only first and never writes the source.
The gate makes real provider calls (cost!); it has been exercised here only with deterministic
stand-in candidates, never against a live model.

### Outcome mapping and revision policy
`passed` + counting effect → **PASS**; `needs_work` → **NEEDS_REVISION**; `provisional` →
PENDING_CONFIRMATION; `human_review_required` → PENDING_HUMAN_REVIEW; `unable_to_assess` →
UNABLE_TO_ASSESS; `passed` with a formative-only/none effect → NOT_COUNTED. Pending/unable states are
never reported as a final verdict. `max_revisions: 1` is set on the 8 Level 1 explain-backs,
`eb-structured-output` (Day 10) and `capstone-foundations`; the generic engine and every other
definition are unchanged (no limit). A `needs_work` streak counts revisions; provisional / review /
unable results do not consume one; a human-requested new assessment starts a new round and bypasses
the limit; when exhausted, readiness names human review instead of "try again". **The retry cooldown
(12 h) is unchanged and still applies between the attempt and its revision** — decide separately
whether the authored "revise and resubmit" should waive it.

### Observability
The design's "Flight Recorder events per stage" are split across two append-only streams, on
purpose: the attempt lifecycle has no task run to hang a Flight Recorder row on (its rows require
`task_id` and `task_run_id`), so lifecycle events are audit events (`started`, `submitted`,
`deterministic_completed`, `finalized`, `evidence_written`, ids/hashes/outcomes only) and every
Grader run is in the Flight Recorder (`grading_requested`, `grading_completed`/`grading_failed`,
`grader_response_rejected`, and the executor's `budget.reservation_rejected`).
Budget exhaustion was already a retry-safe pause (run refused, nothing written, attempt stays
`AWAITING_GRADING`); it now says why (`pause_reason` → "budget_exhausted", surfaced in the result's
pending message) and is proven end to end, including resumption on the same attempt.

### Rollback policy
Irreversible revisions declare `IRREVERSIBLE = True`, refuse in `downgrade()` before touching
anything, and are listed in `EXPECTED_IRREVERSIBLE`. Rollback is restoring the pre-migration backup.
The four historical full-chain downgrade tests still fail at that revision by design (they were already
failing) and were not edited.

### Duplicate Level 1 work in `diag/ail4c-hosted-startup` (superseded, isolated)
Uncommitted there: `academy_level1_service.py`, `academy.py` router/schema/page, `AssistanceLevel`,
and migration `academy_level1_v2` (`down_revision = ail4c_professor_agent_role`, adding
`learning_evidence.assistance_level`). This branch already contains the Level 1 layer and a chain that
already has that column through the 5B migrations; merging the duplicate would create a second Alembic
head. Left untouched for a separate cleanup.

### Validation
* Alembic: single head `academy_learning_item_lineage`, no branch points, old id gone.
* Focused (`-k "ail5 or academy or rollback_policy or lineage"`): **314 passed**.
* Migration/lineage/rollback: 23 passed. Isolation + blindness + API: 31. Golden gate: 18.
  Outcome/revision + observability: 21. Level 1 executable: 12. Frontend: 473 passed.
* Full backend suite: **2275 passed, 9 failed** (previously 2227 / 11). The 9 are the confirmed
  historical baseline ids: 4 stale pinned-head assertions and `test_model_policy_via_api`
  (unchanged causes), and 4 full-chain downgrade tests that now stop at the deliberately irreversible
  revision (expected irreversible-migration-policy mismatch; they failed before it existed, on an
  older cause). The two load-sensitive failures seen at `38562dd` did not recur.
