# AIL.5D.4 — Step-scoped AI Professor (and the knowledge-check completion correction)

Builds on AIL.5D.1-5D.3 (`60a4b0a`). One additive table (`academy_professor_help`). No Lab Kits, no Personal Lab hand-off,
no practice instances, no Workspace UI, no Days 2-30 conversion, no change to AIL.5C readiness, Grader or evidence rules.

## 1. Knowledge check: practice until understood

For a structured Day, the `check` step completes only from a PASSED attempt. An unsuccessful attempt is recorded (evidence
row with `passed=false`, plus the learner's answers as a step response) and returns learning feedback; it does not complete
the step, create demonstrated state or satisfy the Day. The learner retries until the existing checker passes every item; the
step then completes (`completion_basis.kind = knowledge_check_passed`). Retries are unlimited: this is practice, not AIL.5C.

Feedback after a failure (`feedback` in the knowledge-check response, structured Days only): how many items are incorrect,
which ones (by id), the attempt number, `retry_allowed`, and a short coaching message. It withholds the answers and the
authored rationale; the rationale is released once every item is right, so retrying stays meaningful. Legacy Days and the
legacy response shape are unchanged (`feedback: null`). An optional check step is still governed by its authored
`required: false`: skipping is a separate action from completing.

## 2. Context the Professor receives (`target.type = academy_step`, intent `EXPLAIN_THIS`)

Assembled on the server from the CURRENT item version by `AcademyStepService.professor_view`, the single place authored
private material is read for the Professor:

* always: program, Day/week, item version, the current step (key, type, title, required, position, status, public content),
  the public Day outline with per-step status, a progress summary (completed/skipped keys, current step), the Concept and its
  current version, and public vocabulary where the Day has it;
* this learner's own response to THIS step, when one exists (and, for a reflection, the one earlier step it compares to);
* a knowledge check: the public questions (never answers), attempts, pass state and per-item pass/fail flags;
* the think reveal, only after THIS learner committed an answer AND was served the reveal;
* the check rationale, only after THIS learner passed the check.

Never: `private`, answer keys, the AIL.5C reference points or any Grader output, another learner's data, responses from
unrelated steps, or anything from a stale item version (a stale id is a 409 naming the current one).

The legacy Concept context also stops handing the Professor a legacy Academy Day body: those bodies were authored with the
answer reveals and the Grader's reference points inline. A structured Day's body is rebuilt from public content only and stays.

## 3. Modes and the help ladder (`app/academy_professor.py`)

| mode | steps | behavior |
|---|---|---|
| teaching | teach, example | explains normally |
| guided | reflect, think, check, explain_back, reflection (lab later) | coaches; never completes the exercise or writes the learner's response |
| independent | a future `practice` step (declares `mode: independent`) | smallest help that unblocks; same ladder |
| assessment | not a Professor mode | the existing AIL.5C lock pauses the Professor (409) |

The client only chooses `ask` or `hint`. The server chooses the mode and the level: `ask` -> clarify (explain, in teaching);
each `hint` on the same step climbs hint -> stronger hint -> explain. Explain is only reachable when it cannot solve the
exercise for the learner (think after the commit, check after a pass; reading and reflection steps always). Nothing in the
request can name a level or a mode.

## 4. Assistance tracking (`academy_professor_help`)

One row per DELIVERED help (a completed, validated interaction; failures record nothing): user, exact item version and step,
mode, request, level, the H0-H5 `assistance_level` (H1 clarify, H2 hint, H3 stronger hint, H4 explain, H5 when authored
solution material was in the context, none for teaching), `revealed_solution`, the interaction id and a SHA-256 of the exact
context. It reuses the existing independence scale, so a later practice instance simply reads the highest level on its step.
Using the Professor never creates evidence, never changes progress, and never changes what a learner has demonstrated.

## 5. API

`GET /academy/level-1/items/{id}/steps/{key}/professor` (mode, next hint level, what it can see, paused?),
`POST` the same path `{question?, help: ask|hint}`; the generic `POST /professor/interactions` and `GET
/professor/context-preview` accept the same `academy_step` target (`step_key`, `help`). Step help cannot be continued from a
stored context (409 `step_help_not_continuable`): it is re-assembled from the current step each time.
