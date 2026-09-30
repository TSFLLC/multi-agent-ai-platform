# AIL.5D.1 — Structured Learning Foundation

Status: implemented on `feature/ail5d1-structured-learning-foundation`. Foundation only. No curriculum has been converted,
no Workspace UI exists, no Capstone change. The approved design reference is the AIL5D prototype
(`frontend/prototypes/ail5d`, branch `design/ail5d-prototype`).

## Naming

The historical migration `ail5d_learning_item_lineage` is **Learning Item revision infrastructure** (deployed history, never
renamed or edited). It is *not* this work. This slice ships as `academy_step_progress`, on top of `academy_lineage_enforcement`.

## Architecture

```
Program        [AcademyProgram / AcademyProgramVersion, unchanged]
 -> Module     [program_items.module_key / week, unchanged]
  -> Day       [LearningItem, versioned by (lineage_id, version)]
   -> Steps    [LearningItem.spec_json.steps, ordered, immutable with the item version]
Learner state:  academy_step_progress  (mutable, user-scoped, separate from authored content)
```

There is no second content store and no second versioning system. A revised Day is a new `LearningItem` version.

## The step contract (`app/academy_steps.py`, `step_schema_version = 1`)

A Day opts in with `spec["step_schema_version"] = 1` and `spec["steps"] = [...]`. Items without `steps` are unchanged.

Every step: `key` (stable lowercase slug, unique within the Day), `type`, `title`, `estimated_minutes` (1-180),
`required` (explicit boolean), `content`, and optionally `private`, `binding`, `professor_hint`. Unknown fields are rejected.
Order is the list order; `position` is derived.

| type | content | private (server only) | binding (reuses an existing mechanism) |
|---|---|---|---|
| `teach` | `blocks` (heading, md, callout, pair, compare, tabs, accordion, cards) | not allowed | none |
| `example` | `cases`, `lead_md?`, `takeaway_md?` | not allowed | none |
| `reflect` | `prompt_md`, `min_chars?` | not allowed | none |
| `think` | `scenario_md` + exactly one of `question_md` / `statements` | **required**: `reveal_md` or per-statement `claims` | none |
| `check` | `intro_md` | not allowed | **required** `item_knowledge_check` (the Day's existing `knowledge_check`) |
| `explain_back` | `prompt_md`, `points` | not allowed | optional `assessment` -> AIL.5C definition key (must equal the Day's) |
| `lab` | `problem_md`, `predictions`, `prompt_text?`, `record?` | optional `authored_check_md` | **required** `personal_lab` engine (must equal the Day's `engine_binding`) |
| `reflection` | `prompt_md`, `compare_to?` (an EARLIER `reflect` key) | not allowed | none |

Day-level rules: at least one required step; at most one `check` and one `explain_back`; a `check` needs the Day's
`knowledge_check`; a `lab` step only on a lab Day; total size bounded.

**Privacy.** `public_steps(spec)` / `strip_private(spec)` remove `private`; the Level 1 Day endpoint and the new steps endpoint
use them. Answers, reveals and rubric material never reach a client or the Professor: the Professor context is built from
explicit Learning Item fields and never includes `spec`. AIL.5C rubrics and answer keys stay in AIL.5C.

**Fingerprint.** `fingerprint(step)` digests the learner-visible definition (`key,type,title,required,content,binding`); it
ignores `private`, `estimated_minutes` and `professor_hint`, so correcting a hidden answer does not invalidate progress.

## Progress (`academy_step_progress`)

One row per (user, exact item version, step key). `status` is `opened | completed | skipped`. `opened` is a view and never a
completion. Columns: `user_id`, `learning_item_id`, `lineage_id` (server-set), `step_key`, `step_fingerprint`, `status`,
`open_count`, `first_opened_at`, `last_opened_at`, `completed_at`, `skipped_at`, `completion_basis_json`.
DB CHECKs keep `completed_at` / `skipped_at` consistent with `status` and `open_count >= 1`.

`AcademyStepService`:

* only the CURRENT item version can be acted on; a stale id gets `409 stale_item_version` naming the current one;
* `complete` works for `teach` / `example` on the learner's Continue; every other type needs a `VerifiedCompletion` that only
  server code constructs (no API path builds one in 5D.1), so an interactive step cannot be completed by asking;
* required steps cannot be skipped; optional steps can;
* progress carries to a newer item version only for a step whose fingerprint is unchanged and that was completed/skipped;
* nothing is written to AIL.5C, Personal Lab or the Professor. (5D.2: a single `lesson_completed` row is earned when the last
  required step completes; see `docs/ail5d2-step-read-model.md`. Opening, viewing and skipping never write evidence.) "Learning complete" (all required steps
  completed) is derived and labelled as not demonstrated knowledge.

## API (additive, learner-scoped, under `/academy/level-1`)

`GET /items/{item_id}/steps`, `POST /items/{item_id}/steps/{step_key}/open | complete | skip`.

## Deferred

5D.2 convert Day 1 and add the step-aware read model; 5D.3 interactive step submission and server-verified completion;
5D.4 step-scoped Professor; 5D.5 Personal Lab step flow; 5D.6 Workspace UI, remaining Days, Capstone.
