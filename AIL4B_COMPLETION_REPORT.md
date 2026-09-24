# AIL.4B status

Review & retention loop: "What have I previously demonstrated that I should review again?"

This extends the existing learning engine; it is not a second one. `LearnerStateService` remains the only authority for the evidence ladder and now also derives the `REVIEW_DUE` and `REVIEW_FAILED` overlays. Learning Evidence stays canonical and append-only.

## Schema

One migration revision, `ail4b_review_attempts`, directly above `ail3c_experiment_conclusion`, creating two additive tables (no existing table or constraint is touched, so there is no batch rebuild). The chain stays a single line: `... -> ail3c_experiment_conclusion -> ail4b_review_attempts`.

- **`review_attempts`**: lifecycle `started` / `passed` / `failed` only (no dismissed or snoozed). The database enforces it: a `passed` row must have evidence and a completion time, `failed` has a completion time and never evidence, `started` has neither. At most one `started` attempt per learner and Concept; one evidence row can result from at most one attempt.
- **`review_prompt_deliveries`**: the persistence behind "at most two review prompts per week" (below).

The downgrade refuses while either table has rows.

## Permission versus prompting

These are two separate questions.

- **Permission** (`ReviewAttemptService`): an eligible learner with a DEMONSTRATED Concept may start a review at any time, due or not. What gates a start is eligibility (core, or an `active_plan`), the demonstrated-history requirement, a valid reviewed check item, the single in-progress attempt, and the 12-hour cooldown after a failed review. `REVIEW_DUE` and the weekly quota never affect it.
- **Prompting** (`ReviewPromptService`): which reviews are proactively surfaced on Today. Only prompts already *delivered* this week appear there.

## Retention rules

- **Eligible**: core Concept OR an active (`planned`) Learning Plan item, AND the ladder is `demonstrated`.
- **Clock**: starts at the latest qualifying evidence (positive, not self-reported, not demo data, not superseded), including review evidence. New qualifying evidence resets it. Demo-only evidence starts no clock.
- **Interval**: definitional 180 days, mechanism 120, operational 90, architectural 120; doubled after each qualifying successful review, capped at 365. Every qualifying successful review of the Concept counts; a failed review never erases or resets that count. `Concept.freshness_days` is not used.
- **REVIEW_DUE**: eligible AND (interval elapsed OR a material published ConceptVersion newer than the version the baseline evidence cites).
- **REVIEW_FAILED**: demonstrated AND the latest completed, counting review failed. Only a later successful review clears it. Nothing is deleted, downgraded or written to a Learning Plan.
- A `passed` attempt only counts when its evidence really exists for that learner and Concept and qualifies. Otherwise it is ignored entirely.

## Weekly prompt quota

- **One prompt delivery** = one `review_prompt_deliveries` row: the persisted, first-time surfacing of a review prompt for ONE Concept to ONE user in ONE UTC calendar week (Monday 00:00 UTC to the next Monday). Its identity is (user, concept, week).
- **Two per week is enforced by the database**: `slot` is 1 or 2 and unique per (user, week), so a third delivery cannot exist even under concurrent allocation; and a Concept is delivered at most once per (user, week), so the same logical prompt is never counted twice.
- **Today GET stays read-only.** Delivery is written only by the explicit, idempotent action `POST /learning-reviews/prompts/allocate` (`ReviewPromptService.allocate`). Today GET only reports which delivered prompts to show and whether an allocation would deliver more (`allocation_pending`). After loading Today, the client calls the allocation action once if the server says it is pending, then re-reads. Refreshing Today, or calling the action again, delivers nothing further.
- **Allocation is deterministic**: the lowest free slots are filled with the highest-ordered undelivered candidates (failed reviews first, then material changes, then elapsed intervals; within a group the longest overdue, then name, then id). Extra due Concepts stay known (`not_prompted`) and are not newly prompted until a later week. The next week delivers again when a Concept is still relevant.
- A delivered prompt keeps its slot for the week even after the learner reviews the Concept; its card disappears once it is no longer due or failed.
- **A candidate** is a demonstrated Concept that is due or failed and whose review can actually be started or continued now, so the quota is not spent on prompts that cannot be acted on.
- No notification is sent and nothing is scheduled.

## Successful-review evidence contract

`status = PASSED` alone never creates evidence and is never trusted. A review completes only when: the attempt's item is `reviewed`, deterministic, a check/scenario item of that Concept with a valid choice spec and answer key; the submission is well formed; and the server grades it from the key. Only a correct answer appends evidence, through `LearningEvidenceService.record_evidence` (knowledge_check, deterministic, reviewed, not demo, recorded against the ConceptVersion frozen when the attempt started), in one transaction with the attempt. A wrong answer marks the attempt failed and appends nothing. An invalid or incomplete submission, or an item that stopped qualifying, is refused and the attempt stays `started`. Completion is idempotent and race-safe (compare-and-set; the loser rolls back its evidence).

## Limitations

- **No review content exists yet.** The repository has no knowledge-check runner and no way to author `learning_items`. AIL.4B adds only deterministic single-item choice grading (`spec = {"kind": "choice", "options": [...], "answer_key": [...], "multiple": bool}`); there is no item authoring, question bank management or randomization. Until reviewed check items exist, no review can be started, so nothing is prompted (a Concept with no usable check is not prompted, to avoid wasting the weekly quota).
- Only one review format is supported (one deterministic choice question). Record-grounded questions and mini-labs (spec Sec 22.2) are not.
- No "Not now" snooze or "Not important to me" dismiss (excluded by the contract).
- Because a review may be started early, repeated early reviews extend the interval faster than one per interval. This follows the rule that every qualifying successful review counts.
- Prompt selection favors the same top-ordered Concepts each week while they stay due, so a lower-ordered due Concept can go unprompted for several weeks. It can still be reviewed at any time through the API, and the Review section reports how many due Concepts are not prompted; the UI has no other entry point for a Concept that was not prompted.
- A completed attempt is final and replays its result; it is never re-graded.
- A Concept can appear both in "Worth revisiting" (a recorded change) and in the Review section (a review is due). They answer different questions and are deliberately not deduplicated; AIL.4A signal semantics are unchanged.
- The Review section has not been checked visually in a browser.
