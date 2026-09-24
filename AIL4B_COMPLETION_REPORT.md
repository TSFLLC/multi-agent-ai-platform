# AIL.4B status

Review & retention loop: "What have I previously demonstrated that I should review again?"

This extends the existing learning engine; it is not a second one. `LearnerStateService` remains the only authority for the evidence ladder and now also derives the `REVIEW_DUE` and `REVIEW_FAILED` overlays. Learning Evidence stays canonical and append-only.

## Schema

One additive table, `review_attempts` (revision `ail4b_review_attempts`, directly above `ail3c_experiment_conclusion`). No existing table or constraint is touched, so there is no batch rebuild. Lifecycle is `started` / `passed` / `failed` only; there is no dismissed or snoozed state. The database enforces it: a `passed` row must have evidence and a completion time, `failed` has a completion time and never evidence, `started` has neither. At most one `started` attempt per learner and Concept; one evidence row can result from at most one attempt. The downgrade refuses while any attempt exists.

## Rules

- **Eligible**: core Concept OR an active (`planned`) Learning Plan item, AND the ladder is `demonstrated`.
- **Clock**: starts at the latest qualifying evidence (positive, not self-reported, not demo data, not superseded), including review evidence. New qualifying evidence resets it. Demo-only evidence starts no clock.
- **Interval**: definitional 180 days, mechanism 120, operational 90, architectural 120; doubled per successful review in the current streak, capped at 365. The streak is the run of successful reviews since the latest failed one. `Concept.freshness_days` is not used.
- **REVIEW_DUE**: eligible AND (interval elapsed OR a material published ConceptVersion newer than the version the baseline evidence cites). This walks every version, so a minor latest version does not hide an earlier material one (the older single-hop `CHANGED` overlay is unchanged).
- **REVIEW_FAILED**: demonstrated AND the latest completed, counting review failed. Only a later successful review clears it. Nothing is deleted, downgraded or written to a Learning Plan.
- A `passed` attempt only counts when its evidence really exists for that learner and Concept and qualifies. Otherwise it is ignored entirely.

## Successful-review evidence contract

`status = PASSED` alone never creates evidence and is never trusted. A review completes only when: the attempt's item is `reviewed`, deterministic, a check/scenario item of that Concept with a valid choice spec and answer key; the submission is well formed; and the server grades it from the key. Only a correct answer appends evidence, through `LearningEvidenceService.record_evidence` (knowledge_check, deterministic, reviewed, not demo, recorded against the ConceptVersion frozen when the attempt started), in one transaction with the attempt. A wrong answer marks the attempt failed and appends nothing. An invalid or incomplete submission, or an item that stopped qualifying, is refused and the attempt stays `started`. Completion is idempotent and race-safe (compare-and-set; the loser rolls back its evidence).

## Deviations and limitations

- **No review content exists yet.** The repository has no knowledge-check runner and no way to author `learning_items`. AIL.4B adds only deterministic single-item choice grading (`spec = {"kind": "choice", "options": [...], "answer_key": [...], "multiple": bool}`); there is no item authoring, question bank management or randomization. Until reviewed check items exist, a due card honestly shows "No reviewed check is available for this Concept yet."
- Only one review format is supported (one deterministic choice question). Record-grounded questions and mini-labs (spec Sec 22.2) are not.
- The spec's "at most two review prompts per week" cannot be tracked without persisting prompt shows, which the contract forbids. It is a presentation cap instead: at most two review cards are shown and the total is always reported.
- No "Not now" snooze or "Not important to me" dismiss (excluded by the contract).
- A review can only be started while the Concept is due or its latest review failed (so early reviews cannot inflate the interval), and a retry after a failed review waits 12 hours (spec Sec 19.2). Item choice is deterministic: least recently used first, and not the item just failed when another exists.
- A completed attempt is final and replays its result; it is never re-graded.
- A Concept can appear both in "Worth revisiting" (a recorded change) and in the Review section (a review is due). They answer different questions and are deliberately not deduplicated; AIL.4A signal semantics are unchanged.
- The Review section has not been checked visually in a browser.
