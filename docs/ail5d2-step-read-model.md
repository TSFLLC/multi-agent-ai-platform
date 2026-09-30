# AIL.5D.2 — Step-aware read model, `/open` correction, evidence isolation

Builds on AIL.5D.1 (`d8dc801`). No schema change, no migration, no curriculum conversion (Day 1 converts in 5D.3), no Lab
redesign, no `practice` step type, no schema v2, no Personal Lab UI change.

## 1. Read model

`GET /academy/level-1/days/{day}/learning` (and `AcademyStepService.learning_view`). Read-only, learner-scoped, takes no
client state.

Structured Day (`structured: true`): `item_id`, `lineage_id`, `version`, `title`, `day`, `week`, `kind`, ordered `steps`,
`current_step_key`, `next_step_key`, `required_total/completed`, `optional_total/completed`, `learning_complete`,
`demonstrated`, `concept_state`, `note`. Each step: `key`, `position`, `type`, `title`, `required`, `estimated_minutes`,
`content`, `binding?`, and this learner's `status` (`not_started | opened | completed | skipped`), `carried_from_version`,
`open_count`, `completed_at`, `is_current`, `is_next`. **Never `private`, never the fingerprint.**

Legacy Day: `structured: false`, no steps; old endpoints (`GET /days/{day}`, `/open`, knowledge check) are untouched.

* `current_step_key` = first step, in authored order, that is `not_started` or `opened`; `next_step_key` = the one after it.
  Completed and skipped (optional) steps are passed over. An unfinished optional step can still be current after every
  required step is done.
* `learning_complete` = every REQUIRED step completed. Presentation only, never an input to AIL.5C.
* `demonstrated` = the canonical learner state of the Day's Concept is `demonstrated` (evidence ledger).
* Version semantics: the view is of the CURRENT version; a stale item id is a `409 stale_item_version`; unchanged steps carry
  completed/skipped progress forward (fingerprint), opened-only never carries.

`GET /days` gains `structured`, `learning_complete` (null for legacy Days) and `demonstrated`. For a structured Day
`evidence_earned` excludes `lesson_completed`; for a legacy Day it is unchanged.

## 2. `/open`

| | legacy (non-structured) Day | structured Day |
|---|---|---|
| `POST /items/{id}/open` | writes one `lesson_completed` self-evidence row (idempotent). Unchanged. | writes **nothing**. Returns `opened: true`, `evidence_recorded: false`, `evidence_id: null`, the current learner state. |
| `lesson_completed` | written by opening | written **once** (per learner and lineage) when the learner's last REQUIRED step actually completes through `AcademyStepService` |

Opening, viewing and skipping can therefore never make a structured Day learning-complete, exposed, or demonstrated. Interactive
steps cannot complete until 5D.3, so no Day completes this way yet; nothing is manufactured.

## 3. Evidence isolation (practice is never a demonstration)

The smallest additive guard, with no schema change:

* Marker: `LearningEvidence.score_json.practice is true` (append-only, on the row itself).
* `independence_policy.demonstration_floor` refuses a practice row (`"practice"`), so it can satisfy no DEMONSTRATED leg
  however it was written. Practice still counts toward PRACTICED (except H5).
* `LearningEvidenceService.record_evidence` (the single canonical write path) refuses the marker on demonstration-only types
  (`explain_back`, `modification`, `reproduction`, `debugging`, `project_assessment`), so AIL.5C's own writer can never be
  tricked into a practice demonstration.
* The two existing Lab writers (`count_toward_learning`, `qualify_agent_fixture`) carry `practice: true` from the experiment /
  task configuration into the marker. Ordinary labs are byte-for-byte unchanged.

AIL.5C readiness, Grader, mode guard and `experiment_facts` are not touched.
