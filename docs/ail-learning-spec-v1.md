# AIL — AI Intelligence & Learning Lab

**Unified Product & Architecture Specification — Version 1.0 (for final architecture review)**

| | |
|---|---|
| Status | DRAFT FOR ARCHITECTURE REVIEW. Not approved for implementation. |
| Owner | Serge Tchuenteu |
| Date | 2026-09-22 |
| Supersedes | `claude/ail-ai-radar-design-v1.md` (Radar extension). Its approved concepts are carried over; its data model and phasing are replaced by this document. |
| Depends on | `claude/multi-agent-platform-spec-v1.1.md` (platform spec, "MA spec"). All MA section numbers below refer to that document. |
| Explicit non-scope | No code, migrations, commits, deployments or source integrations. |

> **Note on "original AIL Sections 1–39."** Those sections are not stored in this project, and the owner's review called this out as the gap to close. This document does not guess at their wording. It **defines** the learning core (Concept Graph, Learner State, AI Professor, Curriculum, Knowledge Checks, Labs) from the requirements in the owner's review brief. It then integrates the approved Radar design against those definitions. If an earlier AIL draft exists elsewhere and conflicts with this one, this document should be treated as authoritative once approved, and the conflict listed in §40.

---

## 0. Reading Guide, Terminology and Change Log

### 0.1 One sentence

**AIL is the part of the platform that makes its operator more capable over time.** It shows what is changing in AI and how well each claim is supported, teaches the concepts needed to understand it, lets the operator test it on their own work, and keeps an honest record of what they actually know and have practiced.

### 0.2 Resolved terminology

The Radar design and the learning design used several overlapping words. This table fixes one meaning for each, and the rest of the document uses these terms consistently.

| Term | Meaning in AIL | Replaces / disambiguates |
|---|---|---|
| **Development** | A deduplicated unit of external change (a release, price change, paper, protocol update) | "news item", "article" |
| **Source Item** | One raw ingested record from a registered Source | — |
| **Claim** | One atomic, attributable statement with exactly one of the seven **Claim Types** | "fact", "finding" used loosely |
| **Claim Type** | FACT, PROVIDER_CLAIM, RESEARCH_RESULT, BENCHMARK_RESULT, COMMUNITY_SIGNAL, PLATFORM_OBSERVATION, AI_EXPLANATION | — |
| **Verification Level** | The rule-derived ladder for a Development or Model: Claimed → Documented → Available → Independently Measured → Tested by Us | Radar v1's "status ladder" (renamed so it can't be confused with Learner State) |
| **Attention Level** | Low / Rising / High / Sustained. A separate axis, never evidence | "trending", "hot" |
| **Triage Decision** | The operator's call on a Development, Model or Opportunity: IGNORE, WATCH, LEARN, EXPERIMENT, INVESTIGATE | Radar v1 "OperatorDecision"; distinct from MA **Approvals** and MA8 **routing decisions** |
| **Concept** | A node in the Concept Graph: one teachable idea with versioned content | "topic", "glossary entry" |
| **Track** | A named set of goal Concepts (e.g., "Agentic AI"). A *view over* the graph, not a fixed sequence | "course" |
| **Learning Plan** | The user's ordered, editable list of Concepts to work on, computed from goals and the graph | "curriculum" when it means *my* plan |
| **Curriculum** | The whole teachable body: Concept Graph + Tracks + Learning Items | — |
| **Learning Item** | A reusable teaching unit attached to a Concept: resource, exercise, knowledge-check question, scenario, observation task, or lab | — |
| **Learning Evidence** | An append-only record that the learner did something gradable for a Concept | Distinct from a **Claim**'s supporting record |
| **Learner State** | The evidence-derived state of one Concept for one learner (§18) | "learned", "progress %" |
| **Lab** | A hands-on Learning Item. A lab may *create* an Experiment | — |
| **Experiment** | An AIL-owned grouping with a hypothesis, which executes platform Task Runs / Comparison Runs and records results | Distinct from MA's **Comparison Run**, which it uses |
| **Personal Eval Set** | A versioned set of representative Tasks for one role, used to compare models on *your* work | Radar v1 "personal_eval_tasks" (now reuses `tasks`) |
| **Platform Evidence** | Records from the operator's own platform use (evaluations, routing decisions, usage, runs), read by AIL only from projects opted in (§30) | — |
| **Brief** | A stored, reproducible snapshot of the "what should I know" view for a period | — |

### 0.3 Changes relative to the Radar extension (v1)

| Radar v1 | This spec | Why |
|---|---|---|
| "Status ladder" | **Verification Level** | Avoids a clash with Learner State |
| `radar_concept_links`, `radar_platform_links`, `capability_tags` JSON | `development_concepts`, `development_models`, `development_terms` over a single `taxonomy_terms` vocabulary | Owner rule from MA spec §24.4: no relationships stored as JSON arrays |
| `status_cached`, `attention_cached` columns | Removed; derived on read | Small data volumes; caches create staleness bugs |
| `radar_development_items` join table | `radar_items.development_id` FK | An item belongs to exactly one Development |
| `radar_brief_snapshots.item_ids` JSON | `brief_entries` rows, each carrying its visible reason | Every recommendation must show its reason |
| `personal_eval_tasks` | Reuses MA `tasks` via `eval_set_version_tasks` | Tasks are already versionable templates with runs |
| Radar LLM calls "go through Model Call path" (vague) | AIL's AI roles are **registered Agents** in a system project (§29) | Full reuse of versioning, Flight Recorder, cost and budgets |
| Opportunity Radar in V1.1 | Moved to AIL.4 | Needs Radar and Experiment history to be useful |
| Assumed Concept Graph / Learner State / Professor | Fully defined (§15–§22) | Closes the review gap |

---

## 1. Product Vision

### 1.1 The core question

> **How does this platform help me become significantly more capable in AI over time?**

Being "capable in AI" here means five things you can actually check:

1. **Understanding.** You can explain the concepts behind a development and pass a check on them.
2. **Judgment.** You can tell a provider claim from independent evidence from your own evidence, and your triage decisions hold up in retrospect.
3. **Hands-on skill.** You have configured, run and interpreted real experiments, and they're recorded.
4. **Current model knowledge.** You know which models exist, what they cost and how they behave *on your work*.
5. **Platform mastery.** You understand why your own platform behaves as it does: routing, evaluation, cost and approvals.

A news reader can reach (1) partially and (4) superficially. AIL is designed for all five, and each has a way to measure it (§21, §23, §40 acceptance criteria).

### 1.2 The six pillars and how they combine

| Pillar | What it contributes | Main surfaces |
|---|---|---|
| **AI Ecosystem Intelligence** | What changed, with typed claims and a Verification Level | Radar, What's New, Brief |
| **Model Intelligence** | What we know about each model, from facts through to our own evidence | Model Explorer, Model Comparison |
| **Hands-on Experimentation** | Controlled tests on your own tasks | Experiment Lab, Personal Eval Sets |
| **Personal Learning** | Concept Graph, plan, checks, labs, evidence-based state | Learn, My AI Knowledge |
| **AI Professor / Coach** | Teaching, explanation, diagnosis, exercises, all grounded in the above | Professor, available on every page |
| **My Platform Evidence** | Your routing decisions, evaluations, costs and runs as teaching material and evidence | Everywhere, read-only, opt-in |

The loop that ties them together:

```
DISCOVER ──> UNDERSTAND ──> CONNECT ──> EXPERIMENT ──> EVALUATE ──> LEARN ──> APPLY
 Radar /      Claims +       Concepts,    Experiment    MA Evaluation  Professor,   Platform config
 What's New   Verification   Models,      Lab on        + objective    checks,      (done in MA, not AIL);
              Level          Platform     Personal      metrics        Learner      AIL records the outcome
                                          Eval Set                     Evidence
```

### 1.3 What AIL is not

- Not a news feed. There's no infinite scroll, no engagement ranking, and no unlimited item count.
- Not a model leaderboard. There's no universal model score, and every comparison states its slice and sample size.
- Not a course catalog. There's no single fixed curriculum; plans are computed from the graph and your goals.
- Not an autonomous roadmap or router tuner. AIL recommends and records; the human decides, and platform changes happen in the MA surfaces.
- Not a chatbot with a syllabus. The Professor is constrained by data contracts, provenance rules and an evidence policy (§16).

---

## 2. Product Principles

| # | Principle | Consequence in the design |
|---|---|---|
| P1 | **Evidence over engagement** | Learner State comes only from gradable evidence (§18). Opening a page is exposure, not knowledge. |
| P2 | **Every statement is attributable** | Every externally derived statement is a Claim with one of seven types and a source (§26). |
| P3 | **Attention is not evidence** | Attention can never raise a Verification Level, create an Opportunity or justify a recommendation on its own (§10). |
| P4 | **No unexplained scores** | No importance score and no model score. Rankings are sorts on visible fields, and each shown item carries its reason (§6, §23). |
| P5 | **Deterministic first, LLM second** | Detection, diffs, queries, state, ordering and most search are deterministic. The LLM explains, teaches, grades free text (labeled), and drafts. |
| P6 | **Human control over plan, triage and platform** | AIL proposes changes to the curriculum, plan, experiments and opportunities. It never applies them silently. |
| P7 | **Your evidence is local and slice-bound** | Personal Eval results always carry their slice, n, version, date and conditions, and are never presented as general capability (§13). |
| P8 | **Reuse the platform** | Experiments are Comparison Runs, grading is MA Evaluation, AI roles are Agents, spend goes through the budget system, and history is in the Flight Recorder. AIL adds only what's genuinely new. |
| P9 | **Honest freshness and coverage** | Every fact has an as-of date. Stale sources are flagged. Blind spots (unwatched lanes and sources) are visible (§27). |
| P10 | **Scoped, minimal context** | The Professor reads only data in its context contract, never unrelated content to infer interests (§16.5, §30). |
| P11 | **Small, useful slices** | Four implementation slices, each delivering value on its own (§38). |

---

## 3. Relationship to MA8 (Intelligent Model Router)

### 3.1 Distinct responsibilities

| Question | Owner | Surface |
|---|---|---|
| "Why was this model selected for this Agent Run?" | **MA8** | Router View (MA §27.7), `model_routing_decisions` |
| "What would Auto pick for this requirement profile?" | **MA8** | Router dry-run simulation endpoint |
| "What do we know about this model?" (facts, claims, benchmarks, attention, our evidence) | **AIL** | Model Explorer (§7) |
| "Is this model better than my current one on my work?" | **AIL** (runs MA5/MA6 machinery) | Experiment Lab (§14), Personal Eval Sets (§13) |
| "Help me understand routing" | **AIL** | Concept "Model Routing", Professor, observation labs over MA8 records |

### 3.2 Data flow rules (hard boundaries)

1. **AIL reads MA8 data; it never writes router state.** AIL reads `model_routing_decisions`, `router_policy_versions` and calls the dry-run endpoint. It never creates, activates or edits a router policy version.
2. **The Router never reads AIL claims.** PROVIDER_CLAIM, BENCHMARK_RESULT, COMMUNITY_SIGNAL and AI_EXPLANATION data **must not** feed routing scores. The Router's inputs stay as the MA spec defines them: registry facts, declared capabilities and our own Evaluation / Model Call history. This keeps marketing and hype out of automated decisions by construction.
3. **Personal Eval Set runs are ordinary platform evaluations.** They produce standard `evaluations` rows on standard `agent_runs`. Whether MA8 includes experiment-tagged runs in its historical scoring is an **owner decision (§41, D7)**. The default proposed here is *excluded*, because eval sets are deliberately small and skewed.
4. **Configuration labs use dry-run only.** A "configure a routing policy" lab evaluates a *draft, unsaved* scoring config against the dry-run endpoint. **Required from MA8:** the dry-run endpoint must accept an inline, non-persisted scoring config (flagged in §40).

### 3.3 Dependency timing

| AIL capability | Needs | Before MA8 exists |
|---|---|---|
| Observation labs on routing decisions | MA3 routing decisions (static scorer) | Works, with decisions from the MA3 scorer |
| "Compare AUTO policies" lab | MA8 dry-run with inline config | Unavailable; the concept can still reach DEMONSTRATED via an alternative requirement (§18.4) |
| Model Explorer "selected by the router in N runs" | `model_routing_decisions` | Works from MA3 onward |

---

## 4. Relationship to MA9 (Tool Runtime & Sandboxing)

AIL **does not pull MA9 forward** and does not build any tool runtime, MCP adapter, egress control or sandbox upgrade. AIL.1 through AIL.4 run entirely on MA2–MA8 capabilities.

### 4.1 AIL features that wait for MA9

| Feature | Why it waits |
|---|---|
| MCP labs (connect, permission and run an MCP server's tools) | Installing and running third-party MCP servers is code execution; needs MA9's MCP registration, permission model and hardened sandbox |
| Tool-calling labs with new or third-party tools | Tool Bus beyond MA3's repo and terminal tools is MA9 |
| Computer-use / browser-agent experiments | Needs stronger isolation and egress allow-listing (MA §20.8, §21.3) |
| Experiments evaluating "new capability X for Tool Runtime" end to end | The target runtime doesn't exist yet |
| Database-tool labs | MA9 DB read tools |

### 4.2 What AIL *can* do for MA9 topics before MA9

- Teach Tool Calling, MCP, Agent Permissions and Sandboxing (concept content, checks, scenarios).
- Run **observation labs over existing MA3 Tool Bus records**, for example: "find a denied tool call in your Flight Recorder and explain which permission rule denied it."
- Track MA9-relevant Developments (MCP spec changes, MCP registry growth, computer-use releases) and link them to the `platform_component: tool_runtime` term, so "Which releases relate to MA9?" works from AIL.2 onward.
- Record Triage Decisions of type INVESTIGATE against MA9 topics, which feed MA9 planning as *input*, never as roadmap items.

### 4.3 Graceful gating

Every Learning Item and Experiment template declares `requires_platform_capability` (e.g., `ma9.mcp_runtime`). Unavailable items are shown as "Available after MA9 (Tool Runtime)", never hidden. Concepts whose best evidence needs an MA9 lab carry an **alternative requirement set**, so the learner can still reach DEMONSTRATED; the concept page then notes "hands-on lab not yet available" (§18.4).

---

## 5. Information Architecture and Navigation

### 5.1 Top-level navigation (inside the platform's existing nav shell, MA §27.1)

```
AIL
├── Today            (AIL Home: "What should I know today?")          §6
├── What's New       (deterministic change log: models, prices, sources)   §8
├── Radar            (Developments, evidence, triage)                  §9–§11
├── Models           (Model Explorer · Compare)                        §7, §12
├── Lab              (Experiments · Personal Eval Sets)                §13–§14
├── Learn            (My Plan · Concepts · Tracks · Professor)         §15–§20
├── My AI Knowledge  (what I know, what I've practiced, why)          §21
├── Watchlist        (everything I'm WATCHing, with triggers)          §24
├── Briefs           (archive of Brief snapshots)                      §23
└── Settings         (Interests & goals · Sources · Evidence opt-in · Budget · Privacy)
Global: Ask AIL bar (§25) · Professor side panel (§16), available on every page with that page as explicit context
```

### 5.2 Cross-links (the product works by navigation, not by duplication)

| From | To | Link |
|---|---|---|
| Development | Concepts, Models, Platform components, Experiments, Triage | "Understand this", "Models involved", "Relates to MA9", "Test it", "Decide" |
| Model | Developments, Claims, Experiments, Routing decisions (MA8), Concepts | "News", "Evidence", "Our tests", "Router history", "Learn" |
| Concept | Learning Items, Developments (recent), Platform examples, Labs, Evidence | "What's new here", "See it in my platform", "Practice" |
| Experiment | Eval Set version, Models, Comparison Runs, Evaluations, Concepts, Development | Full provenance chain |
| Evidence item | Source record (check result, lab run, evaluation, routing decision) | "Why does AIL believe this?" |

---

## 6. AIL Home: "What should I know today?"

### 6.1 Purpose

Home answers "what should I know today?" for *this* operator. It combines external change, platform evidence and learning state into a short, bounded page. Home is the **live** version of the Brief (§23), and it uses the same generator and the same rules.

### 6.2 Layout (sections, caps and data sources)

| Section | Cap | Data | LLM? |
|---|---|---|---|
| **Header** | — | Sources last refreshed, items considered vs. shown, stale-source warnings, blind-spot line | No |
| **Important changes** | 3 | Developments (§9), ordered by the §6.3 sort | 1 sentence each, labeled AI_EXPLANATION, generated once per Development and cached as a Claim |
| **New models** | 5 | `provider_model_snapshots` where `change_kind = new`, filtered to watched lanes | No |
| **Model / provider changes** | 5 | Snapshots: context, capability, status or deprecation changes | No |
| **Pricing changes** | 5 | Snapshots with `change_kind = price`, showing old → new and % | No |
| **Capability changes** | 3 | Developments tagged with a capability term | 1 sentence |
| **Attention signals** | 3 | Attention series crossing thresholds, **always shown beside Verification Level** | No |
| **Relevant to my platform** | 3 | Developments linked to platform components *you use* (from usage) + open Opportunities (AIL.4) | Cached hypothesis text |
| **Relevant to my learning** | 3 | Developments linked to Concepts in your plan that aren't yet DEMONSTRATED | No |
| **Learn next** | 1 | Planner rule (§17.5) | 2–3 sentences, cited |
| **Experiments you may want to try** | 2 | Templates matching Developments at Verification Level ≥ Available, in lanes you use, with estimated cost | No |
| **Review due** | 1 | Retention rule (§22) | No |
| **My recent progress** | — | Evidence added in the last 7 days, state transitions, experiments completed, spend | No |
| **Pending decisions** | — | Untriaged items, WATCH triggers that fired | No |

**Empty-section rule:** a section with nothing qualifying says so in one line. It is never backfilled with weaker items.

### 6.3 Ordering without a score

Important changes use a **lexicographic sort on visible fields**, and the rule is shown on hover:

1. Linked to a platform component you actually use (from usage in the last 30 days in opted-in projects)
2. Verification Level, higher first
3. Linked to a Concept in your active plan
4. Linked to a watched model, provider or lane
5. `announced_at`, newest first

### 6.4 Visible reasons (mandatory)

Every item on Home carries a **reason line** built from template fragments, not free LLM text. Example:

> *Why this is here: you run the Code Reviewer role on reasoning models · Verification: Documented + Available · linked to "Tool Calling" (in your plan, not yet demonstrated).*

The same reason is stored in `brief_entries.reason_text` when a Brief is snapshotted.

---

## 7. Model Explorer

### 7.1 Purpose and boundary

The Model Explorer answers **"What do we know about this model?"** in layers, each labeled by claim type. It doesn't rank models or recommend one for routing (that's MA8). It gets registry data from MA2 and adds claims, history, attention and your own evidence.

### 7.2 Model profile layout

| Panel | Content | Claim type(s) | Source |
|---|---|---|---|
| **Identity** | Canonical ID, family, provider(s), status, first seen | FACT | `models`, `providers`, `provider_models` |
| **Availability** | Reachable providers, health, "in our registry: yes/no", last refreshed | FACT | `provider_models`, `providers.health_status` |
| **Pricing** | Input/output price per provider, **price history chart** | FACT (as of) | `provider_model_snapshots` |
| **Context & capabilities** | Context window, modalities, tool calling, structured output; declared capability tiers | FACT (for catalog fields) / PROVIDER_CLAIM (for declared tiers) | `models`, `model_capabilities` |
| **Release history** | Snapshot change timeline (new, price, context, status, deprecation) | FACT | `provider_model_snapshots` |
| **Provider claims** | What the provider says (model card, launch post), quoted | PROVIDER_CLAIM | `claims` where `model_id` is set |
| **External benchmarks** | Evaluator, benchmark + version, score, date, methodology link. Provider-published benchmarks are listed under Provider claims, not here | BENCHMARK_RESULT | `claims` |
| **Research** | Papers about the model or its technique | RESEARCH_RESULT | `claims` |
| **Attention / adoption** | Attention Level + series; adoption metric *only* where a real one exists | COMMUNITY_SIGNAL | `attention_samples` |
| **Our usage** | Calls, tokens, cost and roles using it in the last 30/90 days (opted-in projects) | PLATFORM_OBSERVATION | `model_calls`, `agent_runs`, `usage_events` |
| **Our reliability** | Success, timeout and malformed-output rates over a trailing window | PLATFORM_OBSERVATION | MA §13.4 reliability computation |
| **Our evaluation evidence** | Objective outcomes by (role, task type), with n and date range; "insufficient data" below threshold | PLATFORM_OBSERVATION | `evaluations` (MA §17.4 rules apply) |
| **Our experiments** | Experiments that included this model: slice, n, results, conclusion | PLATFORM_OBSERVATION | `experiments`, linked runs |
| **Router history** (link-out) | "Selected by the router in N runs · open Router View" | — | MA8 surface |
| **Related concepts** | e.g., Reasoning Models, Quantization (for local variants) | — | `development_concepts` via linked Developments + model capability → concept mapping |
| **Related developments** | Radar items linked to this model | — | `development_models` |
| **Triage** | Current decision (WATCH etc.), revisit trigger | — | `triage_decisions` |

### 7.3 Header summary (deterministic)

> **Verification:** Available · Independently measured (2 evaluators) · Tested by us (Code Reviewer eval set v2, n=6, 2026-09-14)
> **Attention:** Rising
> **Your usage:** 142 calls / 30 days, Reviewer role

There is no star rating, no overall score, and no "best for" badge.

### 7.4 Model Explorer vs. Model Registry UI (MA §27.6)

The MA registry UI stays as the operational CRUD view. The Model Explorer is a read-only **knowledge** view over the same rows plus AIL data. To avoid two diverging model pages, the MA registry UI links "Explore in AIL" and does not grow its own claims or benchmark panels.

---

## 8. What's New

### 8.1 Purpose

What's New is the **deterministic change log**: everything the registry and sources detected, in time order and filterable. It's the "raw but structured" layer beneath the Radar. You can scan it in seconds, and it needs no LLM.

### 8.2 Contents

| Event kind | Source | Example row |
|---|---|---|
| New model | registry snapshot | "`provider/model-x` appeared · 1M ctx · $1.10 / $4.40 per M · via OpenRouter · 2026-09-21" |
| Price change | registry snapshot | "`model-y` input $3.00 → $1.20 (−60%)" |
| Context / capability change | registry snapshot | "`model-z` context 128K → 400K; tool calling: added" |
| Status change / deprecation | registry snapshot | "`model-w` marked unavailable" |
| New source item | `radar_items` | "Provider changelog: 'Structured outputs GA' (unprocessed / attached to Development #212)" |

Filters: lane, provider, event kind, watched only, date range.

### 8.3 Relationship to the Radar

- Registry events for a model that already has a Development are **attached** to it.
- Otherwise, a registry event creates a Development automatically **only if** the model is in a watched lane or watched provider. All other registry events stay in What's New only. This keeps the Radar curated without losing completeness.
- Source items from S2/S3/S4 sources go through extraction (§9.3) and attach to or create Developments. Items that can't be processed stay visible here as "unprocessed".

---

## 9. AI Radar

### 9.1 Carried over from the approved Radar extension

Carried over unchanged: typed claims, source classes capping claim types, curated sources, independence groups, Developments as deduplicated units, claim-grounded explanations, explicit blind spots, the 8-lane taxonomy, and the five-way triage.

### 9.2 Development record

| Field | Meaning |
|---|---|
| `title` | Short neutral title (LLM-drafted from claims, editable) |
| `development_type` | `model_release`, `pricing`, `capability`, `protocol` (e.g., MCP), `research`, `benchmark`, `tooling`, `policy` |
| `lane_term_id` | One primary lane (§28.3) |
| `announced_at` | When the origin published it |
| `first_seen_at` | When AIL discovered it |
| `merged_into_id` | Set when the operator merges duplicates (manual in AIL.2) |
| Links | `development_concepts`, `development_models`, `development_terms` (topics, capabilities, platform components) |
| Claims | `claims.development_id` |
| Derived on read | Verification Level, Attention Level, Evidence Profile, relevance reasons |

### 9.3 Processing pipeline

```
Source fetch (allowlisted, per-source cadence)
   → radar_items (content_hash dedup)
   → [S1 registry sources] deterministic diff → FACT claims, no LLM
   → [S2/S3/S4 prose sources] Extractor Agent (no tools, schema-constrained output):
         proposes: development match-or-new, atomic claims with quote spans,
                   concept links, model links, capability/topic terms
   → Deterministic validators:
         (a) claim_type ≤ source class ceiling (§26.2)
         (b) quote_span is an exact substring of the stored item text
         (c) concept/term/model IDs exist (no invented IDs)
         (d) provider-authored benchmark numbers are forced to PROVIDER_CLAIM
   → Failures: the claim is dropped and the item flagged; nothing unvalidated is shown as a claim
   → Concept links stored as `proposed`; the user confirms (§15.6)
```

### 9.4 The twelve questions, answered per Development

Unchanged from the Radar extension (§40.5 there) and restated as the Development page layout: **What happened · What's actually new (spec diff vs. closest models) · Sources · When · Why it matters (AI_EXPLANATION) · Capability enabled · Limitations (computed gaps + cited caveats) · Relevant to my learning · Relevant to my platform · Can I experiment · Concepts to learn · Suggested next action.** Eight of the twelve are deterministic or mostly deterministic.

### 9.5 Development ↔ everything (the requested connections)

| Connects to | Mechanism |
|---|---|
| Concepts | `development_concepts` (proposed → confirmed) |
| Curriculum | Through Concepts → Tracks (`concept_terms`) → "Add to my plan" proposal (§17.6) |
| Models | `development_models` |
| Providers | Through models, or a `development_terms` provider term for provider-level news |
| Experiments | `experiments.development_id` |
| Platform components | `development_terms` with vocabulary `platform_component` (e.g., `tool_runtime` ↔ MA9) |
| Personal Eval Sets | Through experiment templates matching the Development's lane or role |
| Triage Decisions | `triage_decisions.development_id` |

### 9.6 Worked example: a new MCP development

| Step | What AIL shows | Source of truth |
|---|---|---|
| **Radar: what happened?** | "MCP specification revision adds X" · announced 2026-09-18 · first seen 2026-09-18 | S2 (spec site), FACT: document published; PROVIDER_CLAIM: stated benefits |
| **Evidence: what's verified?** | Verification: Documented. 0 independent evaluations. Corroboration: 3 independence groups. Attention: Rising | Derived |
| **Professor: what do I need?** | Tool Calling ✅ demonstrated · MCP fundamentals ◻ not started · Agent Permissions ◐ understood · Sandboxing ◻ not started | `development_concepts` × Learner State |
| **Curriculum: where does it fit?** | "Add MCP Fundamentals and Sandboxing to your plan after Agent Permissions (≈ 90 min)" | Planner (§17), user confirms |
| **Lab: can I try it?** | "Hands-on MCP lab: available after MA9. Available now: observation lab on your Tool Bus permission decisions" | `requires_platform_capability` |
| **Platform: could it matter?** | "Relates to MA9 Tool Runtime (MCP tool registration)" | `platform_component` term |
| **Human** | IGNORE / WATCH / LEARN / EXPERIMENT / INVESTIGATE, with rationale and a revisit trigger | `triage_decisions` |

---

## 10. Signal vs. Hype

### 10.1 Evidence Profile (visible dimensions, no composite)

| Dimension | Values | Derived from |
|---|---|---|
| Official announcement | yes / no | Claims from S2 sources |
| Documented | none / announcement only / docs + API reference | S2 FACT claims ("doc page exists") |
| Available to us | not available / waitlist / API / in our registry | Registry + S2 |
| Independent measurement | count of distinct S4 evaluators | BENCHMARK_RESULT claims |
| Corroboration | count of distinct `independence_group`s | `radar_items` → `radar_sources` |
| Measurable adoption | only where a real metric exists | S1/S5 samples |
| Sustained interest | present across 1 / 2 / 4+ weekly samples | `attention_samples` |
| Our experiments | n, date, outcome | `experiments` |
| Our usage | calls, tokens, cost | usage (opted-in projects) |

### 10.2 Verification Level (rule-derived, one label)

| Level | Rule |
|---|---|
| **Claimed** | Only PROVIDER_CLAIM and/or COMMUNITY_SIGNAL |
| **Documented** | ≥1 FACT claim that official documentation or an API reference exists |
| **Available** | Callable by us (in the registry, or a documented public API) |
| **Independently Measured** | ≥1 BENCHMARK_RESULT from a non-provider evaluator |
| **Tested by Us** | ≥1 PLATFORM_OBSERVATION from a completed Experiment |

The levels are cumulative where they can be (Tested by Us implies Available). A research-only Development can sit at Claimed, labeled "research result, no implementation available".

### 10.3 Templated honesty sentences

These are fixed templates keyed on (Verification Level × Attention × internal evidence). There's no generative wording, so they can't be spun:

| Condition | Sentence |
|---|---|
| Claimed + High attention | "High community attention, but only the provider's own claims so far. No independent verification recorded." |
| Documented/Available + 0 independent | "Provider claims this capability. Independent verification has not yet been recorded." |
| Independently Measured + no internal evidence | "Independent results exist. No internal evidence yet; an experiment template is available." |
| Tested by Us + small n | "Our test (n=4, Code Reviewer eval set v2) suggests X on this slice. Sample too small to generalize." |
| Corroboration = 1 group | "All reports trace back to a single origin." |
| Research only | "Interesting research result. No available implementation to test yet." |
| Limited evidence overall | "Interesting, but evidence is still limited." |

### 10.4 Hard anti-hype rules (enforced in code and covered by tests)

1. A source's class caps the claim types it can produce.
2. Provider-published benchmark numbers are PROVIDER_CLAIM.
3. Attention can't change Verification Level.
4. Corroboration counts independence groups, not articles.
5. Opportunities need Verification ≥ Documented plus a matching interest rule (§11).
6. AI_EXPLANATION may not introduce facts that aren't in its cited claims (§26.4).
7. PLATFORM_OBSERVATION always shows n and slice.
8. The Router never consumes Radar claims (§3.2).

### 10.5 Signal vs. hype as a learning outcome

Claim literacy is a **concept in the curriculum** ("Evaluating AI Claims", in AI Foundations). Its knowledge check asks the learner to classify real, anonymized statements from the Radar into the seven types, and it's a prerequisite for "Benchmarks & Their Limits" and "Personal Eval Sets". The Professor reinforces it every time it speaks (§16.6).

---

## 11. Opportunity Radar

### 11.1 Definition (unchanged in substance)

An **Opportunity** is a hypothesis: *"Capability change C, evidenced by Development D, may improve platform area P in way W. Evidence: E. Cheapest test: T."* It is not a roadmap item.

### 11.2 Generation (rule-bound only)

- Opportunities come only from **interest rules** the operator writes and maintains (`interest_rules`). A rule states: capability term(s), minimum Verification Level (≥ Documented), platform condition (e.g., "a role currently uses a reasoning-lane model", checked against usage), and the experiment template for the cheapest test.
- The LLM may draft the hypothesis *text* for a rule-created opportunity. It can't create an opportunity.
- **Open-item cap:** 5 by default. New candidates queue until the operator triages.
- **Mandatory triage and expiry:** each Opportunity requires a Triage Decision. WATCH requires a revisit trigger.
- **Terminal output:** INVESTIGATE produces an **Investigation view**: the Development, claims, Experiments run, results, open questions and the operator's rationale. It's exported as a note. Turning it into roadmap work happens outside AIL.

### 11.3 Examples (from the approved design)

| Capability change | Platform area | Hypothesis | Cheapest test | MA9-gated? |
|---|---|---|---|---|
| Cheaper reasoning models | Model Router | Cheaper reasoning tier for the Reviewer role | Reviewer eval set comparison | No |
| Long-context improvements | Agent context policy | Repo analysis without RAG | Repo-analysis eval set: long context vs. current | No |
| Improved multimodal | Evaluation / UI review | Screenshot-based UI review | Eval set with screenshot tasks | No (if tasks need no new tools) |
| Better computer use | Tool Runtime | Browser-operating agent | — | **Yes: WATCH/LEARN until MA9** |
| MCP ecosystem growth | Tool Registry | Import MCP servers as Tool Bus tools | Observation lab now; hands-on after MA9 | **Yes** |
| New speech models | New capability area | Meeting/transcription agent | Default LEARN unless the owner has a use case | — |

### 11.4 Triage vocabulary and outcomes

| Decision | Effect |
|---|---|
| **IGNORE** | Hidden; resurfaces only if Verification rises by two levels |
| **WATCH** | Revisit trigger required (date, or a condition from §24.3) |
| **LEARN** | Opens a Plan Change Proposal for the linked concepts (§17.6) |
| **EXPERIMENT** | Opens a pre-filled Experiment draft (§14) with estimated cost |
| **INVESTIGATE** | Opens an Investigation view for platform-level consideration |

"Use" or "incorporate into the platform" isn't an AIL action. It happens in MA surfaces (e.g., changing an Agent Version's `model_policy`). AIL **observes** the outcome (the model now appears in usage) and shows it on the Development and Model pages.

---

## 12. Model Comparison

### 12.1 What can be compared, and the rules

A side-by-side view of 2–4 models, organized in the same layers as the Model Explorer:

| Layer | Comparison rule |
|---|---|
| Facts (price, context, modalities, availability) | Always comparable; shown with as-of dates |
| Provider claims | Shown side by side, **never** turned into a "winner" |
| External benchmarks | Shown only when evaluator, benchmark name *and* benchmark version match; otherwise listed separately as "not comparable" |
| Our evaluation evidence | Compared only within the same (role, task type) slice, with n shown for each |
| Our Personal Eval results | Compared only within the **same eval set version**; different versions are shown separately |
| Cost projection | Deterministic: price × your median tokens per task for a chosen role (from usage) |

### 12.2 Output

Each layer produces a comparison table. There is **no overall verdict**. When no layer has comparable evidence, the view says: *"No comparable evidence on your work yet. Create an experiment on the Code Reviewer eval set (est. $0.84)."* Comparison is the natural springboard into the Experiment Lab.

---

## 13. Personal Eval Sets

### 13.1 Definition

A **Personal Eval Set** is a named, **versioned** collection of representative Tasks for one **role** (e.g., Software Engineer, Code Reviewer, Research Agent, Planner, Security Reviewer). It is the operator's personal laboratory for evaluating models on their own work.

### 13.2 Structure

| Element | Design |
|---|---|
| Eval Set | Name, role term, description, owner |
| Eval Set Version | Frozen list of Tasks; immutable once any Experiment runs against it; a new version is created on edit |
| Tasks | Ordinary MA `tasks` rows in the AIL system project (§29), each with requirements and, wherever possible, **objective checks** (tests, lint, expected output, scriptable assertions) |
| Task origin | Created fresh, or **copied** from a real project Task by an explicit "Add to eval set" action. Copying pins the `source_snapshot` (repo and base commit) so the task is reproducible |
| Non-coding roles | Tasks carry a rubric; evaluation uses human decision and/or a labeled LLM judge (MA §17.1: objective first, judge additive) |
| Recommended size | ≥5 tasks per role. The UI labels results as "directional only" below 10 task-runs per model |

### 13.3 Results are never universal

Every result display carries a mandatory **result slice header**:

> *Eval set "Code Reviewer" v2 · 6 tasks · 2 repetitions · 2026-09-14 · model `x` via provider `p` (snapshot #1182) · Agent "Reviewer" v4, prompt v7 · objective checks: tests + lint · judge: none*

Stored or derivable for every run: sample size, task slice, eval set version, date, model/provider snapshot, execution conditions (from `task_run_config_snapshot`: agent version, prompt version, temperature, budget), evaluation evidence, tokens, cost and latency.

Results are displayed as per-task outcome tables plus aggregates (pass counts, cost/task, median latency, failures). There's no single score. They enter the Radar and Model Explorer as **PLATFORM_OBSERVATION** claims whose text includes the slice.

### 13.4 Why this is the core advantage

Public benchmarks measure someone else's tasks. The Personal Eval Set turns any model release into a cheap, repeatable question: *"Is this better than what I use, on my work, at what cost?"* It's also the main source of hands-on evidence for the Model Intelligence and AI Quality & Evaluation tracks.

---

## 14. Experiment Lab

### 14.1 Experiment types

| Type | What it does | Uses | Spend | Available from |
|---|---|---|---|---|
| **Eval-set model comparison** | N models × eval set version × k repetitions | MA5 Comparison Runs, MA6 Evaluation | Yes | AIL.3 (needs MA5/MA6) |
| **Prompt-version comparison** | Same model and role, different prompt versions | MA comparison axis 3 | Yes | AIL.3 |
| **Variance run** | Same config repeated k times | MA comparison axis 4 | Yes | AIL.3 |
| **Routing policy simulation** | Draft scoring config vs. active, via dry-run | MA8 dry-run | **None** | AIL.3, if MA8 exists |
| **Observation study** | Structured analysis of existing records (e.g., cost per role in the last 30 days, reliability by provider) | SQL over platform tables | **None** | AIL.3 |
| Tool / MCP / computer-use experiments | — | MA9 | — | **After MA9** |

### 14.2 Lifecycle

```
DRAFT ──estimate──> ESTIMATED ──(over approval threshold?)──> AWAITING_APPROVAL ──> APPROVED
   │                    │                                                           │
   └── cancel           └──(under threshold)─────────────────────────────────────> RUNNING
RUNNING ──> COMPLETED ──(operator writes/accepts conclusion)──> CONCLUDED
RUNNING ──> FAILED | CANCELLED | BUDGET_EXCEEDED (MA budget semantics)
```

- **ESTIMATED** shows the projected cost: listed prices × historical tokens per task for the role (or a declared estimate when there's no history), multiplied by repetitions.
- Approval uses the **MA Approval Service** with operation type `ail_experiment_spend` (bound to an action fingerprint of the experiment config).
- **CONCLUDED** requires a conclusion. The LLM may draft it (labeled AI_EXPLANATION, citing evaluation IDs); the operator edits or accepts. The conclusion creates PLATFORM_OBSERVATION claims on the models involved.

### 14.3 Experiments as learning

An Experiment can be launched from a **Lab** (§20). When it is, completing it (and interpreting it, §19) writes Learning Evidence for the lab's concept. Experiments launched from the Radar or the Model Explorer can also be *claimed* afterwards as practice for a concept ("Count this as practice for Model Routing?"), and the operator confirms.

---

## 15. Concept Graph

### 15.1 Concept anatomy

| Field | Storage | Notes |
|---|---|---|
| Name, slug, aliases | `concepts` | Aliases power deterministic search matching (§25) |
| Level | `concepts.level`: `foundational` / `practitioner` / `advanced` | Used by the planner's depth filter |
| Kind | `concepts.kind`: `definitional` / `mechanism` / `operational` / `architectural` | Selects the default evidence requirements (§18.3) |
| Importance | `concepts.is_core` | Core concepts are eligible for retention review (§22) |
| Plain-language definition | `concept_versions.plain_definition` | 1–3 sentences, no jargon |
| Technical explanation | `concept_versions.technical_explanation` | Markdown |
| Examples | `concept_versions.examples_md` | General examples |
| Platform examples | `learning_items` of type `observation_task` + `concept_terms` → `platform_component` | "See it in my platform" |
| Prerequisites | `concept_relations` (`prerequisite`) | Hard edges, acyclic |
| Related concepts | `concept_relations` (`part_of`, `related` with a label) | Soft edges |
| Learning resources | `learning_items` of type `resource` | External links carry a source class |
| Exercises | `learning_items` of type `exercise` | — |
| Labs | `learning_items` of type `lab` | May create Experiments |
| Knowledge checks | `learning_items` of type `check_question` / `scenario` | §19 |
| Evidence requirements | `concept_versions.evidence_requirements` (JSON spec; a rule payload, not a relationship) | §18.3 |
| Demonstrated evidence | `learning_evidence` | Per learner |
| Content provenance | `concept_versions.content_origin` (`human` / `ai_drafted_reviewed` / `ai_drafted_unreviewed`), `reviewed_at` | Shown on the page |
| Freshness horizon | `concepts.freshness_days` (e.g., 365 for Tokens, 90 for Model Pricing) | §27 |
| Track membership | `concept_terms` → `track` terms | Many-to-many |

**Concept content is versioned.** Material edits create a new `concept_version`. Learning Evidence records the version it was earned against, which is what makes "what's changed since I learned this?" answerable (§16.3).

### 15.2 Relation types (deliberately few)

| Relation | Semantics | Constraint |
|---|---|---|
| `prerequisite` | A must be at least UNDERSTOOD before B is planned | Graph must be a DAG; validated on write |
| `part_of` | A is a component or sub-idea of B (e.g., "Retrieval step" part_of RAG) | No cycles |
| `related` | Labeled soft link: `contrasts_with`, `alternative_to`, `applies_to`, `often_confused_with` | — |

Why not more: every extra edge type has to be maintained by hand. These three support planning (prerequisite), navigation (part_of) and teaching contrasts (related), which is everything the product needs.

### 15.3 Seed graph (AIL.1 target: ~50 concepts)

| Concept | Kind | Level | Key prerequisites | Tracks |
|---|---|---|---|---|
| Tokens & Tokenization | definitional | foundational | — | Foundations |
| Context Windows | definitional | foundational | Tokens | Foundations, Model Intel |
| Inference (sampling, temperature, determinism) | mechanism | foundational | Tokens | Foundations, Infra |
| Training vs. Inference / Pre- vs. Post-training | definitional | foundational | — | Foundations |
| Hallucination & Grounding | mechanism | foundational | Inference | Foundations, Quality |
| **Evaluating AI Claims (claim literacy)** | operational | foundational | — | Foundations, Quality |
| Prompting | operational | foundational | Tokens | Prompting |
| System Instructions & Prompt Versioning | operational | practitioner | Prompting | Prompting, AI Eng |
| Structured Output | mechanism | practitioner | Prompting | Prompting, AI Eng |
| Model Families & Providers | definitional | foundational | — | Model Intel |
| Model Pricing & Token Economics | operational | foundational | Tokens | Model Intel, Infra |
| Latency & Throughput | definitional | practitioner | Inference | Model Intel, Infra |
| Reasoning Models / Test-time Compute | mechanism | practitioner | Inference, Model Families | Model Intel |
| Coding Models | definitional | practitioner | Model Families | Model Intel |
| Multimodal Models (vision, audio, video) | mechanism | practitioner | Tokens | Model Intel |
| Open Weights | definitional | practitioner | Model Families | Model Intel, Advanced |
| Local Models | operational | practitioner | Open Weights, Inference | Infra, Advanced |
| Benchmarks & Their Limits | mechanism | practitioner | Evaluating AI Claims | Model Intel, Quality |
| Embeddings | mechanism | practitioner | Tokens | AI Eng |
| Vector Search / Vector Databases | mechanism | practitioner | Embeddings | AI Eng |
| RAG | architectural | practitioner | Vector Search, Context Windows | AI Eng |
| Long Context vs. RAG | operational | practitioner | RAG, Context Windows, Pricing | AI Eng, Model Intel |
| Tool Calling | mechanism | practitioner | Structured Output | Agentic, AI Eng |
| Agents (agent loop) | architectural | practitioner | Tool Calling | Agentic |
| Agent ≠ Model (roles vs. inference resources) | definitional | practitioner | Agents, Model Families | Agentic, Multi-Agent |
| Agent Permissions / Least Privilege | operational | practitioner | Tool Calling | Agentic, Security |
| Sandboxing & Isolation | architectural | practitioner | Agent Permissions | Security, Infra |
| MCP | architectural | practitioner | Tool Calling, Agent Permissions | Agentic, AI Eng |
| Computer Use | architectural | advanced | Tool Calling, Sandboxing, Multimodal | Agentic |
| Human Approval Gates | operational | practitioner | Agents | Agentic, Security |
| Multi-Agent Systems | architectural | practitioner | Agents | Multi-Agent |
| Workflow / DAG Orchestration & Bounded Loops | architectural | practitioner | Multi-Agent Systems | Multi-Agent |
| Build + Review Pattern | operational | practitioner | Multi-Agent Systems | Multi-Agent, Quality |
| Parallel Comparison & Isolation | operational | practitioner | Multi-Agent Systems, Sandboxing | Multi-Agent, Quality |
| Evaluation (objective first, judge second) | operational | practitioner | Evaluating AI Claims | Quality |
| LLM-as-Judge | mechanism | practitioner | Evaluation | Quality |
| Variance & Reproducibility | mechanism | practitioner | Evaluation, Inference | Quality |
| Personal Eval Sets | operational | practitioner | Evaluation, Benchmarks & Their Limits | Quality, Model Intel |
| Observability / Flight Recorder | operational | practitioner | Agents | Quality, Infra |
| Model Routing | operational | practitioner | Model Pricing, Latency, Evaluation, Agent ≠ Model | Infra, Model Intel |
| Cost Governance & Budgets | operational | practitioner | Model Pricing | Infra |
| Provider Gateways (e.g., OpenRouter) | definitional | practitioner | Model Families | Infra |
| Inference Serving | mechanism | advanced | Inference, Latency | Infra, Advanced |
| Quantization | mechanism | advanced | Inference, Open Weights | Advanced |
| Fine-Tuning | mechanism | advanced | Training vs. Inference | Advanced |
| Distillation | mechanism | advanced | Fine-Tuning | Advanced |
| Mixture of Experts | mechanism | advanced | Inference | Advanced |
| Prompt Injection | mechanism | practitioner | Tool Calling, RAG | Security |
| Data Privacy & Retention with Providers | operational | practitioner | Provider Gateways | Security |
| Secret Handling for AI Systems | operational | practitioner | Agent Permissions | Security |

### 15.4 Graph fragment (how concepts relate rather than a flat glossary)

```
Tokens ─┬─> Context Windows ──────────┐
        ├─> Model Pricing ───────────┐│
        ├─> Inference ──> Latency ──┐││
        │                           ││└─> Long Context vs RAG <── RAG <── Vector Search <── Embeddings
        └─> Prompting ─> Structured Output ─> Tool Calling ─┬─> Agents ─┬─> Multi-Agent ─> Workflow/DAG
                                                            │           └─> Human Approval
                                           Agent Permissions┴─> MCP
                                                  └─> Sandboxing ─> Computer Use
Evaluating AI Claims ─> Benchmarks ─> Personal Eval Sets <── Evaluation ──> LLM-as-Judge
Model Pricing + Latency + Evaluation + Agent≠Model ──> Model Routing
related: RAG ⟷ Fine-Tuning (alternative_to) · LLM-as-Judge ⟷ Objective Evaluation (contrasts_with)
```

### 15.5 Governance

- The graph is **curated**, not auto-extracted from news. The owner approves every new concept and prerequisite edge.
- Seed content may be LLM-drafted and is labeled `ai_drafted_unreviewed` until the owner reviews it (whether review is mandatory before activation is owner decision **D3**).
- The Extractor may propose `unmapped_concept_candidate` when a Development needs a concept the graph doesn't have. These go to an owner queue and never become concepts automatically.

### 15.6 Development → Concept linking

Links are proposed by the Extractor (restricted to existing concept IDs) or added manually, and they're shown as *proposed* until the user confirms. Only **confirmed** links drive the "Relevant to my learning" section, freshness flags and review triggers.

---

## 16. AI Professor

### 16.1 Architecture: a teaching system, not a chatbot

```
User request ──> Intent Resolver ──> Context Assembler ──> Pedagogy Policy ──> Generator (LLM) ──> Output Validator ──> Response
                 (rules first,        (only data allowed    (teach sequence,     (Professor Agent,     (citations resolve,
                  LLM parse to a      by the intent's        Socratic mode,       versioned,             claim-type phrasing,
                  fixed intent set)   context contract)      level adaptation)    no tools)              no uncited platform IDs)
                                                                                                          │
                                  Graded activities only ──> Evidence Writer ──> learning_evidence
```

- The Professor is a **registered Agent** (`AIL Professor`) in the AIL system project with versioned prompts. Its calls are Agent Runs with full Flight Recorder coverage and budget accounting (§29).
- **Conversation never writes evidence.** Only graded activities (checks, scenarios, labs, interpretations) do, through the Evidence Writer.
- The Professor **never invents platform records**. Any record it mentions must come from the assembled context and be cited by ID with a deep link. The validator rejects the response otherwise.

### 16.2 Intents: what it answers and how

| User asks | Intent | Deterministic part | LLM part |
|---|---|---|---|
| "What should I learn next?" | `next` | Planner rule (§17.5) picks the concept and reason | Explains why, citing the plan, evidence and developments |
| "Teach me MCP." | `teach` | Loads concept version, learner state and prerequisite gaps; if prerequisites are missing, offers the prerequisite first | Lesson at the learner's level: definition → mechanism → example → platform example → check-for-understanding question |
| "Explain this model release." | `explain_development` | Loads the Development's claims (typed), spec diff and Verification Level | Explanation using provenance voice (§16.6) |
| "Why is this development important?" | `explain_relevance` | Relevance reasons (platform, learning, watch) | Narrative over those reasons only |
| "Explain this MA8 routing decision." | `explain_routing_decision` | Loads *that one* `model_routing_decisions` row, its policy version and the candidate snapshot | Walks through the candidates, filters and scores; links to the Model Routing concept |
| "What am I weak on?" | `diagnose` | Query: failed/partial checks, concepts stuck at EXPOSED/UNDERSTOOD, review-failed flags | Summary + one suggested action |
| "What have I actually practiced?" | `practiced` | Query: hands-on evidence (labs, experiments, observations) | Optional short synthesis |
| "Give me an exercise." | `exercise` | Picks an exercise for a concept in the plan at the right level | May generate a variant (labeled as generated) |
| "Test my understanding." | `check` | Draws from the reviewed question bank first (§19) | Generates extra questions only if the bank is exhausted (labeled; see evidence rules) |
| "Show me how this concept exists in my own platform." | `platform_lens` | Runs the concept's observation query (e.g., last 10 routing decisions, denied tool calls) in opted-in projects | Explains the records |
| "What's changed since I learned this?" | `whats_changed` | Diff: concept versions since the evidence's `concept_version_id`; confirmed Developments linked since `demonstrated_at`; registry changes for models referenced in the evidence | Synthesis citing each item |

### 16.3 "What's changed since I learned this?" (precise definition)

For concept C with DEMONSTRATED evidence at time T against version V:
1. `concept_versions` of C newer than V, with their `change_note`
2. Developments with a **confirmed** link to C and `announced_at > T`
3. For Personal Eval or lab evidence that referenced models: registry snapshot changes to those models since T

If all three are empty: "Nothing recorded has changed since you demonstrated this on 2026-05-03." The Professor doesn't speculate beyond the records.

### 16.4 Pedagogy policy

| Behavior | Rule |
|---|---|
| Level adaptation | Uses the learner profile level + concept state; skips basics already DEMONSTRATED |
| Prerequisite gating | Offers the prerequisite first; the learner may override ("teach it anyway"), which is recorded |
| Check for understanding | Every `teach` ends with one question; the answer is graded as a `scenario`-type evidence attempt only if the learner opts in ("count this") |
| Socratic exercises | For exercises, it gives hints before answers; `reveal` is explicit |
| Worked examples from real change | Uses a recent confirmed Development or a real platform record as the example when one exists |
| No false mastery | Never says "you know X" unless Learner State says DEMONSTRATED, and it cites the evidence |
| Uncertainty | Says "I don't have a record of that" instead of guessing about platform or learner facts |

### 16.5 Context contract (strict privacy and scope)

| Data | Professor may read | Condition |
|---|---|---|
| Concept Graph, concept content, Learning Items | Always | — |
| Learner profile: level, goals, depth, time, **explicit** interests | Always | Only what the user entered |
| Learning Plan, Learner State, Learning Evidence | Always | Own user only |
| Radar Developments, Claims, Sources | Always | — |
| Model Registry | Always | — |
| Experiments, Personal Eval results | Always | Own experiments |
| Platform records (routing decisions, evaluations, run metadata, usage aggregates, tool-call permission decisions) | **Only from projects opted in** to AIL evidence (§30) | Per-intent minimum: e.g., `explain_routing_decision` loads one decision |
| Task descriptions, code, diffs, artifacts, prompts, model outputs from project runs | **No, by default.** Only when the user explicitly attaches a specific record to the conversation ("explain this run"), and only that record | Never bulk-loaded |
| Professor conversation history | Current session only, plus explicit "continue from" | Past transcripts are **not** mined for interests or profile |
| Anything outside the platform (files, email, browsing) | **Never** | — |

Interests are **declared**, never inferred. The Professor may *suggest* an interest ("you've asked about MCP three times; add Agentic AI to your interests?"), but it's only saved if the user confirms.

### 16.6 Provenance voice (core teaching behavior)

Whenever the Professor uses a Claim, it renders it with the claim type's attribution pattern:

| Type | Voice |
|---|---|
| FACT | "Per the OpenRouter catalog (as of Sep 21), the context window is 1M tokens." |
| PROVIDER_CLAIM | "The provider **states** it is better at multi-step tool use. That's their claim." |
| RESEARCH_RESULT | "A paper by … **reports**, under <setup>, that …" |
| BENCHMARK_RESULT | "**<Evaluator> measured** … on <benchmark vN> (Sep 2026)." |
| COMMUNITY_SIGNAL | "Community discussion **suggests** … (anecdotal, not verified)." |
| PLATFORM_OBSERVATION | "**In your 6 Code Reviewer tasks** (eval set v2, Sep 14), it passed 5/6 at $0.09/task." |
| AI_EXPLANATION | "**My interpretation:** …" |

The validator checks that every cited claim ID appears with its type's phrasing pattern. When sources conflict, the Professor presents them side by side ("the provider says X; independent evaluator Y found Z; your runs haven't tested this") rather than resolving the conflict by assertion.

---

## 17. Personalized Curriculum

### 17.1 Inputs

| Input | Source | Captured how |
|---|---|---|
| Learner level | `learner_profiles.level` | Onboarding (self-selected) + optional placement check |
| Goal | `learner_profiles.goal_text` + selected goal **Tracks** / goal concepts | Onboarding, editable |
| Desired depth | `learner_profiles.depth`: `survey` / `working` / `deep` | Onboarding |
| Time available | `learner_profiles.weekly_minutes` | Onboarding |
| Interests | `learner_interests` (lanes, tracks) | Explicit selection |
| Demonstrated concepts | Learner State | Derived |
| Prerequisites | Concept Graph | — |
| Experiments completed | `learning_evidence` (lab/experiment types) | Derived |
| Knowledge-check results | `learning_evidence` | Derived |

### 17.2 Tracks are views, not courses

A Track is a `taxonomy_terms` row (vocabulary `track`) plus its goal concepts, via `concept_terms`. The ten requested tracks are seeded: AI Foundations, Model Intelligence, Prompting, Agentic AI, AI Engineering, Multi-Agent Systems, AI Quality & Evaluation, AI Security, AI Infrastructure, Advanced Model Intelligence. A concept can belong to several tracks. **Nothing is hardcoded as one universal sequence.**

### 17.3 Plan construction (deterministic planner)

```
goal_set   = goal concepts from selected Tracks ∪ explicitly chosen concepts
filtered   = goal_set filtered by depth  (survey: foundational + core practitioner;
                                          working: + practitioner; deep: + advanced)
closure    = filtered ∪ all transitive prerequisites
todo       = closure − {concepts DEMONSTRATED (and not REVIEW_FAILED)}
order      = topological sort of todo over prerequisite edges;
             ties broken by: (1) in an interest lane, (2) linked to a Development triaged LEARN,
                             (3) concept level ascending, (4) name
estimate   = Σ estimated minutes per concept (from its learning items' declared durations)
schedule   = estimate ÷ weekly_minutes → "≈ 6 weeks at 3 h/week" (a visible estimate, not a deadline)
```

The LLM's role is limited to writing the plan rationale paragraph and, optionally, suggesting reorderings that the user can accept. It never adds concepts outside the graph.

### 17.4 User control

The user can reorder items, skip ("I don't need this"; recorded, not treated as demonstrated), remove, add any concept (its missing prerequisites are offered but not forced), pin, and switch goals. Every edit is recorded (`learning_plan_items.origin`, plus audit events). The planner **never overwrites user edits**. It proposes changes, shown as a diff to accept.

### 17.5 "Learn next" rule (used by Home, Brief and the Professor)

Pick the first `learning_plan_items` entry in `planned` state whose prerequisites are all ≥ UNDERSTOOD. If a concept linked to a Development shown today satisfies the same condition and sits within the next 3 plan positions, prefer it ("timely"). The reason is always displayed.

### 17.6 Plan Change Proposals (Radar → Curriculum)

Triage LEARN, the Professor's "add to plan", or a Development's "Add these concepts" create `learning_plan_items` rows in state `proposed` with `origin = radar` (and a `development_id`). The user sees the proposal as a diff (concepts, insertion points that respect prerequisites, added time), then accepts or rejects it. Nothing enters the active plan without acceptance.

---

## 18. Learner State

### 18.1 Critique of the suggested states

The brief suggested NOT_STARTED, EXPLORING, LEARNING, PRACTICED, DEMONSTRATED, NEEDS_REVIEW. Three problems:

1. **EXPLORING vs. LEARNING** describe *activity*, not evidence. The system can't defensibly tell them apart.
2. **NEEDS_REVIEW as a state** destroys information. A concept that was DEMONSTRATED and is now stale is different from one never demonstrated, and a single state can't carry both facts.
3. There's no evidence-based state for "understands but hasn't practiced", which is the most common and most useful intermediate state.

### 18.2 Adopted model: one evidence ladder + independent overlays

**Evidence ladder (derived; never set manually):**

| State | Meaning | Minimum evidence |
|---|---|---|
| **NOT_STARTED** | No interaction | — |
| **EXPOSED** | Has engaged with teaching material. **Explicitly not knowledge** | Lesson completed or Professor `teach` session completed |
| **UNDERSTOOD** | Has shown conceptual understanding | Passed a knowledge check (≥ threshold, reviewed items) |
| **PRACTICED** | Has hands-on evidence, but the concept's full requirement set isn't met | ≥1 observation task, lab, experiment or verified platform action |
| **DEMONSTRATED** | Has met the concept's full evidence requirement set (§18.3) against a current or compatible concept version | Per concept kind |

The ladder is the highest level whose rule is satisfied. PRACTICED doesn't require UNDERSTOOD, so a learner can practice first; the page then shows "Practiced (knowledge check not yet passed)".

**Overlays (flags displayed alongside the state):**

| Overlay | Trigger |
|---|---|
| **REVIEW_DUE** | Core concept, DEMONSTRATED, past its review interval with no recent use (§22) |
| **CHANGED** | New material concept version or confirmed linked Development since DEMONSTRATED (§16.3) |
| **REVIEW_FAILED** | Latest review attempt failed. The concept stays DEMONSTRATED historically, but the planner treats it as todo |
| **SELF_REPORTED** | The user says they know it. Informational only; offers a placement check. **Never** changes the ladder |

### 18.3 Evidence requirements differ by concept kind

Defaults per kind (stored per concept version in `evidence_requirements` and overridable per concept):

| Kind | Example | DEMONSTRATED requires |
|---|---|---|
| **Definitional** | Tokens, Context Windows, Open Weights | Knowledge check ≥ 80% on reviewed items **and** 1 correct scenario question |
| **Mechanism** | RAG, Embeddings, Quantization, Tool Calling | Knowledge check **and** scenario **and** (1 lab **or** 1 graded interpretation) |
| **Operational** | Model Routing, Evaluation, Cost Governance, Human Approval | Knowledge check **and** ≥1 observation task on real records **and** ≥1 lab/experiment **and** ≥1 graded interpretation |
| **Architectural** | Agents, Multi-Agent Systems, MCP, Sandboxing | Knowledge check **and** graded design scenario **and** 1 observation task of the platform implementation **and** (1 lab, when available) |

**Model Routing, concretely:**

| Requirement | Satisfied by |
|---|---|
| Explanation | Knowledge check: "Routing" question set ≥ 80% |
| Observe real decisions | Observation task: inspect ≥ 3 real `model_routing_decisions` and answer record-grounded questions (§20.3) |
| Configure / simulate | Lab: draft a scoring config and run the dry-run comparison (MA8) — *alternative:* eval-set comparison of 2 models for one role |
| Run a comparison | Experiment: eval-set comparison completed |
| Interpret | Graded interpretation: "Explain why candidate A outranked B in decision #…" (rubric) |

### 18.4 Evidence integrity rules

1. **No DEMONSTRATED on AI-graded evidence alone.** At least one requirement must be satisfied by deterministic grading (auto-scored check or record-grounded observation) or a completed lab with objective checks.
2. **Generated questions count only toward UNDERSTOOD** when the reviewed bank for the concept is exhausted, and they're labeled `question_origin = generated`.
3. **Evidence is append-only** and records the `concept_version_id`, grader type (`deterministic` / `ai_rubric` / `human` / `self`), score and a reference to the underlying record.
4. **Alternative requirement sets:** when an item is gated (e.g., MA9), the concept version declares an alternative set. The concept page shows which set was satisfied.
5. **Version compatibility:** a new concept version is marked `material` or `minor`. Minor changes preserve DEMONSTRATED; material changes add the CHANGED overlay but don't revoke the state.
6. **Disputes:** the learner may dispute an AI-graded result. It's then re-graded by the owner/human or excluded, and the dispute is recorded.

### 18.5 Computation

Learner State is **computed on read** by the Learner State Service from `learning_evidence` + `concept_versions.evidence_requirements`. There's no stored state table (the evidence volume for one learner is small). A cache table is deferred until profiling shows a need (§32.4).

---

## 19. Knowledge Checks

### 19.1 Item types

| Type | Grading | Use |
|---|---|---|
| Single / multiple choice | Deterministic | Definitions, distinctions |
| Numeric | Deterministic (tolerance) | e.g., "Cost of 40K in + 3K out at $3/$15 per M?" |
| Ordering / matching | Deterministic | Pipelines (RAG steps), prerequisite reasoning |
| **Claim classification** | Deterministic | Classify statements into the seven claim types |
| Scenario (choice-based) | Deterministic | "Given this requirement profile, which model is eligible and why?" |
| **Record-grounded question** | Deterministic, from a real record | "In decision #4411, which candidate was excluded, and for what hard requirement?" (§20.3) |
| Short explanation / design scenario | AI rubric (labeled) + optional human review | "Design an approval policy for …" |
| Interpretation | AI rubric (labeled) | "Explain the result of experiment #37" |

### 19.2 Question bank and anti-gaming

- **Reviewed bank:** items with `reviewed = true`, versioned and tied to concept versions. The pass threshold is computed only from reviewed items.
- **Randomization:** random item selection and option order.
- **Retry cooldown:** after a failed check, a retry for the same concept uses different items and is allowed after a cooldown (default 12 h). The attempt history is visible.
- **Short checks:** 3–6 items. The goal is evidence, not exams.

### 19.3 Rubric grading safeguards

The AI grader is a separate registered Agent (`AIL Grader`) with the rubric, the learner answer and the reference explanation as its **only** context. It outputs a structured score, per-criterion notes and a confidence value. Low-confidence grades are marked "provisional" and don't count until confirmed or regraded.

---

## 20. Labs / Learn by Doing

### 20.1 The learning chain

```
CONCEPT ─> EXPLANATION ─> EXERCISE ─> OBSERVE (real platform records) ─> LAB / EXPERIMENT ─> INTERPRET ─> KNOWLEDGE CHECK ─> LEARNING EVIDENCE
```

Not every concept uses every step. The concept kind determines which steps are required (§18.3).

### 20.2 Lab types

| Lab type | What the learner does | Platform capability used | Grading |
|---|---|---|---|
| **Observation task** | Opens real records (routing decisions, evaluations, Flight Recorder events, denied tool calls, budget events) and answers questions about them | Read-only queries on opted-in projects | Deterministic, record-grounded |
| **Configuration lab** | Creates a draft config in the **AIL system project**: an Agent Version with tool grants, a budget with thresholds, an approval policy, a draft router scoring config (dry-run only) | MA2/MA3 CRUD, MA8 dry-run | Deterministic checks on the resulting config |
| **Experiment lab** | Runs a guided Experiment (§14) | MA5/MA6 | Experiment completes + interpretation |
| **Interpretation lab** | Explains a result (experiment, evaluation, routing decision) | — | AI rubric (labeled) |
| **Tool / MCP / computer-use lab** | Hands-on with new tools | **MA9** | — (gated) |

### 20.3 Record-grounded questions (a key design move)

Observation tasks **generate questions deterministically from real records**, so grading needs no LLM:

- *Routing:* pick a real `model_routing_decisions` row; ask "Which model was selected?", "Which candidate was filtered out, and by which hard requirement?", "Which factor contributed most to the winner's score?" The answers come from the stored JSON snapshot.
- *Evaluation:* "Which objective metric failed in evaluation #…?"
- *Cost:* "What fraction of this task run's cost came from the review step?"
- *Tool Bus:* "Which permission rule denied tool call #…?"

If no opted-in records exist yet, the lab uses a **seeded demo dataset** (synthetic records clearly labeled as such), and the evidence is recorded with `on_demo_data = true`. Whether demo-data evidence can count toward DEMONSTRATED is owner decision **D5**. The proposed default is that it counts toward PRACTICED only.

### 20.4 Worked example: Model Routing

| Step | Activity | Evidence written |
|---|---|---|
| Learn | Professor `teach` on Model Routing | `lesson_completed` → EXPOSED |
| Check | 5-item check (eligibility, scoring, manual vs. auto) | `knowledge_check` 4/5 → UNDERSTOOD |
| Observe | Inspect 3 real routing decisions; answer 6 record-grounded questions | `observation` 6/6 → PRACTICED |
| Experiment | Compare AUTO policies via dry-run (MA8), or run a 2-model Reviewer eval-set comparison | `lab` completed |
| Interpret | "Explain why the router chose model A for run #…" (rubric) | `interpretation` pass |
| Result | All requirements met | **DEMONSTRATED**, with a visible evidence list |

### 20.5 Worked example: MCP (before MA9)

Learn + check (UNDERSTOOD) → observation task on Tool Bus permission decisions (PRACTICED) → design scenario: "Define least-privilege grants for an MCP filesystem server" (graded) → **DEMONSTRATED via the alternative set**, with the note "hands-on MCP lab available after MA9". When MA9 ships, the lab appears and is offered as an optional upgrade.

---

## 21. My AI Knowledge

### 21.1 Purpose

This page answers **"What do I actually know, and what have I practiced?"** with evidence behind every claim.

### 21.2 Track summary (counts with explicit denominators, no bare percentages)

```
AI FOUNDATIONS — 8 concepts in this track
  Demonstrated 5 · Practiced 1 · Understood 1 · Exposed 0 · Not started 1
  Overlays: 1 review due · 1 changed since demonstrated

AGENTIC AI — 9 concepts in this track
  Demonstrated 2 · Practiced 3 · Understood 1 · Exposed 1 · Not started 2
  (2 labs available after MA9)

MODEL INTELLIGENCE — 11 concepts in this track
  …
```

The denominator is the number of concepts tagged with the track, and the count is shown. A percentage appears only as a secondary label next to its fraction ("5 of 8").

### 21.3 Concept detail: "why does AIL believe this?"

```
MODEL ROUTING — DEMONSTRATED (since 2026-08-30, against concept v3)
Requirement set: Operational (default)
  ✓ Knowledge check          4/5 reviewed items · 2026-08-21 · deterministic       [view attempt]
  ✓ Observe real decisions   3 decisions, 6/6 record-grounded answers · 2026-08-24  [view]
  ✓ Lab                      Dry-run policy comparison · 2026-08-27                 [view experiment #12]
  ✓ Experiment               Reviewer eval set v2, 2 models, n=12 runs · 2026-08-29 [view]
  ✓ Interpretation           Pass (AI rubric, confidence high) · 2026-08-30         [view answer + grade]
Overlays: CHANGED — concept v4 (material: "adds budget-aware downgrade") · 1 linked development since
Actions: [What's changed?] [Review now] [Dispute a grade]
```

### 21.4 Also on the page

- **Practice log:** a chronological list of every hands-on activity (labs, experiments, observations), across all concepts.
- **Gaps:** concepts in the active plan stuck at EXPOSED/UNDERSTOOD for > 14 days.
- **Export:** full learner evidence as JSON/Markdown (the user owns it; §30).

---

## 22. Learning Retention / Review

### 22.1 Stance

A full spaced-repetition system (SM-2 style) isn't justified here: concepts aren't flashcards, the volume is small (~50–150 concepts), and **real platform use is itself retrieval practice**. The design is a lightweight, low-frequency review mechanism.

### 22.2 Rules

| Aspect | Rule |
|---|---|
| Eligible | Concepts with `is_core = true` **or** in the active plan, and currently DEMONSTRATED |
| Interval | Default by kind: definitional 180 days, mechanism 120, operational 90, architectural 120. Doubled after each successful review (max 365) |
| Reset by use | Any new evidence for the concept (including observation tasks and experiments) resets the clock. In AIL.4, an opted-in platform action tied to the concept (e.g., editing a budget for Cost Governance) also counts as use |
| Also due when | The CHANGED overlay is set with a material change |
| Frequency cap | At most 2 review prompts per week, shown on Home ("Review due") and in the Brief; never as notifications |
| Format | One of: 1 record-grounded question · 1 scenario · a 5-minute mini-lab. Chosen to be the cheapest format that produces evidence for that concept kind |
| Outcome | Pass → evidence appended, interval extended. Fail → REVIEW_FAILED overlay; the planner re-adds the concept; history is preserved |
| Dismiss | "Not now" (snooze 14 days) or "Not important to me" (removes the concept from review eligibility; recorded) |

Example prompt: *"You demonstrated Model Routing 4 months ago and haven't used it since. One question: in last week's routing decision #5120, why was model B excluded?"*

---

## 23. Personal Intelligence Brief

### 23.1 Relationship to Home

The Brief is a **stored snapshot** of the Home generator over a period. It uses the same sections, caps and rules (§6), plus a period header and a learning-progress summary. It can be generated **on demand** (any range, any lane filter) in AIL.2, and weekly and daily cadences are added in AIL.4 as stored schedules. **No notification infrastructure.** Briefs appear in the Briefs archive and on Home.

### 23.2 Sections

Header (period, sources watched, items considered/shown, stale sources, blind spots) · Important developments (≤3) · New models · Model/provider changes · Pricing · Capability changes · Attention signals · Relevant to my platform · Relevant to my learning · Learn next (1) · Experiments to try (≤2) · My platform (model calls, tokens, cost, experiments run, new evaluations; period vs. prior) · My learning progress (evidence added, state transitions) · Pending decisions.

### 23.3 Deterministic vs. LLM

| Deterministic | LLM (labeled AI_EXPLANATION, cited) |
|---|---|
| Selection, ordering, caps, empty-section lines, reason lines, all numbers, all diffs, platform metrics, learning progress | One "why it matters" sentence per important development (cached per Development, not regenerated per brief); the "Learn next" paragraph |

### 23.4 Reproducibility

`brief_snapshots` records the period, kind, generator version and the Professor/Writer `agent_run_id` (which gives cost and model through the Flight Recorder). `brief_entries` records each shown item, its section, its position and its **reason text**. Reopening an old Brief shows exactly what was shown and why.

---

## 24. Watchlists

### 24.1 What can be watched

| Subject | How it's watched | Storage |
|---|---|---|
| Development | Triage WATCH | `triage_decisions` |
| Model | Triage WATCH on the model | `triage_decisions.model_id` |
| Provider, lane, topic, capability | Interest / watch term | `learner_interests` with `watch = true` |
| Concept | "Watch for changes" (the CHANGED overlay is surfaced prominently) | `learner_interests` with the concept term |

A single **Watchlist page** shows all of them: current Verification Level, attention, last change, trigger status and "days watched".

### 24.2 Watch semantics

WATCH on a Development or Model **requires a revisit trigger**. Watching lanes, providers or concepts doesn't (those just shape relevance).

### 24.3 Trigger types (deterministic, evaluated after each refresh)

| Trigger | Example |
|---|---|
| Date | Revisit on 2026-11-01 |
| Verification reaches level | "When Independently Measured" |
| Price condition | "Input price < $1.00/M" |
| Availability | "When in our registry" / "when API available" |
| New claim type appears | "When a BENCHMARK_RESULT is recorded" |
| Attention sustained | "When attention is Sustained for 4 weeks", allowed only together with a verification condition (P3) |

A fired trigger puts the item in **Pending decisions** on Home and in the Brief. Stale WATCH items (> 90 days, no trigger fired) are surfaced once for re-triage.

### 24.4 "Watched but never tested"

This is a standard saved query on the Watchlist page: WATCH decisions with Verification ≥ Available and no linked Experiment. It feeds the retrospective (§36).

---

## 25. Search / Ask AIL

### 25.1 Pipeline (LLM last, not first)

```
Query
 ├─1. Deterministic parse: entity dictionary (concept names + aliases, model IDs/families, providers,
 │     lanes, tracks, platform components incl. "MA8"/"MA9"), time phrases, personal markers ("I", "my"),
 │     verbs (what is / show / explain / teach / compare / test)
 ├─2. If a structured intent is recognized with confidence → structured query → result list  (no LLM)
 ├─3. Definitional ("what is X") with X a concept → concept page (no LLM) + "Ask the Professor" button
 ├─4. Teaching / explanation verbs → Professor intent (§16.2)
 ├─5. Unrecognized → full-text search (SQLite FTS5) over concepts, developments, claims, experiments
 ├─6. Still ambiguous → LLM parses the query into a **fixed query-spec schema**, executed deterministically;
 │     the parsed spec is shown ("Interpreted as: models · capability=coding · new · last 30 days")
 └─7. External lookup: only on an explicit "Check sources now" action → triggers an immediate fetch of
       registered sources; results arrive as unprocessed Source Items, never as unattributed answers
```

Semantic (embedding) retrieval is an AIL.4 addition for step 5, behind FTS5. The query-spec schema means the LLM never answers a factual "what changed" question from its own training knowledge.

### 25.2 Routing of the example queries

| Query | Route | Notes |
|---|---|---|
| "What is MCP?" | Concept page (structured) | Professor optional |
| "Show me new coding models" | Structured: snapshots `change_kind=new` × coding capability term × last 30 days | Label: "coding" classification is from registry capability data (FACT) or provider-declared tier (PROVIDER_CLAIM), shown per row |
| "What changed in Gemini this month?" | Structured: Developments + snapshot changes where family/provider = Gemini, this month → optional LLM summary over the result set, cited | The summary is AI_EXPLANATION |
| "What reasoning models are trending?" | Structured: attention series × reasoning lane | Header: "Attention is not quality"; Verification Level shown per row |
| "Which coding models have I personally tested?" | Structured: experiments × models × coding-role eval sets | Pure platform query |
| "What did I learn about RAG?" | Structured: learning evidence for RAG → optional Professor recap | — |
| "What should I understand before learning MCP?" | Structured: prerequisite closure minus DEMONSTRATED | Graph traversal |
| "Which model releases relate to MA9?" | Structured: Developments (type model_release/protocol/tooling) × `platform_component` terms mapped to MA9 | Requires the term mapping in §28.3 |
| "Show me things I WATCHED but never tested." | Structured saved query (§24.4) | — |
| "Why did the router pick X yesterday?" | Professor `explain_routing_decision` after structured lookup of the decision | Links to the MA8 Router View |

---

## 26. Provenance Architecture

### 26.1 The seven claim types (mandatory, preserved from the approved design)

| Type | Definition | Test |
|---|---|---|
| **FACT** | Verifiable from a primary structured source at a timestamp | Could a script check it? |
| **PROVIDER_CLAIM** | What the maker says about its own product, *including its own benchmarks* | The source is the provider or an affiliate |
| **RESEARCH_RESULT** | A finding reported in a paper under stated conditions | The source is a paper; the claim carries its setup |
| **BENCHMARK_RESULT** | A score from a non-provider evaluator with published methodology | Evaluator ≠ provider; methodology link present |
| **COMMUNITY_SIGNAL** | Attention, anecdotes, practitioner reports | From an S5 source |
| **PLATFORM_OBSERVATION** | Our own measured result | Links to an Experiment or Evaluation, with n and slice (live usage figures are computed on read, not stored as claims) |
| **AI_EXPLANATION** | Text generated by AIL | Always generated by an AIL Agent; must cite the claims it rests on |

### 26.2 Source class → claim-type ceiling

| Source class | May produce |
|---|---|
| S1 Structured catalog (registries, release APIs) | FACT |
| S2 Official provider | PROVIDER_CLAIM; FACT only for verifiable specifics (doc exists, parameter documented, price listed) |
| S3 Research | RESEARCH_RESULT |
| S4 Independent evaluation | BENCHMARK_RESULT |
| S5 Community | COMMUNITY_SIGNAL |
| S6 Our platform | PLATFORM_OBSERVATION |
| S7 AIL's own agents | AI_EXPLANATION |

This is enforced by a validator on every claim write, and CI tests assert it for each class.

### 26.3 Claim record requirements

Every claim stores: type, text, subject (`development_id` and/or `model_id`), **exactly one** origin (`source_item_id` for S1–S5, `experiment_id`/`evaluation_id` for S6, `agent_run_id` for S7), `quote_span` for prose-derived claims (verified as an exact substring), `as_of`, `created_by` (`rule` / `agent` / `user`), and `status` (`active` / `superseded` / `disputed`). AI_EXPLANATION claims additionally store the IDs of the claims they cite (`claim_citations` join).

### 26.4 Grounded explanation enforcement

- **Generation input:** only the structured claim set for the subject, with IDs.
- **Output format:** sentences carry `[c:ID]` markers.
- **Validator (AIL.2):** every marker resolves; every sentence containing a number, date, name or comparative has at least one marker; unmarked factual sentences cause regeneration once, then the offending sentence is dropped.
- **User flag:** "This sentence isn't supported" marks the claim `disputed` and the sentence is hidden until regenerated.

### 26.5 Provenance in the UI

Every claim renders with a small type chip (FACT, PROVIDER, RESEARCH, BENCHMARK, COMMUNITY, OURS, AI) and a source link on hover. Types can't be hidden. Lists of claims are grouped by type in a fixed order: Ours → Fact → Benchmark → Research → Provider → Community → AI.

---

## 27. Freshness Architecture

| Object | Freshness signal | Stale when | UI behavior |
|---|---|---|---|
| Registry data | `provider_models.last_refreshed_at` (MA §13.3) | > 24 h | "Registry last refreshed 31 h ago" banner; prices shown with as-of |
| Radar source | `radar_sources.last_success_at`, per-source cadence | > 3× cadence | Source flagged in the Brief header and on the Sources page |
| Claim | `as_of` | Superseded by a newer claim on the same subject/key | Old claim shown collapsed as "superseded" |
| Benchmark result | `as_of` + benchmark version | Newer version exists | "Older benchmark version" label |
| Attention | `sampled_at` | No sample in 7 days | Attention shown as "unknown" |
| Concept content | `concept_versions.reviewed_at` + `concepts.freshness_days` | Past horizon **or** confirmed Development since review | "May be outdated: 2 developments since last review" + owner review queue |
| Learner State | Evidence dates | — (retention handles this) | REVIEW_DUE / CHANGED overlays |
| Personal Eval results | Run date + model snapshot | Model snapshot changed since the run (price/version) | "Model changed since this test" label |
| Professor answers about "current" things | — | — | Must state as-of dates; answers citing stale data carry the staleness label |

**Local-first note:** the platform runs on the owner's machine (MA §10.5), so refresh jobs don't run while it's off. On startup, overdue jobs run in priority order (registry → sources → attention). Until they finish, Home shows "Catching up: last refresh 3 days ago" instead of presenting old data as current.

---

## 28. External Sources

### 28.1 Policy

- **Curated allowlist.** 15–25 sources in AIL.2, each with a source class, independence group, cadence, fetch method and ToS note. There is no open crawling and no following of links outside the allowlist.
- **APIs and feeds first.** Official APIs, RSS/Atom and structured endpoints are preferred. Pages without a feed are either fetched as a single page-snapshot diff (only where ToS allows) or entered manually ("paste a link" creates a Source Item from an allowlisted domain).
- **Blind spots are listed.** The Brief header and the Sources page name the lanes with fewer than 2 sources and the source classes missing per lane.
- **ToS review** is required per source before activation (owner decision **D2**).

### 28.2 Candidate sources (to verify at build time)

| Class | Candidates | Purpose |
|---|---|---|
| S1 | OpenRouter models API (already consumed by MA2) · Hugging Face Hub API · GitHub Releases API for a tracked-repo list (e.g., major open-weight model repos, agent frameworks, MCP SDKs) · official MCP Registry | New models, prices, context, releases |
| S2 | Official changelogs, news/blog feeds and pricing/docs pages of the providers you use or watch | Announcements, docs, pricing claims |
| S3 | arXiv API for a small set of categories + keyword filters matched to lanes | Research results |
| S4 | Independent evaluators with published methodology (e.g., LMArena, Artificial Analysis, Epoch AI, SWE-bench, METR reports) | Benchmark results |
| S5 | Hacker News (API), GitHub star/fork counts for tracked repos, Hugging Face downloads/likes, OpenRouter usage rankings where exposed | Attention and adoption |

Social platforms that need scraping or restricted APIs are **excluded** in V1 (§37).

### 28.3 Taxonomy (single controlled vocabulary)

`taxonomy_terms` holds all vocabularies:

| Vocabulary | Examples |
|---|---|
| `lane` | Models, Modalities, Agents, Tools & Interop, Knowledge, Serving & Cost, Quality & Safety, Ecosystem & Research |
| `topic` | The 29 requested topics (reasoning models, MCP, RAG, …), each with a `parent` lane |
| `capability` | `long_context_doc_analysis`, `browser_action`, `cheap_reasoning`, `screenshot_understanding`, `speech_transcription`, … |
| `platform_component` | `model_registry` (MA2), `tool_bus`, `evaluation` (MA6), `workflow_engine` (MA7), `model_router` (MA8), `tool_runtime` (MA9), `cost_governor`, `approval_service`, `flight_recorder`, `sandbox` |
| `track` | The ten learning tracks |
| `role` | Software Engineer, Code Reviewer, Research Agent, Planner, Security Reviewer |
| `provider` | Provider-level subjects for provider news |

Each `platform_component` term stores its MA phase, so queries like "relates to MA9" resolve deterministically.

---

## 29. Cost Controls

### 29.1 AIL's AI roles are platform Agents

All AIL model usage runs as **registered Agents** in a dedicated **AIL system project** (`projects.kind = 'system_ail'`):

| Agent | Purpose | Typical model tier |
|---|---|---|
| AIL Extractor | Claim / link / term extraction from prose source items | Low-cost, structured-output capable |
| AIL Writer | "Why it matters" sentences, Brief paragraphs, experiment conclusion drafts | Low- to mid-cost |
| AIL Professor | Teaching and explanation | Mid-tier (owner choice) |
| AIL Grader | Rubric grading | Mid-tier; stable pinned model for consistency |
| AIL Query Parser | Query → query-spec (§25 step 6) | Low-cost |

Consequences: versioned prompts (MA `prompt_versions`), full Flight Recorder coverage, per-call cost in `usage_events`, and budgets enforced by the existing Cost/Budget Governor. Model selection is manual (pinned) by default and can be changed like any Agent's `model_policy`. **Privacy requirement:** the Professor and Grader declare `privacy_requirement: no_training_retention` (MA §14.1), because they handle learner data.

AIL Agent invocations are lightweight Task Runs of an `ail_internal` task type with no sandbox (`sandbox_ref` null; no tools granted). Confirming this reuse is owner decision **D4**.

### 29.2 Budgets

| Budget | Scope (existing MA budget scopes) | Default proposal |
|---|---|---|
| AIL monthly total | `project` = AIL system project, monthly | Owner sets (D6) |
| Experiment cap | `task`/`execution` per Experiment | Estimate shown; Approval above threshold (e.g., > $2) |
| Professor per-turn cap | `execution` | Token cap per turn |

### 29.3 Cost-minimizing design choices

- **Deterministic first:** S1 sources, diffs, ordering, state and most search cost nothing in model calls.
- **Generate once, reuse:** each Development's "why it matters" is generated once and stored as an AI_EXPLANATION claim. Briefs reuse it and regenerate only when the claim set changes.
- **Extract only what's needed:** only S2/S3/S4 prose items from allowlisted sources in watched lanes are extracted, with a size cap per item (truncate plus flag).
- **Question bank before generation.**
- **Graceful degradation:** when the AIL budget is exhausted, all deterministic features keep working; LLM-dependent sections show "Explanation unavailable: AIL budget reached" rather than failing.
- **Visible spend:** Settings → Budget shows AIL spend by Agent (Extractor/Writer/Professor/Grader) and by experiments.

---

## 30. Privacy

| Area | Rule |
|---|---|
| **Learner data ownership** | Profile, plan, evidence, Professor sessions and decisions are **user-scoped**: visible only to that user (and org Owner/Admin only under explicit policy, owner decision D8). Never shared with other project members by default. |
| **Platform evidence opt-in** | AIL reads platform records only from projects the user explicitly opts in (`projects.ail_evidence_opt_in`). This is per project, revocable, and revoking stops future reads. Evidence already derived keeps a reference but shows "source no longer accessible". |
| **Minimum-necessary reads** | Observation and Professor intents load the minimum records needed (e.g., one routing decision), not bulk project data. |
| **No inference from unrelated content** | Interests are declared. AIL never scans task content, code, artifacts, prompts or conversation history to infer interests or skill. |
| **Task content** | Task text, code and artifacts from real projects enter AIL only through explicit user actions: "Add to eval set" (creates a copy in the AIL project) or "Explain this run" (single record, current session). |
| **Transcripts** | Professor sessions are stored as Agent Run records (Flight Recorder). Retention is configurable (default 90 days for message bodies; metadata and cost kept). They're excluded from any profile building. |
| **External providers** | AIL Agents that handle learner data declare `no_training_retention`. The context sent is the assembled minimum, never the full evidence history. |
| **Export & delete** | Full learner export (JSON/Markdown). "Delete my learning data" removes the profile, plan, evidence and transcripts; aggregated platform records are unaffected. |
| **External sources** | Outbound fetches carry no user data. Search queries are never sent to external sources (§25 step 7 fetches registered sources; it doesn't query them with user text). |

---

## 31. Security

| Threat | Mitigation |
|---|---|
| **Prompt injection via ingested content** (a blog post instructs the Extractor) | Extractor has **no tools** and schema-constrained output; source text is passed as quoted data; validators check claim-type ceilings, quote substrings and ID existence; the Extractor can't trigger fetches, experiments or decisions |
| **Injection reaching the Professor** | The Professor receives claims (validated, typed), not raw source text; it has no tools; any action it suggests requires a user click that goes through normal endpoints and RBAC |
| **Malicious fetch targets** | Allowlisted domains only; no redirects outside the allowlist; size/time limits; content stored as sanitized text; no JavaScript execution; fetchers run in the backend worker with egress limited to allowlisted hosts where the OS allows (full egress policy is MA9 and isn't required here) |
| **XSS through source content** | All ingested text is rendered as escaped text; links use `rel="noopener noreferrer"`; no HTML from sources is rendered |
| **Experiment spend abuse** | Experiments go through MA budgets and the Approval Service with an action fingerprint of the exact config (MA §24.4 #15) |
| **Configuration labs touching real systems** | Configuration labs run only in the AIL system project; router configs are dry-run only (never persisted as active policy) |
| **Credentials** | Source API keys are held server-side like provider keys (MA §20.1, ADR-5); never in the browser |
| **Cross-user leakage** | Learner tables are user-scoped with RBAC checks on every endpoint; RBAC/tenant tests (MA §29) extended to AIL endpoints |
| **Evidence tampering** | `learning_evidence` is append-only (no update/delete endpoints except full user deletion); disputes add records rather than editing |
| **MCP / third-party tool labs** | Deferred to MA9 precisely because they're code execution |

---

## 32. Data Model

### 32.1 Principles

- Owner rules from MA §10.5/§24.4 hold: **SQLite V1**, SQLAlchemy + Alembic, relational FKs and join tables for relationships, **JSON only for non-relational payloads/snapshots**.
- **Derive, don't cache:** Verification Level, Attention Level, Evidence Profile, Learner State, review-due and relevance are computed on read. Data volumes (one learner, thousands of claims, hundreds of concepts) make this cheap.
- **Reuse before adding.**

### 32.2 Existing tables: REUSED unchanged

| Table | AIL use |
|---|---|
| `models`, `providers`, `provider_models`, `model_capabilities` | Model Explorer facts, availability |
| `model_routing_decisions`, `router_policy_versions` | Observation labs, Professor explanations, Explorer link-out |
| `tasks`, `task_runs`, `agent_runs`, `model_calls`, `comparison_runs`, `comparison_candidates` | Eval set tasks, experiments, AIL agent invocations |
| `evaluations` | Experiment results, observation labs |
| `agents`, `agent_versions`, `prompt_versions` | AIL Extractor/Writer/Professor/Grader/Query Parser |
| `usage_events`, `budgets`, `budget_reservations` | AIL cost accounting and caps |
| `approvals` | Experiment spend approvals |
| `execution_events` | Professor/agent run history, transcripts |
| `audit_events` | Plan edits, opt-in changes, deletions |
| `job_queue` | All refresh and extraction jobs |
| `tools`, `tool_calls` | Observation labs on permission decisions |
| `source_snapshots` | Pinning eval tasks copied from repos |

### 32.3 Existing tables: EXTENDED

| Table | Change | Reason |
|---|---|---|
| `provider_model_snapshots` | Written on **every detected registry change** (not only when bound to a run); add `detected_at`, `change_kind` (`new`/`price`/`context`/`capability`/`status`) | Gives release history, price history and What's New with no new table |
| `projects` | Add `kind` (`standard`/`system_ail`), `ail_evidence_opt_in` (bool) | AIL system project; privacy opt-in |
| `task_runs` | Add `experiment_id` (nullable FK) | Groups runs under an Experiment |
| `tasks` | Add task type value `ail_internal` (enum extension) | AIL Agent invocations |
| `approvals` | Add operation type `ail_experiment_spend` (enum extension) | Experiment approvals |

### 32.4 NEW tables (28, introduced across slices: AIL.1 = 10 · AIL.2 = 12 · AIL.3 = 4 · AIL.4 = 2)

**Shared vocabulary (AIL.1)**

| Table | Key fields |
|---|---|
| `taxonomy_terms` | id, vocabulary, key, label, parent_id, ma_phase (nullable), active |

**Learning core (AIL.1)**

| Table | Key fields |
|---|---|
| `concepts` | id, slug, name, aliases (JSON list of strings; not relational), level, kind, is_core, freshness_days, current_version_id, status |
| `concept_versions` | id, concept_id, version, plain_definition, technical_explanation, examples_md, evidence_requirements (JSON rule spec), content_origin, reviewed_at, change_note, change_severity (`minor`/`material`) |
| `concept_relations` | id, from_concept_id, to_concept_id, relation_type, label |
| `concept_terms` | concept_id, term_id (tracks, platform components, lanes) |
| `learning_items` | id, concept_id, item_type, title, body_md, spec (JSON: question/options/answer key/observation query key/lab config), grading_mode, reviewed, version, est_minutes, requires_platform_capability, source_id (nullable, for external resources) |
| `learning_evidence` | id, user_id, concept_id, concept_version_id, evidence_type, learning_item_id, score, passed, grader, grader_confidence, question_origin, on_demo_data, ref_type, ref_id, created_at *(append-only)* |
| `learner_profiles` | user_id (PK), level, goal_text, depth, weekly_minutes, updated_at |
| `learner_interests` | id, user_id, term_id (nullable), concept_id (nullable), watch (bool) *(exactly one of term/concept)* |
| `learning_plan_items` | id, user_id, concept_id, position, state (`proposed`/`planned`/`skipped`/`done`/`rejected`), origin (`planner`/`user`/`radar`/`professor`), development_id (nullable), created_at |

**Radar (AIL.2)**

| Table | Key fields |
|---|---|
| `radar_sources` | id, name, url, source_class, independence_group, fetch_method, cadence_minutes, active, tos_note, last_success_at, last_error |
| `radar_items` | id, source_id, development_id (nullable), url, content_hash, title, published_at, retrieved_at, storage_ref, processing_state |
| `developments` | id, title, development_type, lane_term_id, announced_at, first_seen_at, merged_into_id |
| `development_concepts` | development_id, concept_id, state (`proposed`/`confirmed`/`rejected`), proposed_by |
| `development_models` | development_id, model_id |
| `development_terms` | development_id, term_id (topic/capability/platform_component/provider) |
| `claims` | id, claim_type, text, quote_span, development_id (nullable), model_id (nullable), source_item_id / experiment_id / evaluation_id / agent_run_id (exactly one), as_of, created_by, status, superseded_by_id |
| `claim_citations` | explanation_claim_id, cited_claim_id |
| `attention_samples` | id, development_id (nullable), model_id (nullable), source_id, metric, value, sampled_at |
| `triage_decisions` | id, user_id, development_id / model_id / opportunity_id (exactly one), decision, rationale, revisit_at, revisit_condition (JSON rule), decided_at, superseded_by_id |
| `brief_snapshots` | id, user_id, kind (`on_demand`/`daily`/`weekly`), period_start, period_end, generator_version, agent_run_id, created_at |
| `brief_entries` | id, brief_id, section, position, development_id / model_id / concept_id / experiment_id (nullable), reason_text |

**Lab (AIL.3)**

| Table | Key fields |
|---|---|
| `eval_sets` | id, name, role_term_id, description, created_by |
| `eval_set_versions` | id, eval_set_id, version, status (`draft`/`frozen`), frozen_at |
| `eval_set_version_tasks` | eval_set_version_id, task_id, position |
| `experiments` | id, user_id, experiment_type, hypothesis, eval_set_version_id (nullable), development_id (nullable), learning_item_id (nullable), config_snapshot (JSON), estimated_cost, budget_id, approval_id, status, conclusion_md, concluded_at |

**Opportunity Radar (AIL.4)**

| Table | Key fields |
|---|---|
| `interest_rules` | id, user_id, name, definition (JSON rule), experiment_template_key, active |
| `opportunities` | id, development_id, interest_rule_id, platform_term_id, hypothesis, cheapest_test, state, created_at |

### 32.5 DEFERRED tables (explicitly not created)

| Table | Why deferred |
|---|---|
| `learner_concept_states` (cache) | Computed on read; add only if profiling shows a need |
| `review_schedules` | Review-due is derived from evidence + interval rules |
| `search_embeddings` | FTS5 first; embeddings evaluated in AIL.4 (sqlite-vec or similar), owner decision |
| `investigation_notes` | An Investigation is a view over triage + experiments + claims; export as a Markdown artifact |
| Named `watchlists` | A single implicit watchlist is enough for one operator |
| `professor_sessions` / `professor_messages` | Agent Runs + execution_events cover this |
| `notifications` | Out of scope |
| Team/shared radar tables | Future |
| `learning_plans` (versioned plan header) | Plan items + audit events suffice |

### 32.6 Removed from the Radar v1 proposal

`radar_developments.capability_tags` (JSON) · `status_cached` · `attention_cached` · `radar_development_items` · `radar_concept_links` · `radar_platform_links` · `radar_brief_snapshots.item_ids` / `model_call_ids` (JSON) · `personal_eval_tasks` · `radar_opportunities.development_ids` (JSON). Each is replaced as described in §0.3.

---

## 33. Refresh Architecture

All background work runs as `job_queue` jobs (MA §24.4 #12) with the lease/fencing semantics already defined. Jobs are idempotent: `content_hash`, unique `(source_id, url, content_hash)` and upserts.

| Job | Trigger | Cadence | Idempotency / failure |
|---|---|---|---|
| `registry_refresh` (existing MA2) | Schedule + manual | Hourly | Existing; now also writes change snapshots |
| `registry_change_to_radar` | After `registry_refresh` | Per refresh | Attaches/creates Developments per §8.3 |
| `source_fetch:{source}` | Schedule + "Check sources now" | Per source (default 6–24 h) | Hash dedup; exponential backoff; `last_error` visible |
| `extract_item` | New prose item | Batched every 30 min | Retries once; then `processing_state = failed` (visible in What's New) |
| `attention_sample` | Schedule | Daily | Upsert per (subject, metric, day) |
| `trigger_evaluate` | After any refresh/extract | Per event batch | Pure function over current data |
| `explain_development` | Development's claim set changed and it's shown on Home/Brief | Lazy | Replaces the previous AI_EXPLANATION claim (superseded) |
| `brief_generate` | On demand (AIL.2); schedule (AIL.4) | — | One snapshot per (user, kind, period) |
| `experiment_run` | Approved experiment | — | Uses MA comparison execution; resumable |
| `opportunity_evaluate` | After trigger_evaluate (AIL.4) | Per batch | Cap enforced |

**Startup catch-up:** overdue jobs are enqueued in priority order. A single "AIL refresh status" view shows the last success, next run and errors per job type.

---

## 34. V1 Boundaries

**AIL V1 = slices AIL.1 + AIL.2 + AIL.3.**

### 34.1 In V1

- Concept Graph (~50 seeded concepts, 10 tracks), versioned concept content, reviewed question banks for core concepts
- Learner State (evidence ladder + CHANGED / SELF_REPORTED overlays), My AI Knowledge with evidence detail
- Deterministic planner, user-editable plan, Plan Change Proposals
- AI Professor: all intents in §16.2
- Knowledge checks (deterministic + labeled rubric grading)
- Labs: observation (record-grounded), configuration (AIL project), experiment, interpretation
- Model Explorer (all layers), Model Comparison, What's New
- Radar: curated sources, extraction with validators, Developments, typed claims, Verification Level, basic attention, triage, Watchlist with triggers
- AIL Home and on-demand Brief
- Personal Eval Sets and Experiment Lab (non-MA9 types)
- Ask AIL: deterministic parse + FTS5 + LLM query-spec fallback
- Budgets, privacy opt-in, export/delete

### 34.2 Not in V1

Opportunity Radar · retention/review prompts · scheduled briefs · semantic/embedding search · retrospective/calibration view · platform actions as evidence of use · anything MA9-gated · notifications · team features.

---

## 35. V1.1 (= AIL.4)

- **Opportunity Radar** with interest rules, cap and Investigation view
- **Retention/review** (§22), including REVIEW_DUE and REVIEW_FAILED overlays
- **Scheduled Briefs** (daily/weekly stored schedules; still no notifications)
- **Retrospective & calibration view:** of items you WATCHed, how many reached Tested by Us; of those, how many you adopted; how high-attention/Claimed items turned out *for you*; your triage decisions vs. later evidence
- **Semantic retrieval** behind FTS5 (if D15 approves)
- **Opted-in platform actions as "use"** for review-clock reset (e.g., editing a budget counts toward Cost Governance)
- **Stale-WATCH re-triage** and IGNORE resurfacing on a two-level verification jump
- Automated sentence→claim support check upgrade (if the AIL.2 validator proves insufficient)

---

## 36. Future (after V1.1; several are gated on MA9 or later)

| Item | Gate |
|---|---|
| MCP, tool-calling and computer-use labs; Tool Runtime experiments | **MA9** |
| Opt-in "auto-test new models in watched lanes on my eval set", with a monthly cap and approval | V1.1 experience + owner appetite for spend |
| Deeper research workflow (paper reading sessions with the Professor, reproduction notes) | — |
| Concept pack import/export; shared curricula for a team | Multi-user (MA10 era) |
| Team Radar with shared triage and per-member learning privacy | Multi-user |
| Notifications (email/push) for fired triggers and briefs | Explicitly deferred |
| Fine-tuning / distillation labs | Needs training infrastructure (MA NG3 excludes it) |
| Local-model labs (quantization trade-offs on local endpoints) | Local provider adapter maturity |
| Richer adoption metrics | Availability of reliable public metrics |

---

## 37. Explicitly Rejected / Deferred Ideas

| Idea | Verdict | Reason |
|---|---|---|
| Universal importance score for Developments | **Rejected** | Hides evidence; violates P4 |
| Universal model score / leaderboard | **Rejected** | Every result is slice-bound (P7); MA §17.4 already forbids hard-coded rankings |
| Marking concepts "learned" on page view or time spent | **Rejected** | Exposure isn't knowledge (P1) |
| Bare progress percentages | **Rejected** | Only fractions with explicit denominators |
| Automatic curriculum or plan modification | **Rejected** | Proposals only (P6) |
| Radar claims feeding the Model Router | **Rejected** | Keeps hype out of automated decisions (§3.2) |
| Auto-creating concepts from news | **Rejected** | The graph is curated; candidates go to an owner queue |
| Gamification (streaks, XP, badges) | **Rejected** | Optimizes engagement, not capability |
| Inferring interests or skill from task content, code or chat history | **Rejected** | Privacy boundary (§30) |
| LLM answering "what changed" from its own knowledge | **Rejected** | Every external statement must be attributable |
| Open web crawling; X/Twitter or other scraped social ingestion | **Rejected for V1** | ToS, noise, maintenance; the blind spot is declared instead |
| Full spaced-repetition system (SM-2) | **Rejected** | Not justified for ~100 concepts; lightweight review instead (§22) |
| Notifications | **Deferred** | Owner instruction |
| Embedding every item / vector DB | **Deferred to AIL.4** | FTS5 + structured queries cover the V1 query set |
| Autonomous experiments on every new model | **Deferred (Future, opt-in)** | Spend control and relevance; human-initiated first |
| Separate `learner_concept_states`, `review_schedules`, `investigation_notes` tables | **Deferred** | Derived views suffice |
| Building MCP/tool runtime inside AIL | **Rejected** | Belongs to MA9 |
| A separate "AIL model page" in the MA registry UI | **Rejected** | One knowledge view (Explorer), one operational view (Registry) |

---

## 38. AIL.1–AIL.4 Implementation Sequence

### 38.1 Overview

| Slice | Name | User-visible value | Platform prerequisites |
|---|---|---|---|
| **AIL.1** | **Learn with Evidence & Know the Models** | A real, evidence-based learning system with a Professor, plus a Model Explorer and a registry change log | MA2 (registry), MA3 (agent runs, budgets, job queue, routing decisions from the static scorer) |
| **AIL.2** | **Radar & Today** | "What should I know today?" answered with typed, verified, relevance-explained developments tied to learning | AIL.1 |
| **AIL.3** | **Learn by Doing: Personal Lab** | Test models on your own work; hands-on labs; full DEMONSTRATED for operational concepts | AIL.1; **MA5 + MA6**; MA8 optional (dry-run labs) |
| **AIL.4** | **Stay Ahead Loop** (V1.1) | Opportunities, retention, scheduled briefs, calibration: capability that compounds over months | AIL.2 + AIL.3 |

AIL slices can interleave with MA7/MA8 work. **None requires MA9.**

### 38.2 AIL.1: Learn with Evidence & Know the Models

1. `taxonomy_terms` + seed vocabularies (lanes, topics, tracks, roles, platform components with MA phases)
2. AIL system project + AIL Agents (Professor, Grader) with prompt versions and budgets
3. Concept Graph tables + seed ~50 concepts (content labeled by origin) + prerequisite DAG validator
4. Learning Items + reviewed question banks for **core concepts** (target ≥15 concepts with full banks)
5. Learning Evidence + Learner State Service (ladder + CHANGED/SELF_REPORTED; CHANGED from concept versions only until AIL.2)
6. Knowledge check runner (deterministic) + rubric grading with provisional/confirm flow
7. Observation tasks with record-grounded questions (routing decisions, evaluations if present, tool-call permission decisions) + seeded demo dataset
8. Learner profile, interests, deterministic planner, plan editing
9. Professor intents: next, teach, check, exercise, diagnose, practiced, platform_lens, explain_routing_decision
10. My AI Knowledge page
11. `provider_model_snapshots` extension → What's New (registry events) + Model Explorer v1 (facts, history, availability, our usage, reliability, evaluations, router link-out)
12. Ask AIL v1: deterministic parse + FTS5 over concepts and models

### 38.3 AIL.2: Radar & Today

1. Sources (allowlist, classes, independence groups) + fetchers + `radar_items`
2. Extractor Agent + validators (ceiling, quote substring, ID existence, provider-benchmark rule)
3. Developments, claims, citations, Development ↔ concept/model/term links (proposed → confirmed)
4. Verification Level, Evidence Profile, templated honesty sentences
5. Attention samples (a few S5 metrics) + Attention Level
6. Triage decisions + Watchlist + triggers
7. Plan Change Proposals from the Radar; CHANGED overlay from confirmed Development links
8. Grounded "why it matters" (Writer Agent + validator)
9. AIL Home + on-demand Brief + Brief archive
10. Model Explorer claim layers (provider claims, benchmarks, research, attention)
11. Professor intents: explain_development, explain_relevance, whats_changed
12. Ask AIL v2: developments/claims in FTS + LLM query-spec fallback + "Check sources now"

### 38.4 AIL.3: Learn by Doing: Personal Lab

1. Eval sets + versions + "Add to eval set" (copy from project with source snapshot)
2. Experiments: estimate → approve → run (MA5 comparison) → evaluate (MA6) → conclude
3. Experiment types: eval-set comparison, prompt-version comparison, variance run, observation study, routing dry-run (if MA8)
4. Result slice headers; PLATFORM_OBSERVATION claims from conclusions; "Tested by Us" Verification Level
5. Model Comparison view (layered rules)
6. Configuration labs (AIL project) + experiment labs + interpretation labs; lab ↔ experiment evidence linking; "count this as practice" claiming
7. Model Explorer "Our experiments" panel; Home "Experiments to try" section

### 38.5 AIL.4: Stay Ahead Loop

1. Interest rules + Opportunities + cap + Investigation view/export
2. Retention/review rules, overlays and Home/Brief integration
3. Scheduled briefs (daily/weekly)
4. Retrospective & calibration view
5. Opted-in platform actions as use evidence
6. Semantic retrieval (if approved)
7. Stale WATCH / IGNORE resurfacing

---

## 39. Acceptance Criteria per Slice

### AIL.1

1. A concept can't reach UNDERSTOOD or above without a recorded, graded evidence item; opening lessons produces at most EXPOSED. *(Automated test.)*
2. DEMONSTRATED is impossible on AI-graded evidence alone. *(Automated test.)*
3. Every concept state on My AI Knowledge opens an evidence list that links to the underlying attempt or record.
4. Track summaries show counts with their denominators; no bare percentages appear anywhere.
5. Adding a prerequisite edge that creates a cycle is rejected.
6. The planner generates a plan from goals, depth and time; user edits persist across re-planning; re-plans are shown as a diff to accept.
7. Record-grounded observation questions are generated from real routing decisions (or labeled demo data) and graded without an LLM.
8. The Professor answers "Explain this routing decision #N" using only that decision's record, cites its ID, and refuses to describe records not in its context.
9. The Professor never reads task content, code or artifacts unless the user attaches a specific record in the session. *(Test on context assembly.)*
10. The Model Explorer shows price history from change snapshots with as-of dates, and our usage/reliability only from opted-in projects.
11. What's New lists new-model and price-change events within one refresh cycle of the registry change.
12. All AIL model calls appear in the Flight Recorder and `usage_events` under the AIL project, and stop at the AIL budget.

### AIL.2

1. A claim whose type exceeds its source's class ceiling is rejected. *(Test per class.)*
2. A prose-derived claim whose quote isn't an exact substring of the stored item is rejected.
3. A provider-published benchmark number is stored as PROVIDER_CLAIM.
4. Attention changes never change Verification Level. *(Property test.)*
5. Corroboration counts distinct independence groups, not items.
6. Every AI_EXPLANATION sentence with a number, date, name or comparison carries a resolvable citation; unsupported sentences are dropped.
7. Every Home and Brief item shows a reason line; empty sections show an explicit "nothing met the bar" line.
8. A Brief reopened later shows the identical entries and reasons.
9. WATCH can't be saved without a revisit trigger; triggers fire deterministically after refresh.
10. Radar-originated concepts reach the plan only via an accepted Plan Change Proposal.
11. "Which model releases relate to MA9?" and "Show me things I watched but never tested" run as structured queries with no LLM call.
12. With the AIL budget exhausted, Home still renders all deterministic sections.

### AIL.3

1. An eval set version is immutable once an experiment references it; editing creates a new version.
2. Every experiment result displays the full slice header (§13.3), and every stored result can reproduce it.
3. Experiments over the approval threshold require an approval bound to the exact config fingerprint.
4. Model Comparison never places results from different eval set versions or benchmark versions in the same column.
5. A concluded experiment creates PLATFORM_OBSERVATION claims and moves linked Developments/Models to "Tested by Us".
6. A lab-linked experiment writes Learning Evidence for its concept after completion + interpretation.
7. Configuration labs create objects only in the AIL system project; router configs are never persisted as active policy.
8. No AIL.3 feature requires MA9; MA9-gated items display as "Available after MA9".

### AIL.4

1. No Opportunity exists without a matching active interest rule and Verification ≥ Documented; the open cap is enforced.
2. Review prompts appear at most twice per week, only for eligible concepts, and never as notifications.
3. A failed review adds REVIEW_FAILED and re-plans the concept without deleting prior evidence.
4. The calibration view reproduces its numbers from triage decisions + experiments + claims (auditable query).
5. Scheduled briefs generate from stored schedules and are stored as snapshots.

---

## 40. Risks and Open Questions

| # | Risk / question | Impact | Mitigation / next step |
|---|---|---|---|
| R1 | **Content authoring burden** (50 concepts × explanations × question banks × labs) | AIL.1 slips or ships thin | Seed with AI-drafted content labeled `ai_drafted_unreviewed`; full reviewed banks for ~15 core concepts first; expand per slice |
| R2 | **AI grading reliability** | Wrong evidence, loss of trust | Deterministic-evidence requirement for DEMONSTRATED; provisional low-confidence grades; disputes; pinned grader model |
| R3 | **Little platform data early** | Observation labs and "our evidence" are empty | Labeled demo dataset; lab alternatives; Explorer shows "insufficient data" honestly |
| R4 | **Small-n Personal Eval results overinterpreted** | Bad model decisions | Mandatory slice header, "directional only" label, variance runs, no aggregate score |
| R5 | **Source maintenance and ToS** | Broken or non-compliant ingestion | APIs/feeds first; per-source health; ToS review gate; manual-entry fallback |
| R6 | **Extractor errors / injection** | Wrong claims | Validators, no tools, quote verification, dispute flag, claim status |
| R7 | **Pressure to let Radar claims influence routing** | Hype enters automation | Hard boundary §3.2 as an ADR (proposed ADR-AIL-1) |
| R8 | **MA8 dry-run needs to accept inline configs** | Routing policy lab blocked | Change request to MA8; alternative requirement set in the meantime |
| R9 | **Local-first freshness** (machine off) | Stale "today" view | Catch-up jobs; explicit staleness banners |
| R10 | **LLM cost creep** (Professor usage) | Budget overrun | Per-turn caps; AIL monthly budget; deterministic intents; cached explanations |
| R11 | **Concept graph drift** vs. fast-moving AI | Outdated teaching | Freshness horizons; CHANGED overlays; owner review queue fed by confirmed Developments |
| R12 | **Scope creep** (AIL becomes a second product) | Delays MA roadmap | Four slices only; AIL depends on MA, never the reverse; no MA9 work in AIL |
| Q1 | Do any original AIL Sections 1–39 elements conflict with this spec? | Rework | Owner to confirm this document supersedes them |
| Q2 | Should demo-data evidence ever count toward DEMONSTRATED? | Evidence validity | D5 |
| Q3 | Should MA8 include experiment-tagged evaluations in routing history? | Router bias | D7 |
| Q4 | Is a single learner per installation the only V1 case? | Privacy model scope | Assumed yes (single-user V1, consistent with MA local-first); multi-user privacy already user-scoped |

---

## 41. Owner Decisions Required Before Implementation

| # | Decision | Proposed default |
|---|---|---|
| D1 | Approve the Learner State model: evidence ladder (NOT_STARTED → EXPOSED → UNDERSTOOD → PRACTICED → DEMONSTRATED) + overlays (REVIEW_DUE, CHANGED, REVIEW_FAILED, SELF_REPORTED) and the per-kind evidence requirements | Approve as specified |
| D2 | Approve the initial source list (15–25), their classes and a per-source ToS check | Owner review of the §28.2 candidates |
| D3 | Concept content policy: must seed content be owner-reviewed before activation? | Allow `ai_drafted_unreviewed` with a visible label; review core concepts first |
| D4 | AIL's AI roles as registered Agents in a `system_ail` project with `ail_internal` Task Runs | Approve (maximum reuse) |
| D5 | Can demo-data evidence count toward DEMONSTRATED? | No: PRACTICED only |
| D6 | AIL monthly budget and experiment approval threshold | Owner sets values (e.g., threshold $2/experiment) |
| D7 | Should MA8 routing history include experiment-tagged evaluations? | Exclude by default |
| D8 | Can org Owner/Admin see a user's learner data? | No, unless the user shares it |
| D9 | Approve the seed concept list (§15.3), the ten tracks and the core-concept flags | Approve with edits |
| D10 | First Personal Eval Sets: roles and task count | Code Reviewer + Software Engineer, 5–6 tasks each |
| D11 | Models for the Professor, Grader, Extractor, Writer and Query Parser (must meet `no_training_retention` where learner data is involved) | Owner choice from the registry |
| D12 | Professor transcript retention | 90 days for message bodies |
| D13 | Raise the MA8 change request: dry-run accepts an inline, non-persisted scoring config | Approve |
| D14 | Slice timing relative to MA roadmap: AIL.1 after MA3; AIL.3 after MA6 | Approve |
| D15 | Semantic retrieval in AIL.4 (SQLite vector extension) or FTS5-only | Decide at AIL.4 start based on query logs |
| D16 | Confirm this document supersedes `ail-ai-radar-design-v1.md` and any earlier AIL Sections 1–39 | Approve |

---

## One-Page Product Map

```
                                   ┌───────────────────────────────────────────┐
  EXTERNAL WORLD                   │                 AI RADAR                   │
  S1 catalogs ─┐                   │ Sources → Items → Developments → CLAIMS    │
  S2 providers ├──(allowlisted)───>│ 7 claim types · Verification Level         │
  S3 research  │                   │ Attention (separate axis) · Triage         │
  S4 evaluators│                   └───┬───────────┬──────────────┬─────────────┘
  S5 community ┘                       │ links     │ links        │ WATCH / EXPERIMENT / LEARN
                                       ▼           ▼              ▼
┌──────────────────────────┐   ┌───────────────┐   ┌─────────────────────────────────┐
│          MODELS          │◄──│  CONCEPTS     │   │           EXPERIMENTS           │
│ Registry facts (MA2)     │   │ Concept Graph │   │ Personal Eval Sets (versioned)  │
│ Price/release history    │   │ prereqs·part_of│  │ → MA5 Comparison Runs           │
│ Claims by type           │   │ ·related      │   │ → MA6 Evaluation (objective 1st)│
│ Our usage & reliability  │   │ versioned     │   │ → slice-bound results           │
│ Our evals & experiments ◄┼───┼───────────────┼───┤ → PLATFORM_OBSERVATION claims ──┼──► Radar
│ ⇢ link: MA8 "why chosen" │   │ Tracks = views│   │   ("Tested by Us")              │    & Models
└──────────▲───────────────┘   └──┬─────────▲──┘   └──────────────┬──────────────────┘
           │                      │         │ Plan Change          │ labs create experiments
           │                      ▼         │ Proposals            ▼
           │               ┌──────────────────────┐     ┌──────────────────────────┐
           │               │     CURRICULUM /      │     │   LABS & KNOWLEDGE CHECKS│
           │               │     LEARNING PLAN     │────►│ observe real records     │
           │               │ deterministic planner │     │ configure (AIL project)  │
           │               │ user-editable         │     │ experiment · interpret   │
           │               └──────────▲───────────┘     └────────────┬─────────────┘
           │                          │ next / gaps                  │ graded evidence only
           │               ┌──────────┴───────────┐     ┌────────────▼─────────────┐
           │               │     AI PROFESSOR      │◄───│      LEARNER STATE        │
           └───────────────┤ teach · explain ·     │    │ EXPOSED→UNDERSTOOD→       │
                           │ diagnose · test ·     │───►│ PRACTICED→DEMONSTRATED    │
                           │ provenance voice      │    │ + CHANGED / REVIEW_DUE    │
                           │ strict context        │    │ "why AIL believes this"   │
                           └──────────▲───────────┘    └──────────────────────────┘
                                      │ read-only, opt-in, minimum-necessary
┌─────────────────────────────────────┴──────────────────────────────────────────────┐
│ PLATFORM EVIDENCE (MA): routing decisions (MA8) · evaluations · comparison runs ·    │
│ usage/cost · reliability · tool permission decisions · Flight Recorder               │
└──────────────────────────────────────────────────────────────────────────────────────┘

TODAY / BRIEF = Radar changes + Model changes + Platform relevance + Learning relevance
                + Learn next + Experiments to try + Progress, each item with a visible reason.
HUMAN DECIDES at every arrow that changes something: plan, triage, experiment spend, platform config.
```

**How the map makes you more capable over time.** The Radar tells you what changed and how well it's evidenced. Concepts tell you what you need to understand it. The Professor teaches it at your level, with provenance. The Curriculum puts it in order. Labs and Experiments make you do it on your own platform and your own tasks. Learner State records only what you can show you know. Platform Evidence and Personal Eval results feed back into the Models and the Radar as your own evidence. Over months, the calibration view (AIL.4) shows whether your judgment about AI developments is getting better.
