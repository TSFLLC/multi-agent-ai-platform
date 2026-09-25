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
