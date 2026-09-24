# AIL.4C — AI Professor Architecture and Implementation Contract

**Status:** FROZEN FOR IMPLEMENTATION PLANNING — documentation only
**Baseline worktree:** `.worktrees/ail4b-review-retention`
**Baseline branch:** `feat/ail4b-review-retention`
**Baseline SHA:** `bad3daf7421e245c7652240c081317c9d770c2ce`
**Dependencies:** AIL.3C, AIL.4A, and AIL.4B are staging-pass/frozen
**Initial interaction-content retention:** 90 days, configurable

## 1. Purpose

AIL.4C adds one evidence-grounded AI Professor. It answers:

> Based on what I have learned, tested, reviewed, and what is changing in AI, what should I understand or do next?

The Professor is explanatory and advisory. It is not a generic chatbot and does not become a second learning engine.

## 2. Professor identity and execution

The Professor is a registered, versioned Agent:

- Project: `system_ail`
- Role: `professor`
- Configuration: Agent Version, Prompt Version, context policy, model policy, and budget policy
- Tool access: none for AIL.4C V1

The Professor reuses the existing execution path:

```text
TaskRun
  → AgentRun
    → AgentRunAttempt
      → ModelCall
        → Artifact
          → Flight Recorder
```

It also reuses the Agent Registry, Model Registry, MA8/model policy, Provider Model Snapshots, Budget Reservations, Usage Events, and existing provider adapters.

No separate Professor runtime or execution architecture is permitted.

## 3. Learning authority

The Professor must not directly:

- create Learning Evidence;
- set Learner State;
- mark a Concept UNDERSTOOD, PRACTICED, or DEMONSTRATED;
- modify Learning Plans;
- modify Radar or Claims;
- modify Experiments or Experiment Conclusions;
- modify review outcomes; or
- automatically count any interaction toward learning.

`LearnerStateService` remains authoritative. Existing evidence qualification and review contracts remain authoritative.

Recommendations are advisory and require an explicit user action through the existing domain APIs.

## 4. Deterministic-first flow

Every interaction follows this logical flow:

```text
deterministic context assembly
  → authorization and privacy filtering
    → bounded Professor generation
      → structured response validation
        → Artifact and provenance persistence
          → user
```

The platform determines the intent, eligible records, learner-state facts, evidence references, reason codes, conflicts, and available actions before generation. The model explains those selected facts; it does not decide which private records it may retrieve.

## 5. Authorized context

Professor may automatically read authorized AIL context, including:

- Concepts and Concept Versions;
- Learning Items;
- the learner’s Learning Plan, Learner State, Learning Evidence, and review history;
- Radar Developments and Claims;
- Today signals;
- the learner’s Personal Lab experiments and conclusions; and
- Model Registry data and explicitly opted-in platform evidence.

Professor must not automatically bulk-read project source code, arbitrary artifacts, arbitrary TaskRuns, prompts, private model outputs, or unrelated project data.

Specific private content requires explicit user selection or attachment for the current interaction. Interests are declared by the user and are never inferred through surveillance.

## 6. Provenance and conflicts

The response must preserve the distinction between:

- external/public knowledge;
- `PLATFORM_OBSERVATION`;
- user-authored conclusions; and
- `AI_EXPLANATION`.

The existing Claim Types remain authoritative:

`FACT`, `PROVIDER_CLAIM`, `RESEARCH_RESULT`, `BENCHMARK_RESULT`, `COMMUNITY_SIGNAL`, `PLATFORM_OBSERVATION`, and `AI_EXPLANATION`.

Conflicting evidence is shown side-by-side and is never silently reconciled. Professor interpretation never overwrites a user-authored Experiment Conclusion.

## 7. Persistence

AIL.4C V1 creates no Professor-specific conversation or message tables. A structured Professor response is persisted as an existing Artifact associated with the normal execution records. Continuation requires an explicit previous interaction or run reference.

Professor transcripts are not mined for learner interests or learning state. Initial interaction content is retained for 90 days, with retention configurable and separate from learning semantics.

No new learning-state, evidence, conversation, or budget tables are expected.

Seed/configuration/provisioning work may establish:

- the `system_ail` project;
- the AI Professor Agent;
- its Prompt Version and Agent Version; and
- context, model, privacy, and budget policies.

## 8. Model and cost policy

Professor is not hard-coded to a provider-specific model. It uses the Model Registry and MA8/model policy. An initial staging model is selected through configuration and may later be replaced without redesigning Professor.

Cost control reuses existing infrastructure:

- deterministic context before a model call;
- bounded input and output tokens;
- explicit generation only;
- Budget Reservation before execution;
- ModelCall token/cost recording;
- Usage Event recording; and
- graceful behavior when the budget or provider is unavailable.

No final commercial monthly dollar limit is frozen here. Conservative staging limits are configurable while actual Professor usage is collected.

## 9. AIL.4C V1 experiences

All six experiences are frozen as entry points into one Professor engine:

1. Ask Professor
2. Explain This
3. What Should I Learn Next?
4. Help Me Understand My Experiment
5. Help Me Review
6. Why Does This Matter?

Experiment explanations must preserve the user’s conclusion. Review explanations must not pass, fail, or change review state.

## 10. Response contract

The response is structured and validated before delivery. It must contain, at minimum:

- a direct answer;
- the resolved intent;
- provenance references with record type and Claim Type where applicable;
- uncertainties and as-of information;
- an optional advisory recommended next action; and
- execution provenance identifying Agent Version, Agent Run, Model Snapshot, and cost-bearing records.

The validator rejects unsupported platform references, uncited factual claims, malformed responses, and responses that imply state mutation or learning evidence creation.

## 11. AIL.5 boundary

AIL.4C does not include:

- a generated 30-day curriculum;
- Build With Me;
- Project Mentor;
- Grader or Assessor behavior;
- the H0–H5 hint ladder;
- capstones;
- Teacher/Creator tooling; or
- LMS functionality.

The Professor contract remains forward-compatible with AIL.5 without implementing those capabilities.

## 12. Implementation slices

### AIL.4C.1 — Professor Contract + Deterministic Context

- fixed intents;
- context assembler;
- authorization and privacy filtering;
- provenance contract;
- structured response contract and validator; and
- deterministic next-action facts.

### AIL.4C.2 — Professor Agent Execution

- provision AI Professor;
- create Agent and Prompt Versions;
- integrate with TaskRun/AgentRun;
- use MA8/model resolution;
- persist validated responses as Artifacts;
- record tokens and costs; and
- implement degraded-provider behavior.

### AIL.4C.3 — Professor Product Surfaces + Integration

- implement the six entry experiences;
- provide provenance/evidence inspection;
- support explicit private attachments;
- support explicit continuation;
- add integration tests; and
- complete staging UAT.

These are three meaningful slices, not a collection of micro-slices.

## 13. Explicit implementation constraints

Until implementation is separately authorized:

- do not modify AIL.4B;
- do not create migrations;
- do not create an implementation branch or worktree;
- do not deploy, merge, or push; and
- do not touch production.
