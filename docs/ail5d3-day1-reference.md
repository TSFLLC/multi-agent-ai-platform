# AIL.5D.3 — Learner responses, server-verified completion, and Day 1 as the reference structured Day

Builds on AIL.5D.1 (`d8dc801`) and AIL.5D.2 (`e84b66f`). One additive table (`academy_step_responses`), Day 1 only, no
Workspace UI, no Lab/Practice work, no Professor change, no AIL.5C change.

## 1. Learner responses (`academy_step_responses`, migration `academy_step_response`)

Append-only, user-scoped, bound to the exact Learning Item version and step. Columns: `user_id` (server-set),
`learning_item_id`, `lineage_id` (server-set), `step_key`, `step_fingerprint`, `kind` (`answer | prediction | observation |
reflection | outline | note`), `response_key` (part of a response: a statement id, an outline point; `""` for a single
value), `revision` (>= 1), `content_json`, optional `ref_type`/`ref_id` (the canonical record it belongs to; the
knowledge-check evidence row now, an experiment or run for Lab/Practice later), `assistance_json` (reserved for help
metadata). Unique on (user, item, step, kind, response_key, revision). Refining appends a revision; the latest wins; nothing
is updated or deleted, so ordering is provable (an answer is committed before its reveal is served).

A response is data: it is never a grade, writes no evidence, and is never given to the Professor or the Grader.
`prediction` and `observation` are in the vocabulary now so Lab/Practice (5D.5) needs no replacement.

API (all learner-scoped, bodies carry no learner/version/step/status):
`PUT /items/{id}/steps/{key}/response`, `GET /items/{id}/steps/{key}/response`, `POST /items/{id}/steps/{key}/reveal`.

## 2. Completion contracts (server-authoritative)

| step type | how the learner acts | what the SERVER verifies before `complete` succeeds |
|---|---|---|
| `teach`, `example` | Continue | nothing (explicit Continue) |
| `reflect`, `reflection` | `PUT response {text}` (8+ chars, <= 8000) | a valid saved response |
| `think` | `PUT response` once (text, or per-statement choice + optional reasoning) | a committed answer; the commit is final |
| `check` | the EXISTING `POST /items/{id}/knowledge-check` | canonical knowledge-check evidence for this learner and item |
| `explain_back` | `PUT response {points}` (every point, 5+ chars): a private outline | a saved outline. The graded work stays in AIL.5C |
| `lab` | not yet | cannot complete until the Lab slice |

`complete` never reads a client claim: the request body is ignored, and unknown fields on `response` are rejected (422).
Opening or viewing never completes anything. `lesson_completed` is written once, when the last REQUIRED step completes.

## 3. Think About It: commit, then reveal

`private` is never in the learning read model. `POST .../reveal` is the only path that returns it, explicitly scoped to the
committed learner and the step: 409 `commit_required` before a commit, and a `reveal-viewed` note is recorded once, after the
commit. Re-committing is refused (409 `already_committed`). Statement steps report which of the learner's choices matched.

## 4. Knowledge check

The structured `check` step binds to the Day's existing `spec.knowledge_check`; there is no second checker. A submission
writes the canonical `knowledge_check` evidence as before; for a structured Day it also keeps the learner's own answers
(`answer` response, `ref` -> the evidence row). The step completes from that evidence. A failed submission is recorded and
completes the step; it never makes the Day demonstrated. The authored rationale (`explanation`) is returned only once every
item is right, so it cannot be used to copy answers into a passing evidence row.

## 5. Explain-back and AIL.5C

The `explain_back` step carries the authored learner prompt and its four points, and references the existing
`assessment_definition_key` (`level1-day-01-explain-ai`). AIL.5C keeps readiness, attempts, deterministic checks, the Grader,
retry/cooldown and demonstrated evidence. The Grader's reference points, the PASS/NEEDS_REVISION process and the "Evidence
generated" list are not learner content and are not in any step.

## 6. Day 1 mapping (explicit, versioned authoring)

`POST /academy/level-1/days/1/author-structure` (service `author_day_structure`) publishes Day 1 as the next version of its
Learning Item (legacy version preserved, program item repointed, idempotent). Routine provisioning never touches a structured
Day. Only Day 1 has a mapping; every other Day reports `not_authored`.

| # | key | type | req | authored source | notes |
|---|---|---|---|---|---|
| 1 | `day-overview` | teach | yes | Duration, Type, Learning objectives, Prerequisites | verbatim |
| 2 | `baseline` | reflect | no | 0-5 min Opening | the quoted prompt + closing line |
| 3 | `what-is-ai` | teach | yes | 5-12 min | verbatim |
| 4 | `not-one-technology` | teach | yes | 12-20 min | verbatim |
| 5 | `traditional-vs-ai` | teach | yes | 20-28 min, up to Think #1 | verbatim |
| 6 | `think-traditional-or-ai` | think | yes | Think #1 | scenario + question public; reveal private |
| 7 | `what-ai-can-do` | teach | yes | 28-36 min | verbatim |
| 8 | `what-ai-isnt` | teach | yes | 36-44 min, up to Think #2 | verbatim |
| 9 | `think-three-claims` | think | yes | Think #2 | three statements public; classifications private |
| 10 | `real-world-cases` | example | yes | 44-50 min | two authored case studies |
| 11 | `ai-or-not-ai` | check | yes | 50-55 min | instructions public; scenarios and rationales in `knowledge_check` |
| 12 | `explain-ai` | explain_back | no | 55-60 min learner prompt | four authored points; binds to the AIL.5C definition |
| 13 | `takeaways` | teach | yes | Summary, Vocabulary (as a table block), Connection to Day 2 | verbatim |
| 14 | `compare-answers` | reflection | no | the opening's "come back to what you wrote" line | compares to `baseline` |

Excluded from learner content, and pinned by tests: the stage directions addressed to the Professor/system, and the AIL.5C
Grader material (reference points, PASS/NEEDS_REVISION process, "Evidence generated"). Derived UI text is limited to two
instruction sentences (`DERIVED_TEXT`). The pre-Workspace `body_md` of the converted Day is rebuilt from public content only,
which also removes the answer and rubric leak the old body had.
