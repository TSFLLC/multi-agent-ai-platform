# AIL.5C — ASSESSMENT + DEMONSTRATION: EXTENSIBLE SYSTEM DESIGN

Design only. No files were modified, and nothing was branched, committed, pushed or deployed. Repository facts come from the `ail5b-build-with-me` worktree at `9231c8d`. Where the repository and the specs disagree, the item is labeled **SPEC / REPOSITORY CONFLICT**.

---

## A. Executive Summary

AIL.5C adds one layer: **verify that a learner can independently do or explain what 5A and 5B taught, and record that as append-only Learning Evidence.** It adds five tables and two enum extensions. It adds no mastery table, no score, no second experiment engine and no second approval engine.

**Design in one paragraph.** A learner starts an **Assessment Attempt** against an immutable, versioned **Assessment Definition**. The attempt enters **Assessment Mode**, where Mentor hints are locked for the assessed concept. Fresh challenges are drawn deterministically from authored variant pools. On submit, the platform freezes a hashed manifest. A deterministic pipeline then re-derives every fact from platform records. A separate **Academy Grader** agent judges only the rubric items that need judgment. It sees a blind, allowlisted packet: no assistance level, no Mentor context, no history. Code, not the model, assembles the outcome. A successful outcome appends `LearningEvidence` idempotently, and the existing `LearnerStateService` recomputes state. A low-confidence or disputed outcome goes to a consent-bound human review.

### Repository ground truth

The design depends on these findings. Several are defects in what 5B shipped, so they are P0 prerequisites (§AJ).

| ID | Finding (evidence) | Consequence |
|---|---|---|
| **SRC-1** | **SPEC / REPOSITORY CONFLICT.** 5B `POST …/milestones/{id}/evidence` (`build_with_me.py:269`) accepts client-supplied `passed`, `execution_verification`, `assistance_level` and `source_id`, and `qualify_candidate` writes `LAB` evidence with `passed=True` and grader=`deterministic`. `/test` (`:184`) is a stub returning `execution_available: False`. `/complete` (`:247`) sets `passed` unconditionally. Spec §I/§S-4 says completion is impossible without a real check. | "platform_verified" in 5B is an assertion, not a fact. 5C treats every 5B candidate field as a claim and re-derives facts from platform records. |
| **SRC-2** | **SPEC / REPOSITORY CONFLICT.** `LearnerStateService._requirement_evidence` (`learner_state_service.py:187`) matches on `evidence_type` and `passed` only. It ignores `assistance_level`, `execution_verification`, `on_demo_data`, and `superseded_by_id`. 5A §I.4/§18.4 says DEMONSTRATED needs verified work at ≤H2. | An H3/H4 `LAB` row written by 5B can satisfy a DEMONSTRATED lab leg today. Retention (`evidence_qualifies`) honors demo and superseded rows. Learner State does not. |
| **SRC-3** | `learning_evidence.superseded_by_id` exists (`models/learner.py:195`) but the ladder computation never reads it. | Dispute and override cannot work until Learner State honors supersession. |
| **SRC-4** | `record_evidence` has three callers: 5B (`LAB`), AIL.3C (`LAB`), AIL.4B reviews (`KNOWLEDGE_CHECK`). Nothing writes a first-attempt knowledge check, `INTERPRETATION`, `OBSERVATION`, or `LESSON_COMPLETED`. | With default per-kind requirements, DEMONSTRATED is effectively unreachable in the shipped product. 5C is the first real writer for knowledge-check and interpretation legs. |
| **SRC-5** | `AssessmentReadySubmission` (`models/academy.py:260`) holds IDs only. It is overwritten on resubmit (`row.snapshot = snapshot`). It is unique per project attempt, and `finalized_at` is never set. No learner artifact content exists anywhere; the only learner text is `explain_back_responses`, whose `question` is client-supplied. | The spec's "learner artifacts" handoff does not exist. 5C must freeze content and a hash itself. |
| **SRC-6** | `EvidenceType` has six values. `AgentRunRole` has `evaluator` and `professor` but no `grader`. Both columns are `VARCHAR` plus `CHECK` (`sa_enum`), so adding a value needs a SQLite batch rebuild (see `ail4c_professor_agent_role.py`). The AIL.3C staging crash-loop happened on a migration. | Enum changes are isolated in separate, rehearsed migrations (§AC). |
| **SRC-7** | `ensure_ail_system_project` grants every learner `ProjectRole.OWNER` on the shared per-org AIL project. `ApprovalService.resolve` authorizes by project `MODIFY`. Professor stores learner text in `Task.requirements`. | Project RBAC does not isolate learners. Approvals cannot carry learner disputes, and grader inputs must not be written into `tasks` rows. |
| **SRC-8** | `ConceptKind` has four values. 5A CR-1 `skill` was never applied. `ProjectMentorService` uses an inline `ASK_PROFESSOR` prompt, not the separate prompt version 5A §G.1 prescribes. | Skill-like requirement sets are expressed through `evidence_requirements` JSON. No new kind is needed. |
| **SRC-9** | **SPEC / FROZEN-PROMPT CONFLICT.** 5A §H.5 prescribes token-overlap copy detection. This prompt (§34) forbids it. Authority order puts the prompt above the 5A doc. | No similarity detection anywhere in 5C. |

Already correct at `9231c8d` and to be kept: `alembic/env.py` uses `fileConfig(..., disable_existing_loggers=False)`. `artifacts.agent_run_id` is nullable and `artifacts` already has `milestone_attempt_id` and `enrollment_id`.

---

## B. Product Goal

Answer one question: **"Can I independently show that I understand this and can apply it?"** The learner gets three things:

1. A clear, humane path from "I finished the project" to "I demonstrated the concept."
2. An honest report that separates platform facts, AI judgment, their own reflection, and coaching.
3. A **Demonstration Record**. It is evidence-linked, not a certificate.

Failure produces a specific gap and a targeted next step, never a dead end.

---

## C. Assessment Philosophy

1. **Evidence, not scores.** DEMONSTRATED is derived from a requirement set. No number stands for "how much they know."
2. **The helper never grades the same work.** The Grader is a separate registered agent, and the Professor/Mentor is barred from that code path.
3. **Deterministic first.** The model judges only what the platform cannot compute.
4. **Independence is shown, not hidden.** H-levels stay visible. Policy classifies each evidence row (full, partial, formative) with a reason code, never a hidden independence score.
5. **Fresh beats re-graded.** After assisted work, the answer is a fresh challenge, not a more suspicious grader.
6. **Criteria, not people.** No cross-learner comparison, ranking, percentile, or leaderboard, even in aggregate.
7. **No surveillance.** Integrity comes from evidence design. No webcam, screen, keystroke, focus, or clipboard capture.
8. **Truthful language.** No "certified", "mastered", "expert", or "accredited."

---

## D. Boundary with AIL.5B

| | **AIL.5B: teach, guide, practice, prepare** | **AIL.5C: assess, verify, demonstrate, report** |
|---|---|---|
| Owns | Templates, milestones, hints (H0–H5), Mentor, Study Mode, candidate evidence, PRACTICED-level qualification, explain-back capture, `AssessmentReadySubmission` | Definitions, attempts, challenges, deterministic checks, Grader, results, reviews, DEMONSTRATED-level evidence, reports, records |
| Never does | Grade formally; write DEMONSTRATED-level evidence | Teach, hint, or coach inside an assessment |

**Handoff contract.** 5C consumes an `assessment_ready_submissions` row as a pointer, then re-verifies and freezes it:

- At attempt start, 5C copies the referenced set into `input_manifest_json` and stores `input_manifest_hash`. The set is milestone attempts, candidate and learning evidence IDs, experiment links, and the learner agent version. It also records immutable anchors: `agent_versions.id`, `evaluation_runs.subject_artifact_content_hash`, and `project_templates` version rows.
- Every referenced record is checked for learner ownership by walking the chain from the learner's own `project_attempt` and `experiment`. Project membership is never used as proof (SRC-7).
- 5B's later overwrite of the snapshot cannot alter a 5C attempt, because 5C holds its own frozen copy.

**Only 5C writes into 5B tables**, through services:

1. `assessment_ready_submissions.finalized_at` (an existing, unused column).
2. `project_attempts.status` → `PASSED` or `NEEDS_WORK` (enum values that already exist and are set nowhere).

**Two small 5B hardening asks** (P0-2 in §AJ): `submit` refuses to overwrite once `finalized_at` is set, and the `evidence` route derives `passed` and `execution_verification` server-side from the cited record.

**5B explain-back responses are never graded.** They carry a client-authored question and a Mentor-assisted context. They appear in reports as "your earlier explanation (formative)." 5C explain-back is always a fresh, authored-prompt attempt.

---

## E. Boundary with MA6 (mandatory)

| | **MA6 Evaluation Engine** | **AIL.5C Learner Assessment** |
|---|---|---|
| Subject | An **Agent Run's output artifact**. `evaluation_runs.subject_agent_run_id` and `subject_artifact_id` are NOT NULL, with a content hash frozen at bind. | A **learner's demonstrated understanding**: human-authored responses, behaviors, and platform records |
| Rubric ownership | `EvaluationDefinition` is project-scoped and operator-owned | Academy-owned, learner-facing, tied to Concept Versions |
| Output | `MET / PARTIAL / NOT_MET / NOT_APPLICABLE` per criterion. Never a score, rank, or winner. | Same finding vocabulary, plus an outcome and Learning Evidence for a concept |
| Consumers | MA8.2 routing evidence joins `evaluation_runs.subject_agent_run_id` | Learner State, retention, reports |

**Reuse:**

1. **MA6 `EvaluationRun` results for the learner's app** (App Test Set runs) are consumed read-only as deterministic platform facts. Their identity is cited (run id plus subject artifact hash), and they are never re-judged.
2. **The `EvaluationFinding` vocabulary** is reused as-is.
3. **The evaluator dispatch pattern** is reused. `build_agent_evaluator_run` creates a dedicated bookkeeping Task, TaskRun and AgentRun, with `input_context_json` holding "durable pointers only." `execution_service._build_extra_context` rebuilds the prompt from DB state at run time.
4. **The fail-safe parsing philosophy** of `evaluation_contract.parse_evaluation_response` is reused: exactly the expected criterion keys, and nothing inferred.

**Not reused, deliberately:**

1. `EvaluationDefinition` tables. Learner rubrics do not belong in operator-owned, project-scoped rows.
2. `EvaluationRun` as the vehicle for grading learner text. That needs a fake subject agent run and artifact, which would pollute MA8.2 routing evidence and MA6 semantics.

The Grader consumes MA6 evidence without replacing MA6. MA6 answers "did this app's outputs meet the test-set criteria?" and 5C answers "can this learner explain, modify and defend it?"

---

## F. Assessment Domain Model

```
Concept ─ConceptVersion (evidence_requirements v2 = DEMONSTRATED requirement set)
   ▲ pinned
AssessmentDefinition (row-per-version, immutable when published)
   │ 1..n AssessmentDefinitionConcept (pinned concept_version)
   ▼ instantiated by
AssessmentAttempt ── manifest (frozen, hashed) ── refs → 5B submission, MA6 runs, experiments
   │ 1..n append-only
   ▼
AssessmentResult (deterministic | grader | final | human)
   │ final + passed ──► LearningEvidence (idempotent) ──► LearnerStateService (recompute)
   ▼ optional
AssessmentReview (consent-bound human review)
```

**Reused as-is:** `Concept`, `ConceptVersion`, `LearningItem` (knowledge-check question source), `LearningEvidence`, `LearnerStateService`, `ReviewAssessor`, `AssessmentReadySubmission`, `ProjectAttempt`, `MilestoneAttempt`, `Experiment`, `EvalSet`, `EvaluationRun`, `Agent`/`AgentVersion`/`PromptVersion`, `AgentRun`/`ModelCall`, Flight Recorder, `IdempotencyService`, `AuditService`, `Budget`.

**New:** `assessment_definitions`, `assessment_definition_concepts`, `assessment_attempts`, `assessment_results`, `assessment_reviews` (§AC).

---

## G. Assessment Lifecycle

```
DRAFT ──submit──► SUBMITTED ──► CHECKING ──► GRADING ─┐   (GRADING skipped if no grader criteria,
  │                                  │                 │    or a required deterministic criterion failed)
  │ expire/abandon                   └─────────────────┴──► FINALIZED (outcome on the final result)
  ▼
ABANDONED
```

Attempt statuses are `DRAFT | SUBMITTED | CHECKING | AWAITING_GRADING | GRADING | FINALIZED | ABANDONED`.

1. **Start.** Authorization, then a deterministic readiness check. A failed check returns 409 with reasons, not a result. Then the challenge is issued, the mode lock is set, and `expires_at` is set (default 24h, extendable).
2. **Draft.** Responses are saved to `draft_json`. This is the only mutable state.
3. **Submit.** A compare-and-swap moves `DRAFT→SUBMITTED`. The response set and manifest are frozen and hashed, and the attestation is stored.
4. **Deterministic stage.** It writes an immutable `deterministic` result row before any model call.
5. **Grader stage.** It runs only for `method=grader` criteria, and only if no required deterministic criterion failed. A cross-check second run is triggered by policy (§I).
6. **Final result.** Code aggregates the stage rows into the `final` row. It sets `outcome` and `demonstration_effect`, renders the report, and snapshots the record, all in one transaction.
7. **Evidence writer.** Runs in the same transaction, idempotently (§O). State is recomputed on read.

**Outcomes:** `PASSED | NEEDS_WORK | PROVISIONAL | HUMAN_REVIEW_REQUIRED | UNABLE_TO_ASSESS`.

**`demonstration_effect`** is separate from the outcome: `counts_toward_demonstrated | counts_toward_practiced_only | formative_only | none`. This keeps "PASSED" honest for assisted work.

**Aggregation rules (code, not the LLM):**

1. A required deterministic criterion that is `NOT_MET` gives `NEEDS_WORK`, and the Grader is skipped (definition may override).
2. A required grader criterion that is confidently `NOT_MET` or `PARTIAL` gives `NEEDS_WORK`.
3. A low-confidence result or cross-check disagreement gives `PROVISIONAL`. If the automatic resolution budget is exhausted, it becomes `HUMAN_REVIEW_REQUIRED`.
4. Provider failure with no usable result leaves the attempt in `AWAITING_GRADING` (retry-safe). A permanent inability to grade gives `UNABLE_TO_ASSESS`.
5. All required criteria met gives `PASSED`.

---

## H. Assessment Types

Each kind is an `assessment_kind` on a definition. No concept needs every kind.

| Kind | Verifies | Deterministic legs | Grader legs | Evidence type | Fresh challenge |
|---|---|---|---|---|---|
| **A. Knowledge check** | Recognizes and explains the concept | Choice, numeric, ordering, and classification items from reviewed `learning_items`, scored with the existing `choice_spec` logic; explicit pass rule (e.g. "4 of 5") | None | `knowledge_check` | Fresh draw from the reviewed pool, excluding recently seen items |
| **B. Explain-back** | Explains what was built and why | Structural checks; record-grounded pointer selections (e.g. "select the failing case from your real results") | Accuracy vs reference key points, reasoning, limits, transfer | `explain_back` (new) | Always fresh, authored prompts |
| **C. Modification** | Adapts a working solution to a new requirement | Challenge test-set run on the learner's new agent version; results from MA6 | Optional justification | `modification` (new) | Seeded variant |
| **D. Reproduction** | Re-applies the idea to different inputs or context | Run on fresh inputs from the pool | Optional | `reproduction` (new) | Seeded variant |
| **E. Debugging** | Diagnoses and repairs a seeded fault | Faulty artifact issued from authored set; retest passes; failure→hypothesis→change→re-run chain exists | Diagnosis quality | `debugging` (new) | Seeded fault |
| **F. Project / capstone** | Integrates concepts in a working project | Milestones evidenced (not just "completed"); test results; frozen brief criteria; README structure | README, decisions, explain-back set | `project_assessment` (new) | Modification challenge mandatory for capstone |
| **G. Experiment interpretation** | Correctly reads real experiment results | Structured claim vs platform results (e.g. "which model passed more cases?"); n and slice cited | Faithfulness to results; appropriate hedging; limits | `interpretation` (existing) | Own experiment, or authored result pack for independence |

Extension seam, not in the 5C minimum: a deterministic `record_observation` kind would give Academy concepts a real `observation` leg (SRC-4). Until then, Academy concept versions declare explicit requirement sets (or alternatives) using evidence types that 5C or existing flows can produce.

---

## I. Grader Agent Contract

**Identity.** `Agent(name="Academy Grader", role="grader")` in the AIL system project. It follows the Professor's reserved-ID pattern (`…0108`), with the exact ID chosen at implementation. It has no tools, no retrieval, no web, and no write path.

**Run role.** Add `AgentRunRole.GRADER`. Rationale: "never hide which model performed a grading judgment," and tests must assert that a grading run is not a Professor or Evaluator run. Cost: `agent_runs.role` CHECK rebuild, isolated in its own migration (§AC). Fallback if that risk is rejected: reuse `EVALUATOR`, distinguished by Agent identity. That fallback loses role-level provenance and mixes grader runs with MA6 evaluator runs in any role filter.

**Versioning.** Immutable `PromptVersion` and `AgentVersion` rows, the same discipline as every agent.

- The agent prompt is generic. The rubric and criteria arrive per run through `extra_context`, mirroring `build_evaluator_extra_context`. This keeps prompt versions rare and criteria versioned in the definition.
- Result rows pin `grader_agent_version_id` and `grading_contract_version`.
- **Release gate:** before a Grader version or model policy becomes ACTIVE, an offline golden-set run confirms expected findings on authored responses per definition (2 clear pass, 2 clear fail, 2 borderline). This is a test and script gate, not a schema.

**Model selection.**

- Required capability: `structured_output_support`.
- Use the lowest available temperature.
- The Grader's `model_policy` is set on the Agent Version (manual pin recommended for consistency, or `auto` under MA8). A staging override mirrors the Professor's `professor_staging_provider_model_id`.
- The cross-check pass uses a manual override to a different provider model, chosen from the router's own eligible set with the first run's model excluded. There is no new router feature.
- Grader runs never create `EvaluationRun` rows, so they never enter MA8 routing evidence.

**Execution path (reuse of the MA6 pattern, not Professor's).**

1. Create a dedicated bookkeeping Task, TaskRun and AgentRun with role `GRADER` inside the AIL project.
2. `task.title` is scaffolding only. **No learner content is written to `tasks`** (SRC-7).
3. `AgentRun.input_context_json` holds pointers only: `{"kind":"assessment_grading_request","assessment_attempt_id","grading_round","packet_hash"}`.
4. A new `execution_service._build_extra_context` branch assembles the allowlisted packet at run time from user-scoped, frozen attempt rows. A crash-reclaimed job rebuilds the identical prompt.
5. The existing executor runs it. MA7.8 recovery ("recover worker-interrupted agent runs without replaying provider calls") applies unchanged.

**Structured output contract** (`grading_contract_v1`, strict, fail-safe):

```json
{ "criteria": [{
    "key": "…",                            // exactly the grader-method keys; none missing, duplicate, or extra
    "finding": "met|partial|not_met|not_applicable",
    "confidence": "high|medium|low",
    "rationale": "…",
    "quotes": ["…verbatim substrings of the learner response…"]
}] }
```

- Validation: exact key set; quotes must be verbatim substrings of the frozen response; no extra fields.
- Forbidden fields: score, percentage, rank, mastery, "you have demonstrated", advice, next steps, state claims.
- Any violation is a failed grading run. It never infers a finding.
- **Confidence is categorical.** The model never emits a float. The platform maps `high/medium/low` to fixed numeric values only to fill the existing `learning_evidence.grader_confidence` column. The UI shows the label, never a pseudo-probability.

**Provenance.** The result row stores the Grader `agent_run_id`s and `agent_version_id`. Model, provider snapshot, tokens, cost and latency are reachable through `agent_runs → model_calls → provider_model_snapshots` without duplication. The raw output is the run's own hashed artifact.

**Failure behavior:** see §AB.

---

## J. Grader Context Isolation

The packet is built by an **allowlist assembler** (fields are named in, never filtered out). It takes an `attempt_id` only and follows only frozen manifest references.

| Item | Grader sees? | Reason |
|---|---|---|
| Rubric criteria (grader-method only) and level anchors | **Yes** | That is the task |
| Challenge statement exactly as issued to this learner | **Yes** | Needed to judge the response |
| Frozen learner response (verbatim, in delimited data blocks) | **Yes** | Injection defense: response is data, never instructions |
| Concept Version plain and technical text; authored reference key points | **Yes** | Reference standard |
| Deterministic **facts** relevant to a criterion (e.g. "8 of 10 cases passed, platform-verified") | **Yes**, labeled immutable | Lets it cite, never contradict |
| Experiment results (platform observation) and the learner's conclusion, as separate labeled blocks | **Yes** for kind G only | Required for interpretation |
| Project brief / requirements relevant to a criterion | **Yes** | Context |
| **Assistance level (H0–H5)** | **No** | Independence is applied by deterministic policy afterward. Showing it invites harsher or gentler grading of the same words. |
| Mentor conversation, hints, hint content | **No** | Bias, and leaks hint text into the judgment |
| Professor transcripts | **No** | Same |
| Learner profile, goals, level, interests, identity, email | **No** | Irrelevant, bias, privacy |
| Learner State, prior results, prior attempts, retry status | **No** | Blind to history |
| The stakes ("this decides DEMONSTRATED") and unmet requirement gaps | **No** | Avoids give-them-the-pass pressure |
| Unrelated projects, tasks, artifacts, repositories | **No** | Privacy |
| Other criteria's results the current criterion doesn't declare it needs | **No** | Minimal facts |

Rules:

1. The packet is never stored separately. It is reconstructed from the attempt and hashed as `packet_hash`.
2. The Grader has no tools and no retrieval, so it cannot widen its own context.
3. The cross-check run receives an identical packet and does not see the first run's output.
4. The learner response is treated as untrusted input, and injection attempts are handled by delimiting plus schema validation.

---

## K. Deterministic vs AI Responsibilities

```
AUTHORIZATION → STRUCTURAL VALIDATION → MANIFEST/HASH VERIFY → DETERMINISTIC REQUIREMENT CHECKS
→ EXECUTION-EVIDENCE CHECKS → ASSISTANCE/INDEPENDENCE CLASSIFICATION
→ GRADER (judgment-only criteria) → RESULT VALIDATION → OUTCOME AGGREGATION (code)
→ EVIDENCE WRITER → LEARNER STATE (on read) → REPORT
```

| Deterministic (never delegated) | Grader (judgment only) |
|---|---|
| Required artifacts and sections exist; milestones have qualifying evidence (not just a "complete" click) | Is the explanation accurate against reference key points? |
| Referenced run exists, is terminal, belongs to this learner, was created after the challenge started, and its inputs hash matches the issued challenge | Does the reasoning follow from the results? |
| Tests really passed (MA6 `EvaluationRun` findings, task-run results) | Are limits and trade-offs stated? |
| Execution verification level (from platform records, not 5B fields) | Is the diagnosis of a failure sound? |
| Assistance level and independence class | Is the experiment conclusion faithful and appropriately hedged? |
| Template, concept and program version identity; prerequisites; experiment exists, concluded, owned | Quality of README or justification |
| Submission unchanged (hash); numeric, schema, and choice matches | Transfer ("how would you adapt this to X?") |

**Hard rules:**

- The Grader receives only `method=grader` criteria and cannot see or rewrite deterministic results.
- A `NOT_MET` deterministic required criterion forces `NEEDS_WORK` whatever the model says.
- A grader response that references or restates a deterministic criterion key is rejected as invalid.

**Deterministic check catalog** (data-driven handlers keyed by `check_spec.type`, not per-concept service logic): `artifact_present`, `section_present`, `milestones_evidenced`, `evidence_exists`, `run_exists_owned_terminal`, `run_after_challenge_start`, `inputs_match_challenge`, `evaluation_run_findings`, `experiment_concluded`, `schema_valid`, `numeric_match`, `choice_match`, `pointer_valid`, `length_bounds`, `cost_within`, `manifest_unchanged`.

---

## L. Assistance / Independence Policy

Facts stay visible: the H-level and verification level are stored and shown. Policy classifies each evidence row into `full | partial | formative` with a reason code.

**Source work from a mentored project (5B provenance):**

| Level | Class | PRACTICED | A DEMONSTRATED leg | Next step |
|---|---|---|---|---|
| H0 independent | Full | Yes | Yes if platform-verified | Fresh challenge per definition (`optional` by default) |
| H1 conceptual clue | Full | Yes | Yes if platform-verified | Same |
| H2 pointer / error explained | Full | Yes | Yes if platform-verified (**platform floor**) | Same |
| H3 partial structure | **Partial** | Yes (5B ≤H4 rule) | **No.** Needs an independent leg. | Fresh challenge **required** |
| H4 guided walkthrough | **Formative** for demonstration | Yes (5B ≤H4 rule) | No | Independent retry required |
| H5 solution | **Formative** | **No** (5B blocks it) | **Never** | Study Mode variant, then a fresh challenge |

**Fresh assessments in Assessment Mode.** The Mentor is locked, so platform-known assistance is H0 by construction. The row records `independence_basis = assessment_mode` plus the learner's declaration. A fresh low-assistance result can qualify even after H3–H5 source work. Study Mode never grants DEMONSTRATED on its own.

**Verification.**

- `platform_verified` and `sandbox_verified` (post-MA9) qualify.
- `self_reported` never satisfies a deterministic leg, and reaches PRACTICED only.
- `on_demo_data` never counts toward DEMONSTRATED.
- Generated questions never count toward DEMONSTRATED.

**Declaration.** At submit the learner attests one of `no_external_help | used_docs | used_ai_assistant | other`. It is stored as a self-report and never verified. It never advances the ladder. A declared `used_ai_assistant` makes the attempt formative: it is recorded and useful for feedback, but does not qualify. Honest learners are not punished for honesty.

**Honest limit.** External AI use cannot be detected, and 5C does not try. Mitigations are fresh seeded challenges, record-grounded pointers, transfer questions, the deterministic modification leg, and the existing rule that AI-graded evidence alone never demonstrates.

---

## M. Fresh Challenge Design

**When required (deterministic):** `fresh_required` on the definition is `always | if_assisted | never`, default `if_assisted`.

- `if_assisted` triggers when the source-work class is partial or formative, or when Study Mode was used.
- Capstone is `always`.

**Construction:**

1. **Authored variant pool.** `challenge_spec_json` holds `variants[]` (each with `variant_key`, parameters, requirement text, deterministic `check_spec`, and reference key points) plus input pools. The pool is at least 3× the draw size, enforced at publish.
2. **Seeded selection.** `seed = sha256(user_id | definition_id | attempt_seq)`. The draw excludes variants already issued to this learner for this definition, and excludes any variant shown in Study Mode. The instance is stored in `challenge_instance_json`, so it is reproducible.
3. **Platform-verified execution.** For C/D/E the learner works in the platform (a new prompt or agent version on their own learner agent). Checks verify runs occurred after the attempt start, that run inputs hash to the issued challenge, and that outputs come from the learner's own agent version created during the attempt.
4. **No AI-generated challenges toward DEMONSTRATED.** They may exist only as labeled formative practice.
5. **Retake.** 12-hour cooldown (the spec §19.2 default), a different draw, and no attempt cap. Fall back to a reseeded draw only when the pool is exhausted.
6. **No copy detection, no proctoring.**

---

## N. Demonstration Requirement Model

`ConceptVersion.evidence_requirements` remains the single rule payload. It is immutable per version and pins the rules in force when evidence was earned. It gets a backward-compatible **v2** extension.

**Existing keys (unchanged):** `requires_all`, `requires_any_of`, `alternative`, `evidence_type`, `min_passed`, `allow_generated`.

**New optional per-requirement constraints:**

- `max_assistance`: `h0 | h1 | h2`. It can only tighten the platform floor.
- `min_verification`: `platform_verified | sandbox_verified`.
- `grader_in`: allowed `GradingMode`s for this leg, e.g. `["deterministic"]`.

**Platform floor**, always applied and not configurable:

1. `superseded_by_id` rows are excluded.
2. `on_demo_data` rows are excluded from DEMONSTRATED.
3. Rows with H5, or with `assistance_level > H2`, or with `self_reported` execution, never satisfy a DEMONSTRATED leg.
4. The existing rule stands: a DEMONSTRATED requirement set needs at least one satisfying `deterministic` row.

Assessment definitions declare which concept and evidence type they can produce. Any published definition linked to the concept can fill a matching leg. Concepts never name assessments, so there is no hard-coding and content can evolve.

```json
// Structured Output (mechanism/skill)
{"requires_all":[
  {"evidence_type":"knowledge_check","min_passed":1},
  {"evidence_type":"modification","min_passed":1,"max_assistance":"h2","min_verification":"platform_verified","grader_in":["deterministic"]},
  {"evidence_type":"explain_back","min_passed":1}]}

// Choosing a Model (operational)
{"requires_all":[
  {"evidence_type":"lab","min_passed":1,"min_verification":"platform_verified"},
  {"evidence_type":"interpretation","min_passed":1},
  {"evidence_type":"knowledge_check","min_passed":1}],
 "alternative": {"requires_all":[…]}}    // for gated capabilities (MA9)

// Iterating & Debugging
{"requires_all":[
  {"evidence_type":"debugging","min_passed":1,"min_verification":"platform_verified","grader_in":["deterministic"]},
  {"evidence_type":"explain_back","min_passed":1}]}
```

**Formula (implemented only in `LearnerStateService`):**

```
DEMONSTRATED = primary (or alternative) requirement set satisfied by rows
               that pass the platform floor and the per-leg constraints
             ∧ ≥1 satisfying row with grader=deterministic
             ∧ satisfying rows' provenance valid (not superseded, not demo, verification present)
```

- **Publish-time validation** (new): a concept version that lists an AI-graded leg must also list a deterministic leg.
- **Program `completion_rules`** are unchanged and read Learner State.

---

## O. Evidence Qualification

```
PROJECT ACTIVITY → CANDIDATE EVIDENCE (5B, untrusted claims)
                      └─ 5C re-derives facts ─► DETERMINISTIC RESULT
ASSESSMENT ATTEMPT ─► RESULT(final, passed) ─► QUALIFICATION ─► LEARNING EVIDENCE ─► STATE (recomputed)
```

**Extend `EvidenceType`:** `explain_back`, `modification`, `reproduction`, `debugging`, `project_assessment`. `interpretation` and `knowledge_check` are reused. Extend `EvidenceRefType` with `assessment_result`. This is one CHECK rebuild of `learning_evidence`, aligned with 5A CR-2's intent.

**Evidence writer rules:**

- Only a `final` result with `outcome=PASSED` writes evidence, and only for concepts linked to the definition. Each concept's `criterion_keys_json` says which required criteria decide that concept's row.
- Failures write **no** LearningEvidence. The immutable result is the record, following the existing `ReviewAttempt` precedent. Failures never lower the ladder.
- **Row fields:**
  - `evidence_type` is the definition's `produces_evidence_type`.
  - `grader` is `deterministic` if the deciding legs were deterministic, `ai_rubric` if judged, `human` after an override.
  - `passed` is true.
  - `score` is `{"criteria_met": n, "criteria_total": m}`, never a percentage.
  - `assistance_level` is H0 for Assessment Mode, or inherited for source-work definitions.
  - `execution_verification` comes from platform records.
  - `ref_type` is `assessment_result` and `ref_id` is the result id.
  - `concept_version_id` is the pinned link version.
  - `grader_confidence` uses the categorical mapping.
- **Idempotency:** a partial unique index on `(user_id, ref_id, concept_id)` where `ref_type='assessment_result'`. The writer is find-or-insert in the finalize transaction, so replays create nothing new.
- **Version pinning:** if a newer concept version is published mid-attempt, evidence is recorded against the pinned version. The existing `CHANGED` overlay then handles the drift.
- **Supersession:** a human override that reverses an earlier pass appends a new row. The `LearningEvidenceService` gains one narrowly scoped `supersede(old_id, new_id)` pointer setter, callable only by the review service. This is the already-documented convention, and the old row stays visible.

---

## P. Learner State Integration

5C **never computes state**. `LearnerStateService` stays the only authority. Required corrections (P0-1):

1. Apply the platform floor and per-leg constraints (§N) to requirement matching. Current state computation ignores assistance, verification, demo, and supersession (SRC-2, SRC-3).
2. Honor `superseded_by_id`.
3. Make CHANGED **multi-hop**. Today it checks only the current version's own severity (`_changed_since` admits the gap). It should be: DEMONSTRATED and any *material* version newer than the latest demonstrating version.
4. Parse the v2 requirement keys.

**Effect of correction 1.** Existing H3/H4 `LAB` rows will stop satisfying DEMONSTRATED legs, so some concepts may regress. Before merge, run a **dry-run diff report** (which learner-concept states would change) against a copy of a real DB. Never run destructive migration tests on `data/multi_agent_platform.db` (see memory).

**After a result,** the API returns `state_before` (computed just before the evidence append, informational) and `state_after` (recomputed). The report shows "what changed in my Learner State" from these.

---

## Q. Explain-Back Assessment

- Prompts come from the definition's authored bank, never from the client. Each prompt has a `prompt_key`, level anchors, and reference key points.
- **Record-grounded pointers.** The learner selects a real item first (e.g. a failing case from their own results, validated deterministically), then explains it in free text. This makes generic pasted text weak and gives the Grader concrete material.
- **Capstone set** (5A §J.4): 5 prompts: 2 on design choices, 1 on a failure and its fix, 1 on limits, 1 transfer.
- **Assessment Mode.** The Mentor is locked. The learner may reread lessons and their own artifacts. Answers are frozen at submit.
- **Criteria** are typically accuracy, grounding in own results, reasoning ("why"), and limits or transfer. Any deterministic legs use pointer validity and length bounds only.
- The evidence type is `explain_back` (AI-graded or human). It can never be the sole leg for DEMONSTRATED.

---

## R. Project / Capstone Assessment

The assessment definition is `project` or `capstone`, linked to a pinned project template version. It consumes the 5B handoff plus fresh work.

- **Deterministic:** milestones have platform-derived evidence; MA6 test-set findings meet the frozen brief criteria (from `project_attempts.brief_snapshot`); README and requirements sections exist; cost is within the brief's bound (from usage).
- **Grader:** README quality, model-choice justification (cites experiment ids, which a deterministic check verifies), and the capstone explain-back set.
- **Fresh modification challenge:** mandatory for capstone, at Assessment Mode independence (5A §J.4).
- **Capstone rule:** at least 70% of required criteria are deterministic, enforced at definition publish (from 5A §J.4). Criteria were frozen at brief acceptance in 5B, and 5C never edits them.
- **Learner-defined capstones** are assessed against their frozen brief snapshot.
- **MA9 gating.** Definitions declare `requires_platform_capability`. Sandboxed test execution and code runs are MA9-gated. Local-code work is `self_reported` and reaches PRACTICED only. The no-code path is fully assessable pre-MA9. No runtime is built.
- **Effects:** `project_assessment` evidence per assessed concept; `project_attempts.status` → `PASSED` or `NEEDS_WORK`. Enrollment completion remains the 5A rule over Learner State.
- **Exceptional review:** a `capstone_exception` review is available on request or on a `PROVISIONAL` outcome. It is not mandatory by default (owner decision D-4).

---

## S. Personal Lab Integration

The Lab is reused unchanged. Three layers stay separate and never overwrite each other:

| Layer | Source | Storage |
|---|---|---|
| **Experiment result** = platform observation | `experiments`, `experiment_task_runs`, MA6 runs | Read-only |
| **Learner conclusion** = human interpretation | `experiments.conclusion_text/type`, `project_experiment_links.learner_decision` | Untouched by 5C |
| **Grader assessment** = AI judgment | `assessment_results` | Separate rows |

Kind G is a structured interpretation. The learner selects claims the platform can verify against results (which variant passed more cases, with n and slice cited). Those are deterministic. They then write the limits and the "would this generalize?" reasoning, which the Grader judges for faithfulness and hedging. It writes `interpretation` evidence. For independence, definitions may use an authored result pack instead of the learner's own experiment. The report renders all three layers side by side with labels.

---

## T. Human Review / Dispute

**Human Approval analysis (§36).** Reuse is not safe, so I recommend a purpose-built table that copies the pattern:

1. `ApprovalScope` has `task_run | agent_run | workflow_node_run | artifact`, with no learner-record scope.
2. `resolve` authorizes by project `MODIFY`. Every learner is OWNER of the shared AIL project (SRC-7), so approvals for one learner's grader run would be listable and resolvable by other learners.
3. Approvals are binary approve or reject. Review needs four decisions plus a rationale.

**Reused pattern:** compare-and-swap resolution, an action fingerprint binding the review to the exact result and manifest hash, and a write-once decision.

**Flow:**

1. **Request.** The learner gives a required reason, and an explicit consent flag is recorded (this is the only thing that lets a reviewer see their submission). Triggers are `learner_dispute`, `low_confidence` (system), and `capstone_exception`.
2. **Reviewer.** Must be an org `OWNER` or `ADMIN` and not the learner. The reviewer sees the frozen packet, rubric, deterministic facts, grader outputs, manifest, and hashes. The reviewer does not see unrelated learner data.
3. **Decision:** `confirm | override_pass | override_needs_work | new_assessment`, with a required rationale.
4. **Result.** The decision appends a `human` result row (`supersedes_result_id`). An override to pass writes evidence with `grader=human` through the same idempotent writer. An override that reverses an earlier pass uses the supersede pointer. `new_assessment` issues a fresh attempt with `origin=human_requested`.
5. **Audit.** `AuditService` events, plus an immutable review row. The prior result and evidence stay visible.

One open review per attempt is enforced by a partial unique index. Withdrawal is allowed until a reviewer is assigned.

---

## U. Retry / Remediation

```
NEEDS_WORK → specific gap (criterion key) → deterministic remediation map → RETRY (new attempt)
```

- **Remediation map.** Each criterion carries `on_not_met: [{kind: learning_item | milestone | definition | experiment, ref}]`, or an explicit `none`. Deterministic; no LLM chooses the next step. Kinds: revisit a Learning Item, return to a Build With Me milestone, run another experiment, retry explain-back or knowledge check, or take an independent variant.
- **Retry granularity is per definition (concept level), never program level.** A retry is a new attempt with `previous_attempt_id` and a fresh challenge draw. Earlier attempts and evidence stay in history.
- **Cooldown** 12h by default (definition may override). No attempt cap and no "failed" state.

**Professor after assessment (roles stay separate):**

- A new read-only Professor target `assessment_result` and an intent (e.g. `HELP_ME_AFTER_ASSESSMENT`) extend the 4C contract. This is a change request against 4C.
- The Professor sees the report's platform-evidence and grader-judgment sections plus the remediation map. It explains and recommends.
- The 4C validator gains a rule: outcome wording must match the result's `outcome` enum, and no state or evidence claims are allowed. The Professor cannot rewrite or reinterpret a result.
- The Grader has no advice field and never coaches. Coaching text is generated on demand, labeled AI_EXPLANATION, and never stored inside the result.

---

## V. Assessment Report

The report is a rendered view of the final result with four clearly separated blocks, each with a distinct label:

1. **Factual platform evidence.** Deterministic checks, verification levels, run and evaluation references, assistance provenance H0–H5 (visible), and the independence classification with its reason code.
2. **Grader judgment.** Per-criterion finding, confidence label, rationale, and verbatim quotes. It shows the Grader agent version, model and provider, rubric and definition version, and whether a cross-check ran.
3. **Learner reflection.** The learner's own words: attestation, optional reflection, and their earlier formative explain-back. Reflection is stored but never graded.
4. **Professor coaching.** Separate panel, on demand, labeled, never part of the result.

It answers the seven questions in order:

1. What I demonstrated: outcome and `demonstration_effect`.
2. What evidence proved it.
3. Where I needed help.
4. What I did independently.
5. What still needs work: gaps plus the remediation map.
6. What to practice next.
7. What changed in my Learner State (`state_before → state_after`), and what to revisit later (the retention date, read from `ReviewAssessor`).

---

## W. Demonstration Record / Portfolio

**Contents:** concept and concept version; definition and version; rubric version; project and template version; the evidence rows produced, each with its assistance and verification provenance; execution verification statements; assessment date; Grader agent version, model and provider; cross-check status; artifact and run references. A "verified vs self-reported" statement is mandatory.

**Storage.** The record is `record_snapshot_json` plus `record_hash` inside the immutable final result. It needs no artifact-table change. Export is a deterministic Markdown or JSON render from that snapshot.

**Status** is derived on read: `valid | superseded-by-review | changed-since (CHANGED) | review-due`. It shows current status without editing history.

**Language** is validator-enforced. The title is "Demonstration Record," with the line "Not a certificate or credential." Blocked terms: certified, accredited, mastered, expert, professional-level. It is private by default and owner-exportable. No public share links in 5C. 5A's portfolio page can embed records.

---

## X. Review / Retention Integration

Reuse AIL.4B unchanged. No second spaced-repetition system.

1. Passing assessment evidence is qualifying evidence under `evidence_qualifies`, so it resets the retention clock automatically.
2. `REVIEW_DUE` and `REVIEW_FAILED` stay derived from 4B `ReviewAttempt` rows. 5C does not write review attempts.
3. Interval doubling stays tied to successful 4B reviews. Assessments reset the baseline but do not extend spacing.
4. A 5C reassessment of a concept flagged `CHANGED` is an assessment with `origin=changed_knowledge`. A failure is a `NEEDS_WORK` result. The concept stays historically DEMONSTRATED plus `CHANGED`, with no new overlay.
5. The report shows the next review date from a read-only `ReviewAssessor.assess`.

---

## Y. CHANGED Knowledge Handling

- **Coexistence.** Historical demonstration and current knowledge change both stay true and visible. Evidence rows keep their `concept_version_id`. State is DEMONSTRATED plus the `CHANGED` overlay. The record reads: "Demonstrated against v3 (date); current v5 has a material change: <change_note>."
- **Detection.** Uses the multi-hop fix in §P.
- **Definition impact.** Definitions pin concept versions. When a material concept version is published, a deterministic **definition impact report** lists which definitions link the concept, so owners can publish new definition versions. Nothing republishes automatically, and old results stay valid for the pinned version.
- **Reassessment** is offered through the Assessment Center (§AE-A) with `origin=changed_knowledge`. The old demonstration is never erased.

---

## Z. Privacy / Authorization

- **Ownership.** Every 5C row is user-scoped (`users.id`), and every query filters by the attempt's `user_id`. Project RBAC is never used as the isolation boundary (SRC-7). Cross-user tests are mandatory.
- **Referenced records.** Each record in a manifest must resolve through the learner's own project attempt, experiment, or learner agent. Org Owner or Admin gets no learner visibility by role.
- **Grader.** It reads only the frozen allowlisted packet. There is no bulk scan, no unrelated project or task or artifact or repository or chat access, no inferred interests, and no background profiling.
- **Learner content never lands in `tasks`** rows visible to other project members. Note that the raw Grader output artifact contains learner quotes. It is stored per run in the AIL project, so implementation must confirm artifact and run visibility endpoints filter by owner. Otherwise the Professor's existing `tasks.requirements` exposure recurs here.
- **Human reviewers** see a submission only with per-review consent, and every access is audited.
- **Export and delete.** Extend `LearnerProfileService.export_learner_data` and `delete_learner_data` to cover the five new tables and Grader artifacts.
- **Retention.** Results and reviews are kept until learner deletion. Unsubmitted drafts are purged after 30 days (attempt row kept as `ABANDONED`).
- **No surveillance.** No webcam, screen, keystroke, focus, clipboard, or timing telemetry. Time boxes use only server-side `started_at` and `expires_at`.

---

## AA. Model / Cost / Provenance

- **Reuse:** Agent Registry, `AgentRun`, `ModelCall`, Model Registry, MA8 policy, `usage_events`, budgets, Flight Recorder.
- **Budget.** Grader runs bill against the learner's user-scoped budget in the AIL project. The estimate uses the existing estimator. Cost display keeps the existing labels: exact, estimated, unknown. Cost is never fabricated. Unknown pricing is never treated as free, per the existing model-resolution rule.
- **Cost bound.** A grader-method criterion set is short text, so the cost is small. It is bounded to at most 2 grader runs per round, plus one infrastructure retry.
- **Flight Recorder events** per stage: `assessment.submitted`, `.deterministic_completed`, `.grading_requested`, `.grading_completed|failed`, `.finalized`, `.evidence_written`.
- **Provenance is never hidden.** Reports and records show the model and provider that judged each criterion.
- **Consistency.** A Grader Agent Version with a manual model pin gives stable standards. Any change is a new version, gated by the golden-set release test.

---

## AB. Failure / Recovery

| Failure | Behavior |
|---|---|
| Provider error or timeout in a grader run | Deterministic result row is already durable. Attempt stays `AWAITING_GRADING`. No state change. Retry is idempotent (`assessment:{attempt}:grade:{round}`). |
| Malformed or invalid Grader output | Counts as a failed run. One automatic retry (same model), then the alternate model. Then `UNABLE_TO_ASSESS` or `HUMAN_REVIEW_REQUIRED`. Never infers a finding. |
| Budget exhausted | Grading pauses with a clear message. Deterministic results and the submission stay valid. Resumable. |
| Worker crash mid-run | MA7.8 recovery applies. Grader runs are ordinary agent runs and are not replayed against the provider. |
| Crash mid-finalization | Finalize, evidence write, and status change are one transaction. Replay finds the existing final row and evidence. |
| Duplicate submit or finalize | Compare-and-swap on attempt status. Replay returns the same result. |
| Definition retired or concept version changed mid-attempt | Attempt is pinned and can complete. The `CHANGED` overlay applies afterward. |
| Model deprecated mid-round | New round with a new run under the pinned Grader version's policy. |

**Idempotency keys:**

- Create attempt: `Idempotency-Key`, plus a partial unique index for one active attempt per (user, definition).
- Stage rows: unique on `(attempt_id, result_kind, round)`.
- Final row: partial unique on `attempt_id` where `result_kind='final'` and `supersedes_result_id IS NULL`. Also unique on non-null `supersedes_result_id`.
- Evidence: `(user_id, ref_id, concept_id)` where `ref_type='assessment_result'`.
- Review: one open per attempt.

---

## AC. Schema Changes

Five new tables, kept to the minimum. All follow existing conventions: UUID keys, `sa_enum`, `users.id` ownership, partial unique indexes for NULL-safe uniqueness.

| Table | Why needed / why existing entities can't hold it | Key columns | Immutability and ownership |
|---|---|---|---|
| **`assessment_definitions`** | No existing versioned, immutable, learner-facing rubric plus challenge unit. `learning_items` is a mutable single row. `project_templates` covers projects only. `EvaluationDefinition` is project-scoped and agent-output-shaped. | `definition_key`, `version` (unique together), `status` (draft/published/retired), `assessment_kind`, `title`, `instructions_md`, `produces_evidence_type`, `project_template_id` (nullable), `criteria_json`, `challenge_spec_json`, `independence_policy_json`, `grading_policy_json`, `allowed_resources_json`, `requires_platform_capability`, `author_user_id`, `published_at` | Row-per-version, immutable once published (same as `project_templates`). Retained. |
| **`assessment_definition_concepts`** | Owner rule: relationships are rows, not JSON. Pins the concept version each definition assesses. | `definition_id`, `concept_id`, `concept_version_id`, `role`, `criterion_keys_json` | Immutable with the definition. |
| **`assessment_attempts`** | Learner-owned aggregate with lifecycle. Nothing existing holds a challenge instance, frozen manifest, or attestation. | `user_id`, `definition_id`, `origin`, `project_attempt_id`, `source_submission_id`, `enrollment_id`, `previous_attempt_id`, `status`, `challenge_seed`, `challenge_instance_json`, `draft_json`, `submission_json`, `submission_hash`, `input_manifest_json`, `input_manifest_hash`, `attestation_json`, `grading_round`, `grader_agent_version_id`, `started_at`, `submitted_at`, `finalized_at`, `expires_at`, `idempotency_key` | `draft_json` mutable only in DRAFT. Everything else write-once at submit. Partial unique: one active attempt per (user, definition). Cascades on user delete. |
| **`assessment_results`** | Append-only stage and final outputs. `learning_evidence` holds only passing evidence, and `evaluation_runs` is bound to agent-run subjects. | `attempt_id`, `seq`, `round`, `result_kind` (deterministic\|grader\|final\|human), `outcome` (final and human only), `demonstration_effect`, `criteria_json`, `facts_json`, `gaps_json`, `remediation_json`, `report_json`, `grader_agent_run_ids_json`, `grader_agent_version_id`, `grading_contract_version`, `review_id`, `supersedes_result_id`, `record_snapshot_json`, `record_hash`, `created_at` | **Never updated.** CHECKs: final rows have `outcome`; human rows have `review_id`; grader rows have run ids. Unique `(attempt_id, seq)`. Cascades on user delete. |
| **`assessment_reviews`** | Consent-bound human review with reversible decisions and write-once resolution. `approvals` cannot safely or expressively carry it (SRC-7). | `attempt_id`, `result_id`, `user_id`, `trigger`, `status`, `reason_text`, `consent_shared_at`, `reviewer_user_id`, `decision`, `decision_rationale`, `fingerprint`, `requested_at`, `resolved_at`, `resulting_result_id` | Decision written once by compare-and-swap. Partial unique: one open review per attempt. |

**Changes to existing tables:**

| Change | Type | Risk |
|---|---|---|
| `EvidenceType` +5 values and `EvidenceRefType` +`assessment_result`, plus the partial unique index on `learning_evidence` | CHECK rebuild of a table with real data | **Highest.** Own migration. |
| `AgentRunRole` +`grader` | CHECK rebuild of `agent_runs` | High. Own migration. |
| `ConceptVersion.evidence_requirements` v2 keys | JSON contract only | None |
| `artifacts` | No change | None |

**Migration strategy:**

1. **R1** (additive): the five new tables. Low risk.
2. **R2:** `learning_evidence` enum extension plus index. Separate revision.
3. **R3:** `agent_runs` role. Separate revision.

**Controls, all mandatory:**

- Full-chain tests on `tmp_path` databases only.
- A rehearsal on a copy of the staging volume before any deploy.
- Foreign-key check after each rebuild.
- Keep the current `env.py` logging fix.
- Never `alembic downgrade base` on the real DB.
- No deploy without fresh authorization.

**Fallback** if R2 risk is judged unacceptable: keep existing enum values, add a nullable `assessment_kind` string column to `learning_evidence`, and match legs on `(evidence_type, assessment_kind)`. That blurs `modification` into `lab`, so it is second choice.

---

## AD. API Design

Prefix `/academy/assessments`. State-changing POSTs accept `Idempotency-Key` through the existing `IdempotencyService`. Not duplicated: Learner State reads, Lab endpoints, the 5B submission read, and Learning Item sources.

| Purpose | Endpoint |
|---|---|
| Assessment Center: ready work, in-progress, results, remediation, review-due and changed | `GET /center` |
| Definition detail (instructions, allowed resources, what is assessed) | `GET /definitions/{key}` |
| Readiness pre-flight for a definition and optional project attempt | `GET /definitions/{key}/readiness` |
| Start attempt (issues challenge, enters Assessment Mode). Also used for retry via `previous_attempt_id`. | `POST /attempts` |
| Retrieve attempt (state, challenge, draft, allowed resources) | `GET /attempts/{id}` |
| Save draft, explain-back or challenge responses | `PUT /attempts/{id}/draft` |
| Submit: freeze, hash, attest, run deterministic stage, dispatch Grader if needed | `POST /attempts/{id}/submit` |
| Resume or retry grading (idempotent per round) | `POST /attempts/{id}/grade` |
| Retrieve result and report | `GET /attempts/{id}/result` |
| Request human review | `POST /attempts/{id}/review` |
| Reviewer queue, detail, decision | `GET /reviews`, `GET /reviews/{id}`, `POST /reviews/{id}/decision` |
| Demonstration records and export | `GET /records`, `GET /records/{result_id}?format=md\|json` |
| Author and publish definitions (owner) | `POST /definitions`, `POST /definitions/{id}/publish` |

**Changes to existing services, not new endpoints:**

- An `AssessmentModeGuard` consulted by `ProjectMentorService.ask` and Professor execution. It blocks requests whose concept or project matches an active DRAFT attempt (409 `assessment_mode_active`).
- The Professor `assessment_result` target.
- 5B `submit` and `evidence` hardening.

---

## AE. UX / Screens

**Assessment Mode (§30).** A persistent banner: "ASSESSMENT MODE: the Mentor is paused for this concept. Allowed: your lessons, your own project and results, platform docs. Not allowed: AI assistants." A visible "What is being assessed" list, and a "Leave assessment" action that releases the lock. Everything is auditable through events. No monitoring of any kind.

| Screen | Purpose | Primary actions | Key information and evidence transparency | Assistance restrictions | Failure and empty states |
|---|---|---|---|---|---|
| **A. Assessment Center** | One place for all assessment work | Start, resume, view result | Sections: Ready now, In progress, Results, Needs work, Review due or changed. Each says why it is offered. | None (Learning Mode) | Empty: "Nothing ready yet. Finish a project or lesson to unlock an assessment." |
| **B. Ready for Assessment** | Show the exact readiness checks | Start, or see what is missing | Deterministic checklist of met and unmet items, each linked to its record. Shows the source-work assistance levels and whether a fresh challenge is required, and why. | Explains that a fresh challenge is independent | Unmet items link to the fix (e.g. finish milestone 3 with verified results) |
| **C. Instructions** | Informed consent to the rules | Begin | What is assessed, criteria in plain language, allowed resources, time box (server-side expiry), retake rules, what evidence will be written | Mentor lock explained | If a prior attempt is active, resume instead |
| **D. Challenge Workspace** | Do the fresh modification, reproduction, or debugging challenge | Run in the platform, save draft, submit | Issued challenge, runs and results from the learner's own agent, live checklist of deterministic criteria | Mentor locked. Banner always visible. | Budget exhausted: runs pause, drafts safe. Provider error: retry, no loss. |
| **E. Explain-Back** | Answer authored prompts in your own words | Pick record pointer, write, submit | Prompt, the real records to point at, and what will be judged | Mentor locked | Auto-saved drafts. Expiry warning. |
| **F. Result** | Honest outcome | Read, request review, start remediation | Four separated blocks (§V). Outcome and `demonstration_effect` in plain words. Model, provider and rubric version shown. | Coaching opens only after the result | PROVISIONAL: "AI was not confident enough. Nothing changed. Options: review, retry." UNABLE_TO_ASSESS: safe retry. |
| **G. Needs Work / Remediation** | Turn gaps into next steps | Do step, retry when ready | Gaps by criterion key, deterministic remediation list, cooldown timer | Learning Mode. Professor available for coaching. | "No dead end": always at least one next step |
| **H. Demonstration Record** | View and export what was demonstrated | Export Markdown or JSON | Evidence list with provenance, verified vs self-reported statement, validity status | n/a | If overturned or changed, status shown with the history intact |
| **I. Capstone Assessment** | Integrated project assessment | Submit README, run challenge, explain-back set | Frozen brief criteria, deterministic results, capstone checklist | Mentor locked for the challenge and explain-back parts only | Any MA9-gated part shows "Available after MA9" |
| **J. Request Human Review** | Challenge a judgment | Submit reason, consent, withdraw | What the reviewer will see, and that only this attempt is shared. Status of the review. | n/a | Cannot self-review. Shows if consent is missing. |

Language is beginner-friendly: "needs more work" (not "failed"), "not enough confidence to decide," and "still practicing" are acceptable phrasings.

---

## AF. Test Strategy

Proposed files: `tests/test_ail5c_*.py` plus frontend `*.test.mjs`. Full migration-chain tests use `tmp_path` databases only.

**Isolation and roles**

1. Mentor and Professor cannot call the grading service (import-graph test), and the grading write path requires a `GRADER` run.
2. Grader cannot mentor: no advice or coaching fields are accepted, and Grader agent has no tools.
3. Grader packet isolation: a snapshot test of the allowlist fields, and negative tests proving assistance level, Mentor content, profile, other attempts, and state are absent.
4. Assessment Mode blocks Mentor and Professor for the assessed concept or project only, and releases on expiry or abandon.

**Determinism and integrity**

5. Deterministic results cannot be overridden by the LLM (forced `NEEDS_WORK`).
6. A grader response referencing a deterministic key is rejected.
7. 5B candidate fields are not trusted: fake `platform_verified` and `passed` claims fail the deterministic stage.
8. Platform evidence belongs to the correct learner, including via project-membership confusion in the shared AIL project.
9. Cross-user isolation for attempts, results, reviews, records, export.
10. Manifest and hash tamper detection; 5B resubmit does not affect a frozen attempt.

**Independence and state**

11. H5-only work never demonstrates. H3 is partial. H4 is formative for demonstration.
12. Study Mode does not directly demonstrate.
13. A fresh low-assistance challenge after H3–H5 source work can qualify.
14. Self-reported execution never satisfies a deterministic leg. Demo data never counts.
15. AI-graded evidence alone never demonstrates.
16. Learner State honors supersession, the platform floor, multi-hop CHANGED, and v2 constraint keys. Include a regression test for the pre-change H3 `LAB` behavior.

**Grading behavior**

17. Grader failure changes no state and leaves deterministic results intact.
18. Low-confidence results and disagreements yield `PROVISIONAL` or `HUMAN_REVIEW_REQUIRED`, never silent DEMONSTRATED.
19. Quote validation rejects non-verbatim quotes. Extra, missing, and duplicate keys are rejected.
20. Cross-check runs use a different model.

**Idempotency and immutability**

21. Retry is idempotent. Concurrent submit and finalize converge. No duplicate `LearningEvidence`.
22. Historical results are immutable. Definitions are version-pinned. Later policy changes do not alter old results.

**Review and lifecycle**

23. Human override is audited, consent-bound, and cannot be self-reviewed. Supersession preserves history.
24. Remediation does not erase history, and retry gets a different draw.
25. Personal Lab: result, conclusion, and Grader judgment remain distinct rows.
26. REVIEW_DUE integration: new evidence resets the retention clock. CHANGED plus DEMONSTRATED coexist.

**Negative and scope guards**

27. No MA9 runtime: no new sandbox or tool imports, and MA9-gated definitions are hidden or gated.
28. No surveillance: a schema-allowlist test on the new tables and no telemetry routes.
29. No similarity or copy-detection code exists.
30. Migration tests: R1/R2/R3 full-chain on `tmp_path`, foreign-key check, downgrade one step.

---

## AG. UAT Scenarios

| # | Scenario | Expected |
|---|---|---|
| U1 | Learner finishes P2 at H1, starts a modification challenge | Fresh variant, Mentor locked, deterministic run checks pass, evidence written, state recomputed |
| U2 | Learner used H4 on a milestone, then takes the assessment | Source work formative. A fresh challenge is required and explained. |
| U3 | Learner used Study Mode (H5), completes the variant challenge at H0 | Qualifies. Report shows the H5 history visibly. |
| U4 | Explain-back with a strong answer | Grader `met` with verbatim quotes. Cross-check agrees. Evidence written but not sufficient alone to demonstrate. |
| U5 | Explain-back where the two grader runs disagree | `HUMAN_REVIEW_REQUIRED`. Nothing changes in state. |
| U6 | Provider outage during grading | Attempt `AWAITING_GRADING`. Deterministic results shown. Retry works with no duplicate evidence. |
| U7 | Learner requests human review of a `NEEDS_WORK` | Consent captured. Reviewer confirms or overrides with a rationale. Audit trail intact. |
| U8 | Learner claims `used_ai_assistant` in the attestation | Formative only. No qualification. Feedback still given. |
| U9 | Second learner on the same installation | Cannot see the first learner's attempts, results, records, or reviews. |
| U10 | Concept gets a material new version | DEMONSTRATED plus CHANGED. Reassessment offered. Old record still viewable and marked. |
| U11 | Capstone on the no-code path pre-MA9 | Fully assessable. MA9-gated parts show "Available after MA9". |
| U12 | Retention review due after a passing assessment | Clock was reset by the assessment evidence. 4B review flow unchanged. |
| U13 | Experiment interpretation | Platform result, learner conclusion, and Grader judgment displayed side by side and unedited. |
| U14 | Budget hits 100% before grading | Grading paused with a clear message. Lessons and deterministic results keep working. |

---

## AH. Explicit Exclusions

AIL.5C is not: another Academy; another learning-state engine (no mastery table, no score); another Evidence engine; another Experiment engine; another MA6; another workflow engine; a teacher LMS; classroom, cohort, or school administration; an external accreditation platform; exam proctoring (no webcam, screen, keystroke, clipboard, or focus monitoring); a plagiarism or copy-detection system (no token-overlap or similarity detector); MA9; or an autonomous coding runtime. Also excluded: leaderboards, rankings, percentiles, "intelligence" scores, cross-learner comparison, public share links, certificates, and any AIL.5D/5E/5F phase. Later technical checkpoints are implementation checkpoints, not roadmap phases.

---

## AI. Risks & Mitigations

| # | Risk | Mitigation |
|---|---|---|
| R1 | Enum CHECK rebuilds on real-data tables (staging incident history) | Separate R2/R3 revisions, `tmp_path` full-chain tests, staging-volume rehearsal, foreign-key check, fresh deploy authorization, fallback column option |
| R2 | Learner State correction regresses some existing DEMONSTRATED states | Dry-run diff report on a DB copy before merge. Communicate it as a correctness fix. |
| R3 | 5B claims trusted as facts | 5C ignores them. P0 hardening of the 5B route. |
| R4 | Shared AIL project exposes learner text via project RBAC (SRC-7) | No learner content in `tasks`. User-scoped storage. Verify artifact and run visibility filters by owner. Cross-user tests. |
| R5 | External AI use undetectable | Fresh seeded challenges, record-grounded pointers, transfer prompts, deterministic modification leg, declaration, AI-graded-alone rule. State the limit openly. |
| R6 | Grader inconsistency or bias | Blind packet, categorical confidence, quote validation, cross-check on deciding judgments, golden-set release gate, human review path |
| R7 | Pre-MA9 execution gap: no learner-app execution in 5B (`/test` stub) | Definitions declare capability. Fall back to alternative requirement sets. Self-reported execution reaches PRACTICED only. |
| R8 | Content volume (authored pools, rubrics, reference key points) | Ship a small authored set first (one per weekly project plus capstone). Publish validators enforce pool size and criterion structure. |
| R9 | Cost of cross-check | Only for deciding AI judgments and capstone. Bounded at 2 runs per round. |
| R10 | Beginners find Assessment Mode intimidating | Plain language, humane failure, no dead ends, always a next step |

---

## AJ. Implementation Recommendation

Work in implementation checkpoints, not new phases. Each is reviewable before the next starts.

- **CP0: Prerequisites (P0), no new features.**
  - P0-1: `LearnerStateService` corrections (§P) plus the dry-run diff report.
  - P0-2: 5B `submit` no-overwrite after `finalized_at`, and server-side derivation of `passed` and `execution_verification` in the `evidence` route.
  - P0-3: confirm artifact and run visibility is owner-filtered in the shared AIL project.
  - P0-4: migration rehearsal plan.
- **CP1: Schema R1, definitions, and the deterministic engine, with no AI.**
  - Knowledge check, modification, reproduction and debugging with deterministic legs.
  - Attempt lifecycle, manifest and hash, Assessment Mode guard, readiness.
  - The R2 evidence-type migration.
- **CP2: Grader.**
  - R3 role migration, Grader agent and prompt, `grading_contract_v1`, packet assembler, dispatch branch, cross-check, failure handling, golden-set gate.
- **CP3: Finalization.**
  - Outcome aggregation, evidence writer and idempotency, report, record snapshot and export, Learner State integration, Professor `assessment_result` target.
- **CP4: Human review, remediation and capstone.**
- **CP5: UI screens (vanilla JS under `frontend/assets/js/pages`) and UAT.**

Author a minimal seed set of definitions for the AI Foundations program: one per weekly project plus the capstone.

**Owner decisions needed before CP1:**

- **D-1:** `GRADER` run role (recommended) or reuse `EVALUATOR`.
- **D-2:** Grader model policy: manual pin (recommended) or MA8 `auto`.
- **D-3:** Extend the enums (recommended) or use the fallback column.
- **D-4:** Whether capstone requires human confirmation (default: no).
- **D-5:** Reviewer roster: org Owner or Admin.
- **D-6:** Default fresh-required policy, retake cooldown, and attempt expiry.
- **D-7:** Whether a `record_observation` kind is added to 5C or authored requirement sets avoid `observation`.

---

## AK. TOP 10 AIL.5C DECISIONS TO FREEZE

**1. DECISION:** The Grader is a separate registered agent (`Academy Grader`, run role `GRADER`). The Professor and Mentor code paths cannot invoke grading. The Professor coaches after a result and can never rewrite it, and the Grader never coaches.
**WHY:** The helper must never grade the same work.
**IMPACT:** Fixes the agent identity, the run role, the migration R3, and the dependency direction that tests enforce.

**2. DECISION:** Grading input is a blind, allowlisted packet rebuilt at run time from frozen attempt data. The bookkeeping task holds pointers only, and no learner content is written to `tasks`. The Grader never sees assistance level, Mentor content, profile, history, or stakes.
**WHY:** It avoids bias and leakage, and it stops the shared-project exposure (SRC-7).
**IMPACT:** Fixes the packet schema, the `_build_extra_context` branch, and the isolation tests.

**3. DECISION:** Deterministic first and non-overridable. The Grader judges only `method=grader` criteria, and code assembles the outcome. MA6 results are consumed as read-only facts, and MA6 tables are never reused for learner grading.
**WHY:** The model must not re-decide facts the platform can compute.
**IMPACT:** Fixes the check catalog, the aggregation rules, and the MA6 boundary.

**4. DECISION:** No new mastery system. 5C only appends `LearningEvidence` (five new evidence types plus `assessment_result` ref), idempotent on `(user, result, concept)`. Only `LearnerStateService` computes state. Failures write no evidence.
**WHY:** One ladder and one evidence store keep state honest and recomputable.
**IMPACT:** Fixes the evidence mapping, the migration R2, and the writer's idempotency.

**5. DECISION:** The requirement model lives on the immutable `ConceptVersion.evidence_requirements` (v2 constraints), with a platform floor: superseded and demo rows excluded, and H5, >H2, or self-reported rows never satisfy a DEMONSTRATED leg. AI-graded evidence alone never demonstrates.
**WHY:** DEMONSTRATED must be defensible, data-driven, and reproducible.
**IMPACT:** Fixes the Learner State corrections (P0-1) and the publish-time validators.

**6. DECISION:** The independence policy is H0–H2 full, H3 partial, H4 formative for demonstration, H5 formative. Fresh challenges are drawn from authored variant pools by a deterministic seed. They are required when source work is assisted. AI-generated challenges never count toward DEMONSTRATED.
**WHY:** Independence is shown through evidence design, not surveillance.
**IMPACT:** Fixes the policy table, the challenge spec, and the fresh-required rule.

**7. DECISION:** No proctoring, no copy detection, and no similarity code. Assessment Mode is a Mentor lock plus a self-report attestation, with server-side time boxes only.
**WHY:** It honors the frozen exclusions and keeps trust with the learner.
**IMPACT:** Fixes the guard, the attestation field, and the schema-allowlist and no-surveillance tests.

**8. DECISION:** Versioned and immutable. Definitions are row-per-version. Attempts pin the definition, concept versions, Grader version and contract. Results are append-only, with supersession only through new rows and the sanctioned evidence supersede pointer.
**WHY:** Assessments must be reproducible, and history must never change.
**IMPACT:** Fixes the schema constraints and version pinning.

**9. DECISION:** Confidence is categorical. Low confidence or disagreement yields `PROVISIONAL` or `HUMAN_REVIEW_REQUIRED`, with no evidence written. Deciding AI judgments get a second-model cross-check. AI never silently establishes DEMONSTRATED.
**WHY:** It protects the learner from a fallible judge and the record from noise.
**IMPACT:** Fixes the thresholds, the two-run cap, and the outcome vocabulary.

**10. DECISION:** Human review is a purpose-built, consent-bound `assessment_reviews` table using the Approval CAS and fingerprint pattern, not the Approval engine. Prerequisite corrections (Learner State fixes, 5B trust hardening, isolated and rehearsed enum migrations) land before any 5C feature.
**WHY:** Approvals are project-RBAC-scoped and unsafe for learner data, and correcting existing gaps first prevents building on false verification.
**IMPACT:** Fixes the review table, the reviewer roles, and the CP0-first ordering.

I can also publish this as a private page for the architecture review if that would help.

AIL.5C DESIGN COMPLETE — READY FOR ARCHITECTURE REVIEW