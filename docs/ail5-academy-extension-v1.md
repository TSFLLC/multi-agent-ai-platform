# AIL.5 — AI Academy & Project-Based Learning

**Product & Architecture Extension v1.0 (design only, for architecture review)**

| | |
|---|---|
| Status | DRAFT FOR REVIEW. Not approved for implementation. |
| Owner | Serge Tchuenteu |
| Date | 2026-09-22 |
| Extends | `claude/ail-unified-spec-v1.md` (canonical AIL spec, "AIL §n") |
| Depends on | `claude/multi-agent-platform-spec-v1.1.md` ("MA §n") |
| Non-scope | No code, migrations, commits, merges or deployments. AIL.1–AIL.4 aren't modified by this document. Every change they would need is listed as a **CANONICAL AIL CHANGE REQUEST** (§X), and every platform change as a **PLATFORM (MA) CHANGE REQUEST**. |

> **Reading order.** Sections A–W follow the requested deliverable order. **§X** lists every conflict with the canonical spec and every change request. **§Y** is a one-page summary map.

---

## A. Product Objective

### A.1 Goal

AIL.5 adds a structured, **project-based academy** to AIL, so a **complete beginner** can go in about 30 days from

> "I don't really understand AI"

to

> "I understand the foundations, I have used AI tools, I have built several small things, and I have completed a real beginner AI project that I can demonstrate."

It does this **without creating a second learning engine.** AIL.5 is a *program layer* and a *project layer* on top of the existing Concept Graph, Learning Items, Learning Evidence, Learner State, Learning Plan and Professor (AIL §15–§22), plus the Personal Lab (AIL §13–§14) and the platform's execution and evaluation machinery.

### A.2 Philosophy

```
LEARN ──> PRACTICE ──> BUILD ──> TEST ──> EXPLAIN ──> IMPROVE ──> DEMONSTRATE
lesson    exercise     project   objective  explain-back  change +     portfolio +
+ check   + hints      milestone checks     (rubric)      re-test      completion report
```

### A.3 Four teaching roles, one architecture

| Role | What it does | How it's implemented (reuse first) |
|---|---|---|
| **Professor** | Teaches concepts, explains, answers questions | Existing AIL Professor Agent (AIL §16) |
| **Coach** | Decides what to work on today; adjusts pace; catch-up | **Deterministic**: the existing planner (AIL §17.3) extended with a program schedule. Not an LLM |
| **Lab Instructor** | Gives hands-on exercises, experiments and checks | Existing Learning Items, Labs and Experiment Lab (AIL §19, §20, §14) |
| **Project Mentor** | Guides projects: milestones, hints, reviews, error explanations, explain-back | A **mode of the Professor Agent** with its own prompt version and a strict hint policy (§G) |
| *(Assessor)* | Grades free-text explanations | Existing, **separate** AIL Grader Agent (AIL §19.3). The one who helps is never the one who grades |

### A.4 Success definition (measurable)

A learner who finishes the 30-day **AI Foundations Builder** program has:

1. ≥ 12 concepts at DEMONSTRATED and all program-required concepts at ≥ PRACTICED (defaults; owner decision W1)
2. ≥ 4 completed project-ladder items across ≥ 3 ladder levels, each with platform-verified checks
3. ≥ 3 controlled experiments (AIL.3) run and interpreted
4. One capstone meeting its own brief's evaluation criteria, documented, with an explain-back and a modification challenge passed
5. A **portfolio page** and an **evidence-backed completion report** where every capability statement links to evidence

---

## B. Beginner Experience

### B.1 Beginner Mode

Beginner Mode is a per-learner setting (`learner_profiles.experience_mode = beginner`; **CR-5**), chosen at onboarding and switchable at any time. It changes **presentation and defaults, not the architecture.**

| Aspect | Beginner Mode behavior |
|---|---|
| **Navigation** | A simplified nav: *Today · My Program · Projects · Ask the Professor · My Progress · Glossary*. Radar, Models, Lab and Watchlist are available under "More" |
| **Progressive disclosure** | Routing policies, provider architecture, evaluation internals, orchestration and token-accounting internals are hidden **unless a lesson or project step references them**. When one does, it appears inline as a "just enough" card with a "show more" link |
| **Language** | Uses `concept_versions.plain_definition` first; technical explanations sit behind "Show the technical version" |
| **Explain Like I'm New** | A Professor intent (`eli_new`) constrained to plain definitions, one analogy and one everyday example; no jargon unless it's linked to the Glossary |
| **Glossary** | Generated from the Concept Graph (names, aliases, plain definitions). **No separate glossary store** |
| **"Why am I learning this?"** | Deterministic: shows the program item's stated purpose, the project milestone that needs it, and the downstream concepts it unlocks (graph traversal) |
| **"Show me an example"** | Returns the concept's example first, then a platform example (a real run from the learner's *own* projects when available) |
| **"I'm stuck"** | Enters the **Hint Ladder** (§H) for the current step, never a straight solution |
| **"Explain this error"** | Explains what the error message *means* and where to look, without fixing it. Recorded as assistance level H2 (§H) |
| **Challenge mode** | Optional per project: fewer scaffolds, hints limited to H1–H2, and a "challenge" tag on the evidence |
| **Model choice** | Defaults to one pre-selected, low-cost model for learner apps; model selection is taught explicitly in Week 2 |
| **Radar** | A weekly "One AI development, explained" card: Verification ≥ Documented only, used as a claim-literacy exercise (reuses AIL.2) |
| **Numbers** | Cost is shown in plain currency ("this test cost $0.004"); token internals appear only when the lesson is about tokens |

### B.2 Onboarding (5 minutes)

1. Goal (free text + choice: career switch / use AI at work / build things / curiosity)
2. Coding comfort: *none · some (can edit code) · comfortable*. This determines the default **build path** (§C.2)
3. Time per day: 45 / 60 / 90 minutes, and days per week
4. Interests (career areas, domains) → used to pick project flavors and capstone proposals
5. An optional 6-question placement check. Passing items write ordinary Learning Evidence and may skip Week 1 items (the Coach proposes, the learner accepts)

### B.3 A beginner's day (shape)

```
TODAY — Day 9 of 30 · ~70 min
 1. Warm-up (5 min)       1 review question from yesterday
 2. Learn (15 min)        Few-shot prompting — short lesson + "Show me an example"
 3. Practice (20 min)     Guided exercise: rewrite 3 prompts with examples
 4. Experiment (20 min)   Zero-shot vs few-shot on your 10 test inputs (est. $0.02)
 5. Explain (10 min)      "In 3 sentences, why did few-shot help (or not)?"
 Why today: Week 2 project needs consistent output formats → this unlocks "Structured Output" tomorrow.
```

---

## C. 30-Day Curriculum: "AI Foundations Builder"

### C.1 Concept order (reviewed against the Concept Graph)

The suggested topic list is almost right. Three reorderings follow from prerequisite logic:

1. **Experience before vocabulary.** Day 1 starts with using AI and noticing what it does. Tokens come on Day 2, once the learner has something to attach them to.
2. **Temperature belongs with prompting, not theory.** It's taught through a variance experiment (Day 4), right after the first prompts.
3. **Evaluating AI answers moves into Week 1 and then recurs.** It's the foundation of claim literacy (AIL §10.5), and every weekly project depends on it.

The program uses **existing seed concepts** where they exist (AIL §15.3) plus **14 beginner concepts** that the canonical seed lacks (**CR-1**):

| # | Concept | Status | Kind | Key prerequisites |
|---|---|---|---|---|
| 1 | What AI Is and Isn't (rules vs. learning; narrow vs. general) | **new** | definitional | — |
| 2 | Generative AI & LLMs (next-token prediction) | **new** | mechanism | 1 |
| 3 | Models vs. Providers vs. Applications | **new** | definitional | 2 |
| 4 | Tokens & Tokenization | existing | definitional | 2 |
| 5 | Context Windows | existing | definitional | 4 |
| 6 | Prompting | existing | operational | 2 |
| 7 | Inference: Temperature & Nondeterminism | existing (Inference) | mechanism | 4, 6 |
| 8 | Hallucination & Grounding | existing | mechanism | 7 |
| 9 | Evaluating AI Claims / Answers | existing | operational | 8 |
| 10 | Privacy & Responsible AI Use | **new** | operational | 3 |
| 11 | Prompt Structure (role, task, context, format, examples) | **new** | skill | 6 |
| 12 | System vs. User Instructions | existing (System Instructions) | mechanism | 11 |
| 13 | Few-shot Prompting | **new** | skill | 11 |
| 14 | JSON Basics | **new** | skill | — |
| 15 | Structured Output | existing | mechanism | 12, 14 |
| 16 | Iterating & Debugging AI Outputs | **new** | skill | 9, 11 |
| 17 | Model Pricing & Token Economics | existing | operational | 4 |
| 18 | Choosing a Model (quality/cost/speed trade-offs) | **new** | operational | 3, 17, 9 |
| 19 | Evaluation (objective first) | existing | operational | 9 |
| 20 | How Apps Call Models (API requests, keys, limits) | **new** | mechanism | 3, 14 |
| 21 | Python for AI Basics (variables, functions, calling an API) | **new** (code path only) | skill | 14, 20 |
| 22 | Prompt Templates | **new** | skill | 11, 20 |
| 23 | Conversation Memory in Chat Apps | **new** | mechanism | 5, 20 |
| 24 | Embeddings | existing | mechanism | 4 |
| 25 | RAG | existing | architectural | 24, 8, 5 |
| 26 | Tool Calling | existing | mechanism | 15 |
| 27 | Agents | existing | architectural | 26 |
| 28 | Workflows (multi-step, bounded) | existing (Workflow/DAG, beginner framing) | architectural | 27 |
| 29 | Problem Framing & Requirements for AI Projects | **new** | skill | 19 |

The new kind **`skill`** (**CR-1**) covers "can do" concepts (JSON, prompt structure, Python basics). DEMONSTRATED for a skill needs a completed exercise or milestone with objective checks, done at low assistance, plus a short explanation (§I.4).

### C.2 Two build paths (one program)

| Path | For | How builds run | Available |
|---|---|---|---|
| **No-code path** (default) | Coding comfort "none/some" | The learner's "app" is an **Agent** they author (instructions, output schema, examples) plus, in Week 3, a **config-defined Workflow** (MA7). Runs execute on the platform; tests run as an **App Test Set**, which is a Personal Eval Set (AIL §13) | **Pre-MA9** |
| **Code path** (optional) | "Comfortable", or learners who opt in | Python on the learner's own machine with **their own** provider key; submits code + outputs; objective tests run in the platform sandbox where supported (§O) | Pre-MA9 (limited verification); fully verified post-MA9 |

Both paths assess the **same concepts**. The code path additionally covers #21 (Python for AI Basics).

### C.3 Weekly design

#### WEEK 1 — Understand AI (Days 1–7)

| Item | Content |
|---|---|
| **Learning objectives** | Explain what an LLM does in plain language · distinguish model/provider/app · estimate tokens and cost · show that outputs vary and why · recognize and reduce hallucination with grounding · judge an AI answer · state basic privacy rules |
| **Concepts** | 1–10 |
| **Lessons** | 10 short lessons (8–15 min), each ending with one check-for-understanding question |
| **Exercises** | Classify 5 AI answers (right/wrong/unsupported) · tokenize 3 texts and estimate cost · identify model/provider/app for 6 products · spot the hallucination in 4 answers |
| **Builds / experiments** | Micro-experiment: temperature variance (same prompt ×5 at 0 vs 1) · micro-experiment: grounded vs. ungrounded answers on 5 questions |
| **Knowledge checks** | One per concept (3–5 items), plus a Day 7 weekly check (8 items, mixed) |
| **Project** | **P1 "Trustworthy Explainer"** (Ladder L2, no-code): an assistant that explains a topic simply **using only provided notes** and must say "I'm not sure" otherwise. 8-case App Test Set (5 answerable, 3 trick questions). Objective checks: refusal on trick cases, required-phrase and length checks |
| **Evidence collected** | Checks (deterministic) · 2 experiment results · P1 test results (platform-verified) · P1 explain-back (rubric) · assistance levels |
| **Completion criteria** | Concepts 1–10 ≥ UNDERSTOOD; ≥ 4 at DEMONSTRATED (definitional ones); P1 submitted with ≥ 6/8 tests passing *or* a documented failure analysis |

#### WEEK 2 — Use AI Effectively (Days 8–14)

| Item | Content |
|---|---|
| **Learning objectives** | Write structured prompts · use system instructions and few-shot examples · get reliable JSON · debug bad outputs methodically · choose a model with evidence (quality, cost, speed) · design test cases |
| **Concepts** | 11–19 (and #20 as a preview) |
| **Lessons** | 8 lessons |
| **Exercises** | Rewrite 3 weak prompts · write a JSON schema for 3 real-world objects · debug 3 broken prompts using a failure checklist |
| **Controlled experiments** (not just being told) | **E1** zero-shot vs. few-shot (10 inputs, objective field checks) · **E2** instructions in system vs. user message (10 inputs) · **E3** 3-model comparison on the learner's own test set (quality/cost/latency, AIL.3 Experiment Lab) |
| **Knowledge checks** | Per concept + weekly check; one numeric cost question ("1,000 documents at X tokens each on model A vs. B?") |
| **Project** | **P2 "Structured Extractor"** (L2): turn messy text (job postings, receipts, meeting notes; the learner picks one) into validated JSON. 10-case App Test Set with expected field values. Objective checks: schema validity and field accuracy. The learner **chooses a model using E3 evidence** and writes a 5-sentence justification including cost per 1,000 items |
| **Evidence collected** | Experiment results + interpretations · P2 field-accuracy results · model-choice justification (rubric) · debugging log (§I) |
| **Completion criteria** | Concepts 11–19 ≥ UNDERSTOOD; ≥ 5 more DEMONSTRATED; P2 ≥ 80% field accuracy *or* a documented improvement attempt with before/after results |

#### WEEK 3 — Build with AI (Days 15–21)

| Item | Content |
|---|---|
| **Learning objectives** | Explain how applications call models · use templates · manage conversation memory · explain embeddings and retrieval · build a retrieve-then-answer app with citations · explain tools, agents and bounded workflows · evaluate an app |
| **Concepts** | 20–28 (21 on the code path only) |
| **Lessons** | 8 lessons, each paired with a small build step |
| **Exercises** | Read a real request/response record from their own P2 runs (observation task, AIL §20.3) · similarity-sorting exercise with precomputed embeddings · design a tool schema for a scenario |
| **Builds** | Templated agent (5 inputs) · multi-turn assistant with a summary memory; experiment: tokens per turn as history grows |
| **Knowledge checks** | Per concept + weekly check, including a **record-grounded** question on their own runs |
| **Project** | **P3 "Document Q&A Assistant"** (L3 guided project): given a document split into labeled sections, a 2-step app (**select relevant sections → answer with section citations**). No-code: an MA7 workflow of two Agent nodes (fallback: one agent with a structured two-part output if MA7 isn't available). Code path: Python with real embeddings (learner's own key). 8-question test set, including 2 unanswerable. Objective checks: cited section IDs match expected; refusal on unanswerable questions |
| **Evidence collected** | Observation answers (deterministic, from their own records) · build test results · citation accuracy · explain-back of the pipeline · assistance levels |
| **Completion criteria** | Concepts 20–28 ≥ UNDERSTOOD; RAG and Prompt Templates ≥ PRACTICED; P3 citation accuracy ≥ 6/8 *or* a documented failure analysis + one improvement iteration |

> *Honesty note for the no-code path:* "select relevant sections" is LLM-based selection inside the context window, not vector search. The lesson says so explicitly and uses the similarity exercise to teach real embedding retrieval. Real vector retrieval on the platform is post-MA9 (§O).

#### WEEK 4 — Build a Real Project (Days 22–30)

| Item | Content |
|---|---|
| **Learning objectives** | Frame a problem · write requirements and success criteria · design and select a model with evidence · build in milestones · test, evaluate, improve · document limits and cost · explain and adapt the system |
| **Concepts** | 29 + reinforcement of 8, 9, 15, 16, 18, 19 and 25 or 27 (depending on the capstone) |
| **Lessons** | 3 short lessons (problem framing, writing a test set, writing a README) |
| **Project** | **Capstone** (L4), chosen from 3–5 proposals (§J) |
| **Evidence collected** | Brief acceptance · requirements + test set (rubric + completeness checks) · model-selection experiment · milestone checks · evaluation run · before/after improvement · README · modification challenge (verified) · final explain-back (rubric) |
| **Completion criteria** | Capstone meets its brief's criteria (§J.4); modification challenge passed at assistance ≤ H2; explain-back passed |

### C.4 Day-by-day plan (45–90 min/day, 5–7 days/week)

Legend: **L** lesson · **Ex** exercise · **Xp** experiment · **KC** knowledge check · **B** build/milestone · **Expl** explain-back.

| Day | Focus | Activities | Evidence produced | Min |
|---|---|---|---|---|
| 1 | What AI is and isn't; generative AI | L: 2 short lessons · Ex: ask an assistant 5 questions (one it should fail) and classify the answers · KC | KC, exercise | 55 |
| 2 | LLMs and tokens | L · Ex: tokenize 3 texts, estimate cost · KC (numeric) | KC | 55 |
| 3 | Models vs. providers vs. apps; context windows | L ×2 · Ex: classify 6 products · Xp: a long chat "forgets" early details | KC, Xp | 65 |
| 4 | Prompts; temperature and nondeterminism | L · Xp: variance run (5× at T=0 vs T=1) · Expl (3 sentences) | Xp result, explanation | 70 |
| 5 | Hallucination and grounding | L · Xp: grounded vs. ungrounded on 5 questions · KC scenario | Xp, KC | 70 |
| 6 | Evaluating AI answers; privacy and responsible use | L ×2 · Ex: classify statements into claim types (AIL §10.5) · KC privacy scenarios | KC ×2 | 60 |
| 7 | **P1 Trustworthy Explainer** | B: write instructions → run 8 tests → fix one failure → re-run · Expl · Week 1 check | P1 results, explain-back, weekly KC | 90 |
| 8 | Prompt structure | L · Ex: rewrite 3 weak prompts, compare outputs | exercise (rubric + checks) | 60 |
| 9 | System vs. user; few-shot | L ×2 · **Xp E1** zero- vs. few-shot (10 inputs) · Expl | Xp, explanation | 75 |
| 10 | JSON and structured output | L ×2 · Ex: write 3 schemas · B: micro JSON extractor (5 inputs, schema checks) | exercise, micro-build | 75 |
| 11 | Debugging AI outputs | L · Ex: debug 3 broken prompts with the failure checklist · **Xp E2** | debugging log, Xp | 65 |
| 12 | Cost and choosing a model | L ×2 · **Xp E3**: 3 models on your test set · KC numeric | Xp, KC | 75 |
| 13 | Evaluating outputs; how apps call models (preview) | L ×2 · Ex: write 10 test cases for P2 · KC | test-set quality check, KC | 60 |
| 14 | **P2 Structured Extractor** | B: build → run tests → improve once → model-choice justification · Week 2 check | P2 results, justification, weekly KC | 90 |
| 15 | How apps call models | L · Observation: inspect the request/response records of your own P2 runs · (code path: first Python API call) | record-grounded answers | 75 |
| 16 | Prompt templates | L · B: templated agent (5 inputs) · (code path: template function) | micro-build | 70 |
| 17 | Chat apps and memory | L · B: multi-turn assistant with summary memory · Xp: token growth per turn | micro-build, Xp | 75 |
| 18 | Embeddings and RAG | L ×2 · Ex: similarity sorting with precomputed vectors · KC | exercise (deterministic), KC | 70 |
| 19 | Tool calling, agents and workflows | L ×2 · Ex: design a tool schema · Observation: tool calls in a seeded demo run's Flight Recorder · KC | exercise, KC | 80 |
| 20 | **P3 Document Q&A**: milestones 1–2 | B: section the document · build select-then-answer · first test run | milestone checks | 90 |
| 21 | **P3**: milestones 3–4 | B: full 8-question test · fix one failure · Expl pipeline · Week 3 check | P3 results, explain-back, weekly KC | 90 |
| 22 | Capstone: choose and frame | Review 3–5 briefs · choose · problem statement + users | brief acceptance, problem statement | 60 |
| 23 | Requirements and test set | L · write requirements, success criteria, ≥ 8 test cases incl. edge cases | requirements + test set (checks + rubric) | 75 |
| 24 | Design and model selection | Design sketch · Xp: 2 models × 4 cases · choose with justification | design, Xp | 75 |
| 25 | Build milestone 1 (core path) | B · first runs | milestone check | 90 |
| 26 | Build milestone 2 (structure, grounding, edge cases) | B | milestone check | 90 |
| 27 | Test and debug | Full test run · failure log · one fix | evaluation run, debugging log | 75 |
| 28 | Improve | One targeted change · before/after comparison run | before/after evidence | 75 |
| 29 | Document + modification challenge | README (what, how, limits, cost) · **modification challenge**: a new requirement implemented at ≤ H2 | README (rubric), modification (verified) | 75 |
| 30 | Demo and graduation | Explain-back (5 Professor questions, rubric-graded by the Grader) · portfolio page · completion report · next-track recommendation | explain-back, report | 60 |

**Total ≈ 36 hours** (≈ 72 min/day on average). Days 7, 14 and 21 are the heaviest. The Coach offers **catch-up rules**: optional exercises are dropped first, required evidence is never dropped, and the program can stretch to 6 weeks (a "self-paced" pace setting).

### C.5 Why this is a progression, not 30 disconnected lessons

- Each week's project **consumes the previous week's skills**: P1 (prompting + grounding) → P2 (structure + model choice, reusing P1's evaluation habit) → P3 (P2's structured output becomes citations; retrieval builds on grounding) → Capstone (combines all of it, and the learner designs the tests).
- Experiments aren't decoration. **E3's result is the input to P2's model choice**, and the capstone's model selection repeats the same method at a smaller scale.
- Observation tasks in Week 3 use **the learner's own Week 2 runs**, so the platform itself becomes the learning material.
- Evaluation is practiced every week, then *designed by the learner* in Week 4.

---

## D. Project Ladder

### D.1 Levels

| Level | Name | Typical time | Scaffolding | Example |
|---|---|---|---|---|
| **L0** | Guided Exercise | 5–15 min | Fully guided; one skill | Rewrite a prompt using a template |
| **L1** | Micro Build | 15–45 min | Step-by-step; 1–2 concepts | JSON extractor for 5 inputs |
| **L2** | Mini Project | 1–3 h | Milestones given; learner fills in the design | P1, P2 |
| **L3** | Guided Project | several sessions | Milestones given; design choices are the learner's | P3 Document Q&A |
| **L4** | Capstone | multi-day | Learner writes the requirements, tests and design; the Mentor reviews | Capstone |

### D.2 Difficulty is a profile, not a time estimate

Every Project Template carries a **difficulty profile** (a JSON payload on the template; displayed as chips, never collapsed into one number):

| Dimension | Values |
|---|---|
| Prerequisite depth | max graph depth of the required concepts (computed) |
| Concepts involved | count of `project_template_concepts` (computed) |
| Independence | guided · milestone-given · design-open · learner-specified |
| Coding complexity | none · configuration · templated code · own code |
| Evaluation complexity | none · given objective checks · given checks + rubric · learner-designed tests |
| External tools | none · platform only · learner's local environment · MA9 sandbox |
| Debugging required | none · guided · independent |
| Time estimate | range in minutes |

**Level assignment rule** (deterministic, shown on hover): the level is the maximum of the independence level, the evaluation-complexity level and a concept-count band, adjusted up one level if independent debugging or learner-designed tests are required. Time is shown but **doesn't determine the level**.

### D.3 Progression rule (recommendation, not a lock)

The Coach recommends level N+1 projects once the learner has completed ≥ 1 level-N project where **most milestones were at assistance ≤ H2**. Learners may start a higher-level project anyway; the Coach warns them and lists the missing prerequisites (AIL §17.4 principle: the user stays in control).

---

## E. Project Library

### E.1 Browsing facets (all backed by existing structures)

| Facet | Backed by |
|---|---|
| Beginner / Intermediate / Advanced | Template `audience_level` (maps to concept levels) |
| Estimated time | Template time range |
| AI topic | `project_template_concepts` → concepts → topic terms |
| Skill | Concepts of kind `skill` |
| Coding / no-code | Template `build_mode` |
| Prerequisites | Computed from concepts + learner state ("ready / 2 missing") |
| Project type | `taxonomy_terms` vocabulary `project_family` (**CR-6**) |
| Career relevance | `taxonomy_terms` vocabulary `career` (**CR-6**) |
| Available now / after MA9 | `requires_platform_capability` (AIL §4.3) |

### E.2 Starter library (24 templates)

Build modes: **NC** = no-code agent · **WF** = workflow (MA7) · **XP** = experiment (AIL.3) · **LC** = local code (learner's machine) · **SB** = sandboxed coding (**MA9**).

| # | Project | Lvl | Mode | Time | Concepts taught/assessed | Evidence it can generate | Career tags |
|---|---|---|---|---|---|---|---|
| 1 | Prompt Makeover | L0 | NC | 10m | Prompt Structure | exercise checks | all |
| 2 | Temperature Lab | L1 | XP | 20m | Inference, Nondeterminism | experiment + interpretation | all |
| 3 | Hallucination Hunt | L1 | XP | 30m | Hallucination, Grounding, Evaluating Answers | experiment, classification KC | research, education |
| 4 | Token Budget Calculator | L1 | NC | 20m | Tokens, Pricing | numeric checks | business |
| 5 | Trustworthy Explainer (P1) | L2 | NC | 90m | Prompting, Grounding, Evaluating Answers | App Test Set results, explain-back | education |
| 6 | Few-shot Showdown | L1 | XP | 30m | Few-shot, Prompt Structure | experiment | all |
| 7 | Structured Extractor (P2) | L2 | NC | 2h | JSON, Structured Output, Choosing a Model | field accuracy, justification | business, data |
| 8 | Model Face-off | L2 | XP | 90m | Choosing a Model, Pricing, Evaluation | 3-model comparison + interpretation | all |
| 9 | Email Tone Rewriter | L1 | NC | 30m | System Instructions, Evaluation | rubric + checks | productivity |
| 10 | Meeting Notes → Action Items | L2 | NC | 2h | Structured Output, Evaluation | field accuracy on sample transcripts | productivity, business |
| 11 | Resume Bullet Improver (synthetic resumes) | L2 | NC | 2h | Prompt Templates, Evaluation, Privacy | rubric + constraint checks | career |
| 12 | Job Posting Analyzer | L2 | NC | 2h | Structured Output, Choosing a Model | extraction accuracy | career |
| 13 | Translation Assistant with Glossary | L2 | NC | 2h | System Instructions, Evaluation | glossary-term checks | business, education |
| 14 | Chat Assistant with Memory | L2 | NC/LC | 2h | Conversation Memory, Context Windows | token-growth experiment, tests | software |
| 15 | Document Q&A (P3) | L3 | WF/LC | 3h | RAG, Grounding, Workflows | citation accuracy, refusals | research, support |
| 16 | Customer Support Answerer (FAQ-grounded) | L3 | WF | 4h | RAG, Evaluation, Human Approval | grounded-answer checks; escalation cases | support |
| 17 | Research Brief Assistant (provided sources) | L3 | WF | 4h | Grounding, Structured Output, Evaluating Claims | citation + claim-type checks | research |
| 18 | Summarize → Critique → Revise Workflow | L3 | WF | 3h | Workflows, Evaluation | before/after quality checks | productivity |
| 19 | AI Output Evaluator (build a judge + validate it) | L3 | NC/XP | 4h | Evaluation, LLM-as-Judge | judge-vs-ground-truth agreement | data, software |
| 20 | Personal Knowledge Assistant (own notes) | L3 | WF/LC | 4h | RAG, Privacy, Memory | citation checks | productivity |
| 21 | Simple Tool-using Agent (design + simulate) | L3 | NC | 3h | Tool Calling, Agents | schema-design checks, observation | software |
| 22 | Python API Starter | L1 | LC | 45m | How Apps Call Models, Python Basics | submitted output (self-reported) + explain | software |
| 23 | Real Embedding Search | L3 | LC→SB | 4h | Embeddings, RAG | tests (verified post-MA9) | data, software |
| 24 | Real Tool Agent with Tests | L4 | **SB** | multi-day | Tool Calling, Agents, Sandboxing | automated tests (MA9) | software |

Every template **must** declare its concepts (with a role: *taught* / *applied* / *assessed*) and its evidence outputs. A template with no assessed concept or no verifiable output can't be published (validation rule).

---

## F. Build With Me

### F.1 Flow (a guided state machine over project milestones)

```
UNDERSTAND ─> PLAN ─> BUILD(step) ─> TEST ─> DEBUG? ─> IMPROVE ─> EXPLAIN ─> REFLECT ─> SAVE EVIDENCE
                 ▲        │                      │
                 └────────┴── next step ◄────────┘   (BUILD/TEST/DEBUG repeat per milestone)
```

| Stage | Professor/Mentor behavior | Learner does | Stored |
|---|---|---|---|
| **UNDERSTAND** | States the goal and the user need; asks "Who is this for? What would 'good' look like?" | Answers in 1–2 sentences | Milestone note |
| **PLAN** | Asks the learner to list the components; offers a checklist only after an attempt | Lists components (e.g., input → instructions → model → output format → tests) | Plan (graded for completeness against the template's component list) |
| **BUILD** | Gives one small step with a goal and acceptance check; asks a question before revealing structure | Implements the step (writes instructions or schema; code on the code path) | Artifact version |
| **TEST** | Runs (or asks the learner to run) the App Test Set; asks "What do you expect before we run it?" | Predicts, runs, compares | Evaluation results (platform-verified) |
| **DEBUG** | Asks "Which cases failed? What do they have in common?"; Hint Ladder on request | Diagnoses, changes, re-runs | Debugging log (attempts, hypotheses, results) |
| **IMPROVE** | Asks for one targeted change with a hypothesis | Changes one thing, re-runs | Before/after comparison |
| **EXPLAIN** | Asks 2–3 explain-back questions about how the system works and why | Answers | Rubric-graded (Grader Agent) |
| **REFLECT** | "What surprised you? What would you do differently?" | Short reflection | Stored; **not graded** (reflection isn't evidence of mastery) |
| **SAVE EVIDENCE** | Shows what evidence was recorded and why, including assistance levels | Confirms | `learning_evidence` rows |

### F.2 Example: "Build a simple AI document summarizer" (no-code, L2)

> **Mentor:** Today we'll build a summarizer for long emails. Before we start: who would use it, and what makes a summary *good* for them?
> **Learner:** Me, for work emails. Good = short, and I don't miss deadlines.
> **Mentor:** Great, that's a testable requirement. Let's write it down: *≤ 5 bullets; every deadline in the email appears in the summary.* What pieces does the system need?
> **Learner:** Instructions and… the email?
> **Mentor:** Yes, two of them. What else decides whether we can *check* the deadline rule automatically? *(Hint H1 available.)*
> …
> **[TEST]** 6 test emails: 4/6 pass. The two failures both have deadlines written as "next Friday".
> **Mentor:** What do the two failures have in common?
> **Learner:** Relative dates.
> **Mentor:** What change would you try? Tell me your hypothesis before changing anything.

### F.3 Rules

- The Mentor **asks before it tells**. Each stage has at least one learner-produced response before any explanation beyond H1.
- The Mentor **never runs a test and reports success** itself. Results come from platform execution records only (§G.3).
- Sessions can be **paused and resumed**. State lives in `milestone_attempts` (§N), not in chat history.

---

## G. Professor / Project Mentor Behavior

### G.1 Architecture decision: option C (reuse), realized as a Professor mode

| Option | Assessment |
|---|---|
| A) Separate registered Agent | More isolation, but duplicates context assembly, validation, pedagogy and budgeting; two personas a beginner has to understand |
| B) Pure Professor intent | Simple, but mentor behavior (hint policy, milestone state, no-solution rules) would be mixed into general teaching prompts |
| **C) Reuse via Professor Agent + a "Mentor" mode** ✅ | Same registered Agent (`AIL Professor`) and the same context assembler and validator, with a **separate prompt version per mode** (`teach`, `mentor`) chosen by the Intent Resolver. Mentor mode adds: milestone context, the hint policy engine (deterministic), and stricter output validation |

**Reasoning:** the Professor and Mentor share 90% of their machinery (context contract, provenance voice, validator, budget). Separating them by *prompt version + mode-specific validator* gives the behavioral separation without a second agent. **Assessment stays in the separate Grader Agent**, so the helper never grades its own help. The **Coach stays deterministic** (planner + program schedule).

### G.2 Mentor intents (added to AIL §16.2 via **CR-4**)

| Intent | Deterministic part | LLM part |
|---|---|---|
| `recommend_project` | Library filter: learner state vs. template prerequisites, ladder progression rule, time, interests, build path | Short rationale |
| `check_prerequisites` | Graph + learner state | — |
| `plan_milestones` | Template milestones (fixed for L0–L3; for L4, drafted then confirmed) | Draft for L4 only; learner edits |
| `hint` | **Hint policy engine** decides the allowed level (§H) | Writes a hint *at that level only* |
| `review_progress` | Milestone status, test results, assistance so far | Feedback tied to specific results |
| `explain_error` | Error text + milestone context | Meaning of the error + where to look (no fix) |
| `recommend_tests` | Template's test guidance; missing edge-case categories (deterministic checklist) | Suggestions phrased as questions |
| `ask_explain` | Picks explain-back questions from the template's question bank | Follow-up questions |
| `diagnose_gap` | Failed checks, repeated hint use by concept | "You may be missing X" + a link to the lesson |
| `next_learning` | Existing `next` rule (AIL §17.5) + program schedule | Rationale |

### G.3 Hard rules (enforced by the validator)

1. **No fabricated execution.** The Mentor may state test outcomes only by citing an execution record (agent run, evaluation) in its context. Phrases like "this works" or "tests pass" without a cited record are rejected.
2. **No fabricated evidence.** The Mentor has no write path to `learning_evidence`. Only the Evidence Writer (on graded events) writes evidence.
3. **Solution gating.** Complete solutions for L2+ milestones can't be emitted unless the hint engine has authorized the `solution` level (§H.3). The validator compares output against the milestone's reference solution: high similarity without authorization triggers rejection and regeneration at the allowed level.
4. **The learner's own words for explain-back.** During explain-back the Mentor may not supply the answer. It asks clarifying questions only.

---

## H. Hint Ladder

### H.1 Levels

| Level | Name | Content | Example (JSON extractor failing on dates) |
|---|---|---|---|
| **H0** | Independent | No help | — |
| **H1** | Conceptual clue | Names the relevant idea without saying where | "Think about how the model knows what format you want dates in." |
| **H2** | Pointer | Points to the lesson, concept or failing case; explains an error message | "Revisit 'Structured Output → field formats'. Compare failing cases 3 and 7." |
| **H3** | Partial structure | Skeleton or template with blanks | "Your schema's date field could specify: `"date": { "type": "string", "description": "____" }`" |
| **H4** | Guided help | Step-by-step walkthrough of the fix, learner performs it | "Add a format description (ISO 8601), add one example with a relative date, re-run cases 3 and 7." |
| **H5** | Solution | Complete worked solution | Full corrected schema + instructions |

### H.2 Unlock policy (deterministic)

| Level | Unlocks when |
|---|---|
| H1 | Immediately on "I'm stuck" |
| H2 | After H1 **and** at least one attempt (a run or a submitted change) since H1 |
| H3 | After H2 **and** a failed attempt, or 10 active minutes since H2 |
| H4 | After H3 **and** another failed attempt |
| H5 | L0/L1 items: after H4. L2+: after H4 **and** two further failed attempts, **or** when the learner switches the milestone to "study mode" (§H.3) |
| Challenge mode | H1–H2 only |

"Explain this error" is always available and counts as **H2**.

### H.3 Study mode ("show me how, then let me try")

Instead of forcing H5 on a stuck learner, the Mentor offers **study mode**. The learner sees a worked solution (assistance H5 is recorded for that milestone), and a **variant milestone** is generated from the template's variant spec (different input, same concept). Completing the variant at ≤ H2 produces full-credit evidence for the concept. This keeps the learner moving without passing off copied work as mastery.

### H.4 Recording

Each hint is a Professor Agent Run (Flight Recorder). `milestone_attempts.max_assistance_level` stores the highest level used for that milestone, and it's copied onto every Learning Evidence row that milestone produces (**CR-2**).

### H.5 Copy detection (deterministic, cheap)

When a learner submits an artifact, AIL compares it with every Mentor output in that milestone's session (normalized token overlap). If the submission substantially matches content the Professor showed *outside* the hint ladder (for example, a worked example in a `teach` turn or the reference solution of a related milestone), the milestone's assistance level is raised to H5 for the matching portion. This is logged and visible to the learner, who can dispute it. **Limitation:** content copied from *outside* AIL (e.g., another chatbot) can't be detected. That's mitigated by explain-back and modification challenges (§I.3).

---

## I. Project Evidence Model

### I.1 Principle

Project work is **the richest evidence source in AIL**. It's also the easiest to fake. Evidence is therefore always tied to (a) an execution or check record, (b) the learner's own explanation, and (c) the **assistance level** at which it was produced.

### I.2 Evidence types (extends AIL §18 / `learning_evidence.evidence_type`; **CR-2**)

| Evidence type | Source record | Grader | Execution verification |
|---|---|---|---|
| `knowledge_check` (existing) | check attempt | deterministic | n/a |
| `interpretation` (existing) | answer | AI rubric | n/a |
| `observation` (existing) | record-grounded answers | deterministic | n/a |
| `lab` / `experiment` (existing) | experiment | deterministic (completion) | platform-verified |
| **`milestone_check`** | milestone attempt + its check spec (tests, schema checks, field accuracy) | deterministic | platform-verified *or* self-reported |
| **`project_evaluation`** | Evaluation over the App Test Set (MA6) | deterministic (+ optional labeled judge) | platform-verified |
| **`debugging_record`** | debugging log: failed run → hypothesis → change → re-run | deterministic (structure present + the re-run exists) | platform-verified |
| **`learner_decision`** | justified choice (model choice, design choice) referencing experiment IDs | AI rubric + deterministic reference check | n/a |
| **`explain_back`** | answers to the explain-back question bank | AI rubric (Grader) | n/a |
| **`modification`** | new requirement → learner change → tests pass | deterministic | platform-verified |
| **`reproduction`** | re-run of the project on new inputs with the expected behavior | deterministic | platform-verified |
| **`human_review`** (Teacher seam) | reviewer feedback on a submission | human | n/a |

**New columns on `learning_evidence` (CR-2):**

- `assistance_level`: H0–H5; null for non-project evidence
- `execution_verification`: `platform_verified` / `sandbox_verified` / `self_reported` / `not_applicable`
- `milestone_attempt_id`: nullable FK

### I.3 What never counts as evidence

| Action | Treatment |
|---|---|
| Opening a project or lesson | At most EXPOSED (the existing rule) |
| Clicking "Complete" | Not possible without the milestone's check spec being satisfied |
| Copying generated code or text | Assistance raised to H5 (§H.5); counts at most toward EXPOSED |
| Watching a lesson or reading a worked solution | EXPOSED only |
| Reflection text | Stored, not graded, not evidence |
| Self-reported execution (code run on the learner's machine) | Can't be the deterministic component of DEMONSTRATED |

### I.4 State transitions from project evidence

| Transition | Project evidence that qualifies |
|---|---|
| NOT_STARTED → **EXPOSED** | Started a milestone that teaches the concept; any assistance level |
| → **UNDERSTOOD** | (unchanged) knowledge check passed. *Additionally:* an `explain_back` pass on a milestone assessing the concept counts toward UNDERSTOOD only when paired with ≥ 1 deterministic check item for that concept (keeps the AIL §18.4 rule) |
| → **PRACTICED** | Any `milestone_check`, `project_evaluation`, `debugging_record`, `lab` or `experiment` assessing the concept, **at any assistance level ≤ H4**. H5 work never counts as practice |
| → **DEMONSTRATED** | The concept kind's requirement set (AIL §18.3) is satisfied, where project evidence can fill the "lab/experiment/observation" slots **only if**: (1) ≥ 1 qualifying item is **platform- or sandbox-verified**; (2) that item was produced at **assistance ≤ H2**, *or* a `modification` / `reproduction` for the same concept was later completed at ≤ H2 (independence can be shown after help); (3) an `explain_back` or `interpretation` for the concept passed; (4) the existing rule holds: not on AI-graded evidence alone |

**Skill-kind requirement set (new, CR-1):** a platform-verified exercise or milestone at ≤ H2 **and** a short explanation (rubric), **or** two verified exercises at ≤ H2 on different inputs.

### I.5 Assistance-weighted display

On My AI Knowledge and on the completion report, project-backed evidence shows its assistance level: *"Structured Output — Demonstrated · P2 milestone 3 (independent, H0) · modification challenge (H1)"*. Assistance isn't hidden or averaged away.

---

## J. Capstone

### J.1 Proposal generation (3–5 options; the learner chooses)

**Inputs:** interests and career goal (declared), coding comfort / build path, demonstrated concepts, time remaining in the program, selected track, and MA9 availability.

**Process:**
1. **Deterministic candidate set:** capstone-eligible templates (L4 or L3 with an "extend" spec) whose prerequisites are ≥ PRACTICED, filtered by build path and platform capability.
2. **Diversity rule:** at most 2 proposals from the same `project_family`; at least 1 that matches a declared interest, and at least 1 that's "safe" (lowest prerequisite gap).
3. **Personalization (LLM, labeled):** the Mentor adapts each brief's *flavor* (domain, sample data, user persona) to the learner's interests **without changing the concepts, milestones or evaluation criteria** defined by the template.
4. The learner picks 1 (or asks for more options, max 2 refreshes). The proposals are stored as `project_attempts` in state `proposed`; the chosen one becomes `active` and the others `declined`.

### J.2 Categories and example capstones

| Category | Example (no-code / workflow) | Example (code path) |
|---|---|---|
| Productivity | Email triage & summary with deadline extraction | Local email-to-tasks script with tests |
| Career | Job-fit analyzer: posting vs. (synthetic) resume → structured gap report | Resume keyword matcher + LLM feedback |
| Education | Study-guide generator from course notes with self-quiz + grounded answers | Flashcard generator with answer checking |
| Business | Customer-review insight extractor → structured themes + sentiment + evidence quotes | Same, with batch processing and a cost report |
| Data | Natural-language → structured query spec over a small CSV (validated) | Python + pandas assistant with tests |
| Software development | Code-review checklist assistant for small diffs (text in, findings out) | Unit-test suggestion tool (fully verified post-MA9) |
| Customer support | FAQ-grounded answerer with escalation rules (Human Approval node) | Same, with local retrieval |
| Research | Source-grounded brief generator with claim-type labels (applies claim literacy) | Same, with embeddings |
| Automation | Summarize → classify → route workflow with a bounded retry | Scripted pipeline (post-MA9 sandbox) |

### J.3 Project Brief (standard structure)

```
PROJECT BRIEF — Customer Review Insight Extractor                    Ladder L4 · est. 9–11 h
Problem        Small shop owner gets 200 reviews/month; can't see patterns.
Goal           Turn raw reviews into themes, sentiment and quotable evidence.
Requirements   R1 structured output (theme, sentiment, quote) · R2 quotes must appear verbatim in input
               R3 flag "unclear" instead of guessing · R4 cost ≤ $0.50 per 200 reviews
Constraints    No-code path (Agent + Workflow) · synthetic/public reviews only (no personal data)
Concepts       Assessed: Structured Output, Grounding, Evaluation, Choosing a Model, Workflows,
               Problem Framing · Applied: Prompt Templates, Pricing
Milestones     M1 problem & users · M2 requirements & 10-case test set · M3 design + model choice (Xp)
               M4 core extraction · M5 grounding & "unclear" handling · M6 evaluation run
               M7 one improvement (before/after) · M8 README · M9 modification challenge · M10 explain-back
Expected evidence  test set (rubric + completeness) · Xp · milestone checks · evaluation · debugging log
                   · before/after · README (rubric) · modification (verified) · explain-back (rubric)
Evaluation criteria  ≥ 8/10 test cases pass · 100% verbatim-quote check · cost within R4 on the full set
                     · README covers purpose, design, limits, cost · modification at ≤ H2 · explain-back pass
Estimated effort  9–11 h over Days 22–30
```

### J.4 Capstone pass rules

- Evaluation criteria are **fixed when the brief is accepted** (brief snapshot). Changing them requires an explicit re-scoping with the Mentor, recorded with a reason.
- Criteria must be ≥ 70% objective (deterministic checks). Rubric-graded items can't be the only pass condition.
- The final **explain-back** includes 5 questions: 2 on design choices, 1 on a failure and its fix, 1 on limits, 1 transfer question ("how would you adapt it for X?").
- **Portfolio page** (generated, learner-editable text): problem, demo (selected test inputs and outputs from real runs), test results, model choice with evidence, cost, limits, what was learned, and evidence links. Exportable as Markdown.

---

## K. Graduation / Completion Report

### K.1 Completion rules (defaults; owner decision W1)

A learner **completes** AI Foundations Builder when:
- All program-required concepts are ≥ PRACTICED, and ≥ 12 are DEMONSTRATED
- P1, P2 and P3 are submitted with their completion criteria met (including the "documented failure analysis" alternative)
- ≥ 3 experiments are completed with interpretations
- The capstone passes (§J.4)

If a learner doesn't meet every rule by day 30, the enrollment stays **active** (self-paced continuation). There is no "failed" state.

### K.2 Report (snapshot, evidence-linked)

```
AI FOUNDATIONS BUILDER — 30 DAYS (program v1.0)        Completed 2026-10-24 · Serge T.

Statement: Completed AI Foundations Builder. Demonstrated foundational AI skills in
prompting, structured output, grounding, model selection and basic evaluation.

CONCEPTS (29 in program)       Demonstrated 18 · Practiced 6 · Understood 3 · Exposed 2   [each → evidence]
PROJECTS                       8 exercises · 4 micro-builds · 3 mini-projects · 1 guided project · 1 capstone
                               Independence: 21 of 27 project milestones at assistance ≤ H2
EXPERIMENTS                    5 (E1 few-shot, E2 system vs user, E3 model face-off, ...)   [→ results]
KNOWLEDGE CHECKS               24 attempts · first-attempt pass 17
CAPSTONE                       Customer Review Insight Extractor · 9/10 tests · 100% quote check
                               · $0.31 per 200 reviews · modification (H1) · explain-back pass  [→ portfolio]
STRENGTHS                      (derived) Structured Output, Evaluation: independent, verified, re-used in capstone
AREAS TO CONTINUE              (derived) RAG: practiced with H3 help, not demonstrated · Agents: understood only
RECOMMENDED NEXT TRACK         AI Engineering (because: RAG and Tool Calling are the next unmet prerequisites
                               for your goal "build things")
Model spend during program     $4.12 (AIL budget)
```

### K.3 Language rules (validator-enforced)

- **Allowed:** "Completed AI Foundations Builder", "Demonstrated foundational AI skills in …", "Practiced …", "Built …".
- **Blocked terms:** "expert", "certified", "mastered", "professional-level" (the certificate question is deferred; §V).
- **Strengths / Areas to continue** are *derived deterministically* from evidence (demonstrated + low assistance + reused across projects → strength; practiced with high assistance or understood-only → area to continue). The LLM may only phrase them and must cite evidence IDs.
- The report is stored as a **snapshot artifact** (Markdown + JSON of the evidence IDs) so it doesn't change when later evidence arrives.

---

## L. Teacher / Creator Future Seam

### L.1 What AIL.5 V1 includes now (seams only)

| Seam | Why it's needed now | Cost now |
|---|---|---|
| `programs.author_user_id`, `project_templates.author_user_id` | Content has an owner, so instructors can author later without a data migration | 2 columns |
| **Programs and project templates are versioned and immutable once published**; enrollments reference a *version* | Instructors will edit programs while learners are mid-cohort | Already needed for integrity |
| **Enrollment** as a first-class entity (learner × program version) | Cohorts group enrollments later | Needed now for progress |
| `milestone_attempts` with status, attempts count, `max_assistance_level`, timestamps | "Where are learners stuck" analytics later | Needed now for the Hint Ladder |
| Evidence grader type `human` (exists) + `human_review` evidence type | Instructor review of submissions | Enum value |
| Program-level **evidence requirements** and **completion rules** as data (JSON on the program version), not code | Instructors define requirements later | Needed now for W1 flexibility |
| `enrollments.progress_shared_with_author` (default **false**) | Consent-based visibility is the hard privacy problem for teacher mode; the flag fixes its semantics early | 1 column (owner decision W8) |

### L.2 Explicitly deferred

Cohorts/classrooms · instructor roles and permissions beyond "author" · learner rosters · school administration · tuition/payment · attendance · certificates · large cohort analytics dashboards · messaging · grading queues · deadlines per cohort · plagiarism tooling beyond §H.5 · content marketplace.

### L.3 How Teacher Mode would attach later (not designed further now)

`cohorts (id, program_version_id, instructor_id, …)` + `cohort_memberships` referencing enrollments; instructor views read `milestone_attempts` and evidence **only for enrollments with sharing consent**; instructor feedback writes `human_review` evidence. This needs **no change** to the entities introduced here.

---

## M. Existing-Component Reuse

### M.1 Reuse by AIL phase

| Existing | AIL.5 use |
|---|---|
| **AIL.1** Concept Graph, concept versions, Learning Items, Learning Evidence, Learner State, planner, Professor, Grader, My AI Knowledge, knowledge checks, observation tasks | The whole learning engine. Programs *select and order* concepts and items; they don't hold content. Lessons are concept versions; exercises and checks are Learning Items; state is computed by the same service |
| **AIL.2** Radar, claims, Verification Level, provenance voice | Beginner weekly "One development, explained" card; claim-literacy exercises built from real Radar claims; capstone research projects use typed claims |
| **AIL.3** Personal Eval Sets, Experiment Lab, Model Comparison | **App Test Sets are Personal Eval Sets** (role term `learner_app`); E1–E3 and capstone model selection are ordinary Experiments; results are PLATFORM_OBSERVATION on the learner's slice |
| **AIL.4** Retention/review, scheduled briefs, calibration | Post-program review of core foundations concepts (the retention rules apply unchanged); the graduate's continuing weekly brief in simplified form |

### M.2 Reuse of platform (MA) components

| MA component | AIL.5 use |
|---|---|
| Agent Registry + Agent Versions + Prompt Versions (MA2, MA §24.4 #3) | The learner's no-code app **is** an Agent (in the AIL project, owned by the learner); each improvement is a new prompt version → before/after comparison for free |
| Single-agent execution (MA3) | Running learner apps on test inputs |
| MA3 sandbox + Tool Bus test tool | *Conditional pre-MA9:* running a learner's submitted repo tests (§O.2) |
| MA5 Comparison Runs | Model face-offs, before/after comparisons |
| MA6 Evaluation | App Test Set grading (objective metrics first) |
| MA7 Workflow/DAG | Week 3+ multi-step apps (select → answer; summarize → critique → revise); Human Approval node in support-bot projects |
| Human Approval (MA §23) | Experiment spend over threshold; approval nodes inside learner workflows |
| Flight Recorder | Hints, Mentor turns, learner runs, observation-task source records |
| Cost/Budget Governor + `usage_events` | Per-learner budgets (existing `user` scope) |
| `artifacts` | Learner submissions, portfolio exports, completion report snapshots (needs **MA-CR-1**) |
| MA9 Tool Runtime (later) | Sandboxed coding environments, file editing, shell, automated tests, browser and MCP labs |

### M.3 What AIL.5 does *not* reuse (and why)

- **Workflow/DAG for tutoring flow:** Build With Me is interactive, learner-paced state, not an agent-execution DAG. MA7's bounding and resumability semantics are for agent runs; tutoring state lives in `milestone_attempts`.
- **Tracks for programs:** a Track is an unordered goal set (AIL §17.2). A Program is a *time-sequenced, versioned* schedule with projects and completion rules. They're related (a program targets a track) but distinct.

---

## N. Minimum Data-Model Changes

### N.1 Classification of what's needed

| Need | Reuse / extend / new | Notes |
|---|---|---|
| Lesson content | **Reuse** `concepts` / `concept_versions` | Programs reference concepts; they never copy content |
| Exercises, checks, scenarios, observation tasks | **Reuse** `learning_items` | Milestone checks may reference a Learning Item |
| Learner evidence | **Extend** `learning_evidence` (CR-2) | +`assistance_level`, +`execution_verification`, +`milestone_attempt_id`, new evidence types |
| Learner plan | **Extend** `learning_plan_items.origin` + `program` (CR-4) | The Coach writes program items into the plan as `planned` (enrollment = acceptance) |
| Learner profile | **Extend** `learner_profiles` (CR-5) | +`experience_mode`, +`coding_comfort`, +`career_goal_term_id`, +`build_path` |
| Taxonomy | **Extend** vocabularies (CR-6) | `project_family`, `career` |
| Concept kinds | **Extend** enum (CR-1) | `skill` |
| Learner apps | **Reuse** `agents`, `agent_versions`, `prompt_versions` | Owned by the learner inside the AIL system project |
| App test cases | **Reuse** `eval_sets` / `eval_set_versions` / `eval_set_version_tasks` + `tasks` | Role term `learner_app` |
| App runs | **Reuse** `task_runs`, `agent_runs`, `model_calls`, `workflow_runs` | Standard execution |
| Grading of app runs | **Reuse** `evaluations` | Objective metrics first |
| Experiments E1–E3, capstone model choice | **Reuse** `experiments` (AIL.3) | `learning_item_id` links to the lab |
| Submissions, portfolio, completion report | **Reuse + extend** `artifacts` (**MA-CR-1**) | `agent_run_id` becomes nullable; add `milestone_attempt_id` / `enrollment_id` owner reference |
| Hints | **Reuse** Professor Agent Runs + `execution_events` | Only the max level is materialized on `milestone_attempts` |
| Programs, program versions, schedule | **NEW** | — |
| Project templates, template concepts, milestones | **NEW** | — |
| Enrollment / progress | **NEW** | — |
| Project attempts, milestone attempts | **NEW** | — |
| Capstone proposals | **Reuse** `project_attempts` (state `proposed`) | No separate table |
| Completion report | **Reuse** `artifacts` (snapshot) | No separate table |
| Glossary | **Derived** from concepts | No table |

### N.2 NEW tables (9)

| Table | Key fields | Notes |
|---|---|---|
| `programs` | id, slug, title, description, author_user_id, target_track_term_id, current_version_id, status | Stable identity |
| `program_versions` | id, program_id, version, status (`draft`/`published`/`retired`), duration_days, completion_rules (JSON rule payload), published_at | Immutable once published |
| `program_items` | id, program_version_id, week, day, position, item_kind (`concept_lesson`/`learning_item`/`project`/`checkpoint`), concept_id / learning_item_id / project_template_id (exactly one), required (bool), est_minutes, purpose_text | The schedule; "Why am I learning this?" uses `purpose_text` |
| `project_templates` | id, template_key, version, status, title, audience_level, ladder_level, family_term_id, build_mode, brief_md, difficulty_profile (JSON), evidence_requirements (JSON), variant_spec (JSON), requires_platform_capability, est_minutes_min/max, capstone_eligible, author_user_id | **One row per version** (`template_key` + `version` unique); immutable once published, so no separate versions table |
| `project_template_concepts` | project_template_id, concept_id, role (`taught`/`applied`/`assessed`) | Normalized, not a JSON list |
| `project_milestones` | id, project_template_id, position, title, instructions_md, check_spec (JSON: tests / schema / field-accuracy / rubric refs), learning_item_id (nullable), expected_artifact_kind, evidence_type, reference_solution_ref, hint_content (JSON H1–H4 authored hints, optional) | Authored hints are preferred over generated ones (cost + quality) |
| `enrollments` | id, user_id, program_version_id, pace (`30d`/`self_paced`), status (`active`/`paused`/`completed`/`withdrawn`), started_at, completed_at, progress_shared_with_author (bool, default false) | Progress is **derived** from evidence + milestone attempts; no cached percentages |
| `project_attempts` | id, user_id, project_template_id, enrollment_id (nullable), is_capstone, status (`proposed`/`declined`/`active`/`submitted`/`passed`/`needs_work`/`abandoned`), brief_snapshot (JSON: personalized brief + fixed criteria), learner_agent_id (nullable), app_eval_set_id (nullable), started_at, submitted_at | Standalone library projects have no enrollment |
| `milestone_attempts` | id, project_attempt_id, project_milestone_id, status (`not_started`/`in_progress`/`checking`/`passed`/`failed`/`skipped_study_mode`), attempts_count, max_assistance_level, mode (`normal`/`challenge`/`study`/`variant`), started_at, completed_at | Resumable Build With Me state |

### N.3 Explicitly not created

`lessons` (concept versions are lessons) · `hints` (Flight Recorder) · `glossary` · `capstone_proposals` · `completion_reports` · `portfolios` (artifact + derived view) · `project_template_versions` (row-per-version) · `cohorts`, `classes`, `instructor_roles`, `certificates` (deferred) · `program_progress` cache.

### N.4 Integrity rules

- Published `program_versions` and `project_templates` rows are immutable; edits create new versions.
- A template can't be published without ≥ 1 `assessed` concept and ≥ 1 milestone with a deterministic check.
- `program_items` must respect prerequisite order: a concept can't be scheduled before its prerequisites unless they're marked "assumed" in the program version's rules. Validated at publish.
- Learner-owned agents and eval sets in the AIL project are scoped to the learner (RBAC), like other learner data.

---

## O. Pre-MA9 vs. Post-MA9 Boundary

### O.1 Capability split

| Capability | Pre-MA9 | Post-MA9 |
|---|---|---|
| Lessons, Professor explanations, knowledge checks | ✅ | ✅ |
| Guided exercises (prompt, schema, classification, numeric) | ✅ | ✅ |
| No-code apps (learner-authored Agents) run on test sets | ✅ MA3 + MA6 | ✅ |
| Multi-step apps (workflows) | ✅ with MA7 (fallback: single agent with structured multi-part output) | ✅ |
| Model experiments (face-offs, variance, prompt comparisons) | ✅ AIL.3 on MA5/MA6 | ✅ |
| Observation tasks on the learner's own runs | ✅ | ✅ |
| Guided project specs, briefs, milestones, Build With Me | ✅ | ✅ |
| Learner-submitted text artifacts (plans, READMEs, test sets) | ✅ | ✅ |
| Code path: learner codes locally, submits code + output | ✅ **self-reported execution** | ✅ |
| Code path: automated tests on a submitted repo | ⚠️ *Conditional:* only if the MA3 sandbox + test tool can run a learner-submitted repo snapshot safely (owner decision W6). Otherwise self-reported | ✅ sandboxed, platform-verified |
| In-platform code editing, governed filesystem, shell | ❌ | ✅ MA9 |
| Real embeddings / vector search as a governed tool | ❌ (code path local only) | ✅ |
| Browser / computer-use labs | ❌ | ✅ |
| MCP / third-party tool labs | ❌ | ✅ |
| Sandboxed per-learner project environments | ❌ | ✅ |

### O.2 Rules

- AIL.5 **builds no execution runtime.** Every run goes through MA3/MA5/MA6/MA7 pre-MA9 and through MA9's governed runtime afterwards.
- Learner code is **never executed outside the platform sandbox**. There's no "run my script" feature pre-MA9 other than the conditional W6 path.
- **Platform provider keys are never given to learners** (ADR-5). On the code path, learners use their own provider key on their own machine. The program says this clearly and teaches key safety as part of the "How Apps Call Models" concept.
- Templates declare `requires_platform_capability`. MA9-only templates appear as "Available after MA9" (AIL §4.3). Programs published pre-MA9 contain no MA9-only required items.

---

## P. UI / Navigation

### P.1 Placement

Under **Learn** in the canonical nav (AIL §5.1), add **Academy**:

```
Learn
├── My Plan · Concepts · Tracks · Professor   (existing)
└── Academy                                   (new, AIL.5)
    ├── Today            (Coach's session for today: timed steps + "why today")
    ├── My Program       (week/day map, progress by evidence, catch-up options)
    ├── Projects         (Library with facets §E.1 · My projects)
    ├── Build With Me    (milestone workspace)
    ├── Portfolio        (completed projects with evidence)
    └── Completion       (report when eligible)
```

In **Beginner Mode**, Academy *is* the home: *Today · My Program · Projects · Ask the Professor · My Progress · Glossary · More*.

### P.2 Build With Me workspace (layout)

```
┌ Milestone 3 of 6 — Make dates reliable ─────────────────────── Assistance so far: H1 ┐
│ LEFT: instructions · acceptance check · "Why this step?"                              │
│ CENTER: your artifact (agent instructions / schema / workflow config / code upload)   │
│         [Run tests] → results table (pass/fail per case, cost, tokens) from real runs  │
│ RIGHT: Mentor panel — questions, "I'm stuck" (next hint: H2, unlocks after 1 attempt) │
│        "Explain this error" · "Show me an example" · "Explain like I'm new"           │
│ FOOTER: Debug log (auto-captured: run → change → run) · Save & pause                  │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

### P.3 My Program

A week/day grid with states per item (not started / in progress / done / needs work), time used vs. estimate, and **evidence-based progress**: "Required concepts: 11 of 29 demonstrated, 20 of 29 practiced or better". No percentage bars without denominators (AIL §21.2).

---

## Q. Cost Controls

| Control | Design |
|---|---|
| **Per-learner budget** | Existing `budgets` with scope `user` within the AIL system project; program default (owner decision W4). The program's estimated model spend is shown at enrollment |
| **Cheap defaults** | Learner apps default to a low-cost model; Week 2's model face-off is the only planned multi-model spend before the capstone |
| **Small test sets** | App Test Sets of 5–10 cases; variance runs capped at 5 repetitions |
| **Authored hints first** | H1–H3 come from `project_milestones.hint_content` when authored; LLM hints are generated only when absent |
| **Mentor turn caps** | Per-day Mentor turn cap and per-turn token cap; beyond the cap, deterministic help only (lesson links, authored hints) |
| **Cached explanations** | ELI-new and concept explanations are cached per concept version and level |
| **Estimate before run** | Every "Run tests" shows the estimated cost; runs above the learner's per-action threshold need confirmation; experiments follow AIL §14.2 approval rules |
| **Graceful degradation** | When the budget is exhausted, lessons, checks, authored hints and deterministic feedback still work; runs pause with a clear message |
| **Owner visibility** | Settings → Budget shows spend per learner and per program |

---

## R. Privacy and Security

| Area | Design |
|---|---|
| **Learner data scope** | All Academy data is user-scoped (AIL §30). Program authors see nothing unless `progress_shared_with_author` is true (default false; Teacher Mode deferred) |
| **Personal data in projects** | Projects that touch personal data (resumes, emails, meeting notes) ship with **synthetic sample data** as the default; uploading real personal data shows a warning and requires confirmation; AIL Agents and learner apps handling uploads declare `no_training_retention` |
| **Professor/Mentor context** | Extended (**CR-3**) to include the learner's own project artifacts and runs *for the active project attempt only*; still no access to unrelated projects, platform task content or chat history for profiling |
| **Prompt injection via learner documents** | Learner-provided documents reach the Mentor as quoted data. The Mentor has no tools and no write paths; its suggestions need learner action |
| **Learner apps** | Run as ordinary Agents in the AIL project with **no tool grants pre-MA9** (text in, text out), so a learner app can't act on anything |
| **Submitted code** | Never executed outside the platform sandbox; pre-MA9 sandbox execution only if W6 is approved, with no network egress and time/resource limits |
| **Keys** | Platform provider keys never leave the server (ADR-5); code-path learners use their own keys locally; AIL never asks learners to paste keys into the platform |
| **Evidence integrity** | `learning_evidence` stays append-only; assistance levels are system-recorded (not learner-editable); disputes add records |
| **Multi-user installation** | Beginners are separate `users` (MA1 RBAC); learner-owned agents, eval sets and attempts are RBAC-scoped; tests extended accordingly (see CR-9) |

---

## S. Acceptance Criteria

1. A beginner can enroll in AI Foundations Builder v1.0, see Day 1 with time estimates and "why today", and complete Days 1–7 using only pre-MA9 capabilities.
2. Programs and project templates are versioned; editing a published version creates a new one; enrolled learners stay on their version.
3. A template can't be published without an assessed concept and a deterministic milestone check.
4. Clicking "Complete" on a milestone is impossible unless its check spec is satisfied by a real execution or check record.
5. Hints unlock only per §H.2; the solution level can't be reached on L2+ milestones without the unlock conditions or study mode.
6. Every Mentor statement about test outcomes cites an execution record; statements without one are rejected by the validator. *(Automated test.)*
7. Assistance levels are recorded on milestone attempts and copied to every resulting Learning Evidence row.
8. H5 work never produces PRACTICED or DEMONSTRATED evidence. *(Automated test.)*
9. DEMONSTRATED via project evidence requires a platform- or sandbox-verified item at ≤ H2 (or a later independent modification/reproduction) plus a passed explanation, and never AI-graded evidence alone. *(Automated test.)*
10. Self-reported execution never satisfies a deterministic requirement. *(Automated test.)*
11. The capstone offers 3–5 proposals meeting the diversity rule; the learner chooses; the brief's criteria are frozen on acceptance.
12. The completion report is a stored snapshot; every capability statement links to evidence; blocked terms ("expert", "certified", "mastered") can't appear.
13. Beginner Mode hides advanced surfaces except where a lesson/step references them.
14. Learner apps, eval sets and attempts are invisible to other users. *(RBAC test.)*
15. The Academy creates no execution runtime and no MA9-dependent required items in a pre-MA9 program version.
16. With the learner's budget exhausted, lessons, checks, authored hints and the program map still work.

---

## T. UAT Scenarios

| # | Scenario | Expected result |
|---|---|---|
| T1 | New learner, coding comfort "none", 60 min/day, enrolls | No-code path selected; Day 1 plan ≈ 55 min; nav shows Beginner Mode |
| T2 | Learner opens all Week 1 lessons but takes no checks | All Week 1 concepts show EXPOSED only; the Coach proposes checks |
| T3 | Learner is stuck on P2 milestone 3 and presses "I'm stuck" 4 times without attempting anything | Only H1 is given; H2 is locked with "try one change first" |
| T4 | Learner uses study mode on P2 milestone 3, then completes the variant at H0 | Milestone at H5 (study); variant at H0 produces PRACTICED/DEMONSTRATED-eligible evidence |
| T5 | Learner pastes a Mentor worked example from a teach turn into their app | Copy detection raises the milestone to H5; the learner sees the notice and can dispute |
| T6 | Mentor is prompted "just tell me it passes" | Mentor refuses to claim success without a run; offers to run the tests |
| T7 | Learner completes P3 with H3 help, then later passes a modification challenge at H1 | RAG evidence: PRACTICED from P3; the modification enables DEMONSTRATED if an explanation also passed |
| T8 | Code-path learner submits a local Python project without platform test support | Evidence recorded as `self_reported`; counts toward PRACTICED only |
| T9 | Learner falls 4 days behind | The Coach offers catch-up: optional items dropped, required items kept, and a "switch to self-paced" option |
| T10 | Capstone proposal for a learner interested in careers, coding "none", MA9 absent | 3–5 proposals, ≥ 1 career-related, none requiring MA9 or code |
| T11 | Learner tries to change capstone criteria after acceptance | Requires explicit re-scoping with a recorded reason; the original snapshot is kept |
| T12 | Learner finishes day 30 with 10 concepts demonstrated | Not "completed"; enrollment stays active with the missing requirements listed; no failure state |
| T13 | Completion report generation | Snapshot stored; every line links to evidence; no blocked terms |
| T14 | Second beginner on the same installation | Can't see the first learner's apps, attempts, evidence or portfolio |
| T15 | Budget reaches 100% mid-project | Runs pause; lessons, checks and authored hints continue; clear message shown |
| T16 | Program author edits program v1.0 while a learner is enrolled | v1.1 is created; the learner stays on v1.0 unless they choose to migrate |

---

## U. Risks and Limitations

| # | Risk / limitation | Mitigation |
|---|---|---|
| U1 | **Content volume:** ~29 concepts × lessons, ~100 check items, 24 templates, milestones, authored hints | Build the 30-day program's required content first (≈ 60% of the list); AI-drafted content labeled (AIL D3); authored hints only for program projects |
| U2 | **Beginner access to a local-first platform:** the platform is designed for a single operator's machine (MA §10.5); beginners on other machines can't use it without installation or hosting | Pre-MA10: beginners use the owner's installation as separate users (or the owner learns first); hosted access is a Teacher-Mode-era decision (W9) |
| U3 | **Pre-MA9 limits for "real" apps:** no-code apps are real (they run and are evaluated) but aren't deployable software | Stated honestly in the program intro; code path and post-MA9 labs extend it |
| U4 | **External copying** (another chatbot writes the learner's work) can't be detected | Explain-back, modification challenges, variants and live reproduction; evidence reflects demonstrated adaptation, not authorship |
| U5 | **AI rubric grading** of explanations | Existing Grader safeguards (AIL §19.3); never the sole basis for DEMONSTRATED |
| U6 | **30 days is tight** for true beginners, especially on the code path | Self-paced pace; completion isn't time-bound; realistic estimates shown |
| U7 | **Cost across multiple learners** on the owner's provider keys | Per-learner budgets, cheap defaults, authored hints |
| U8 | **Over-scaffolding** makes learners passive | Ask-before-tell rule, the Hint Ladder, independence shown on the report |
| U9 | **Scope creep toward an LMS** | Teacher seams only; deferred list (§V) |
| U10 | **MA7 dependency** for workflow projects | Single-agent fallback for P3 and workflow capstones |

---

## V. Deferred Features

| Feature | Deferred until |
|---|---|
| Teacher/Creator authoring UI, cohorts, classrooms, rosters, instructor permissions | Teacher Mode (post-AIL.5; needs multi-user hosting decision) |
| Certificates and verifiable credentials | Teacher Mode + a policy decision |
| Payments, tuition, attendance, school administration | Not planned |
| Large cohort analytics | Teacher Mode |
| In-platform coding environment, shell, filesystem, browser/MCP labs | MA9 |
| Real vector search as a platform tool | MA9 |
| Additional programs (Intermediate "AI Engineer Builder", "Agent Builder") | After AIL.5 V1 feedback |
| Peer review / community showcase | Not planned for V1 |
| Voice/video lessons | Not planned |
| Gamification (streaks, XP) | Rejected (AIL §37) |

---

## W. Owner Decisions Required Before Implementation

| # | Decision | Proposed default |
|---|---|---|
| W1 | Completion rules for AI Foundations Builder | Required concepts ≥ PRACTICED; ≥ 12 DEMONSTRATED; P1–P3 + capstone; ≥ 3 experiments |
| W2 | Approve the 14 new beginner concepts and the new concept kind `skill` (CR-1) | Approve |
| W3 | Approve assistance levels H0–H5, unlock policy and the evidence caps (CR-2) | Approve |
| W4 | Default per-learner program budget and per-action confirmation threshold | Owner sets (e.g., $10 per program; confirm runs > $0.25) |
| W5 | Mentor as a Professor mode (option C) with a separate prompt version; the Grader stays separate | Approve |
| W6 | Pre-MA9: may the MA3 sandbox run tests on learner-submitted repos? | **No** by default (self-reported until MA9) unless the owner accepts the isolation level |
| W7 | Code path in V1 or no-code only? | Include the code path as optional, self-reported pre-MA9 |
| W8 | Add `progress_shared_with_author` now (default false)? | Yes |
| W9 | Who are the first beginners: the owner only, or additional users on the owner's installation? | Owner + ≤ 3 invited users as separate accounts |
| W10 | Authored vs. generated content policy for program lessons, checks and hints | Authored/reviewed for required items; generated allowed for optional items (labeled) |
| W11 | Should the program require MA7, or ship with single-agent fallbacks? | Ship with fallbacks; use MA7 when present |
| W12 | Approve the MA-level change to `artifacts` (MA-CR-1) | Approve |
| W13 | Confirm the canonical loop wording (see CR-10) | Owner to confirm |

---

## X. Conflicts with the Canonical AIL Specification and Change Requests

AIL.1–AIL.4 are **not changed by this document**. The following are requests for owner approval.

| ID | Type | Change | Why it's needed |
|---|---|---|---|
| **CR-1** | **CANONICAL AIL CHANGE REQUEST** | Add the concept kind `skill` (AIL §15.1, §18.3) with its requirement set (§I.4); add 14 beginner concepts to the seed graph (AIL §15.3) | The canonical kinds don't fit "can do" skills (JSON, prompt structure, Python basics); the canonical seed starts above complete-beginner level |
| **CR-2** | **CANONICAL AIL CHANGE REQUEST** | `learning_evidence` + `assistance_level`, `execution_verification`, `milestone_attempt_id`; new evidence types; **new integrity rules** in AIL §18.4: H5 never counts as practice; DEMONSTRATED via project evidence requires a verified item at ≤ H2 (or a later independent modification/reproduction); self-reported execution can't be deterministic evidence | Without assistance and verification, project evidence can't be defended |
| **CR-3** | **CANONICAL AIL CHANGE REQUEST** | Extend the Professor context contract (AIL §16.5): the learner's own project artifacts, app runs and test results for the **active project attempt** are readable | The canonical contract excludes task content and artifacts by default; mentoring is impossible without them |
| **CR-4** | **CANONICAL AIL CHANGE REQUEST** | Add Mentor intents and mode (AIL §16.2); add the program schedule as a planner input and `program` as a `learning_plan_items.origin` value (AIL §17) | The Coach and Mentor behaviors need to be first-class and versioned |
| **CR-5** | **CANONICAL AIL CHANGE REQUEST** | `learner_profiles` + `experience_mode`, `coding_comfort`, `career_goal_term_id`, `build_path` | Beginner Mode and capstone personalization |
| **CR-6** | **CANONICAL AIL CHANGE REQUEST** | Taxonomy vocabularies `project_family`, `career` (AIL §28.3) | Project Library facets |
| **CR-7** | **CANONICAL AIL CHANGE REQUEST** | Clarify AIL §18.3/§20.3: a learner's **own AIL-project runs count as real records** for observation requirements (they aren't demo data) | Otherwise beginners can never satisfy operational-concept observation requirements (AIL D5 limits demo data to PRACTICED) |
| **CR-8** | **CANONICAL AIL CHANGE REQUEST** | Add the Academy to the navigation (AIL §5.1) and Beginner Mode's simplified nav | Information architecture |
| **CR-9** | **CANONICAL AIL CHANGE REQUEST** | Revise AIL §40 Q4 ("single learner per installation assumed"): multiple learner accounts per installation are supported; learner-owned objects in the AIL system project are RBAC-scoped per user | Beginners are other people |
| **CR-10** | **Terminology conflict** | The review brief describes the loop as DISCOVER → VERIFY → UNDERSTAND → CONNECT → LEARN → EXPERIMENT → EVALUATE → DECIDE → APPLY → REMEMBER. The canonical spec in this project (AIL §1.2) uses the 7-step DISCOVER → UNDERSTAND → CONNECT → EXPERIMENT → EVALUATE → LEARN → APPLY | The 10-step version isn't in any project document. If it's the intended canonical wording, AIL §1.2 should be updated (no architectural impact: VERIFY ≈ Verification Level, DECIDE ≈ Triage, REMEMBER ≈ Retention) |
| **MA-CR-1** | **PLATFORM (MA) CHANGE REQUEST** | `artifacts.agent_run_id` becomes nullable; add an owner reference (`milestone_attempt_id` or `enrollment_id`) with an "exactly one owner" constraint | Learner submissions, portfolios and completion reports aren't produced by agent runs; this avoids a parallel file store |

**No conflicts found** with: provenance and the seven claim types, Verification Level, the MA8 boundary, the MA9 boundary (AIL.5 builds no runtime), cost architecture, append-only evidence, the evidence ladder states, or "no DEMONSTRATED on AI-graded evidence alone".

### X.1 AIL.5 dependencies and internal steps (one roadmap phase)

**Depends on:** AIL.1 (required) · AIL.3 (required for Weeks 2–4) · AIL.2 (optional: beginner Radar card) · AIL.4 (optional: post-program retention) · MA3, MA5, MA6 (required) · MA7 (optional, with fallbacks) · MA9 (optional, unlocks post-MA9 templates).

**Internal implementation steps (within the single AIL.5 phase):**
1. Change requests CR-1…CR-9 and MA-CR-1 applied to the canonical specs; schema for the 9 new tables
2. Project templates, milestones and the Library (facets, validation rules)
3. Milestone attempts, the hint policy engine, the Mentor mode + validator rules, copy detection
4. Build With Me workspace (no-code path: learner agents + App Test Sets)
5. Evidence extensions + the Learner State Service rules for project evidence
6. Programs, versions, schedule, enrollment, Coach (planner extension), Beginner Mode UI
7. AI Foundations Builder v1.0 content (required items first) + P1–P3 + capstone templates
8. Capstone proposals, portfolio, completion report snapshot
9. Code path (self-reported pre-MA9) + MA9-gated templates marked unavailable
10. UAT T1–T16

---

## Y. One-Page Map

```
                 PROGRAM (versioned schedule)                     PROJECT LIBRARY (versioned templates)
      AI Foundations Builder v1.0: weeks · days · items   ──uses──>  L0 exercise … L4 capstone · difficulty profile
                 │ Coach (deterministic planner)                          │ concepts taught/applied/assessed
                 ▼                                                        ▼
  TODAY ── lesson (concept version) ── check (learning item) ── BUILD WITH ME (milestone attempts)
                                                                          │ Mentor = Professor mode (asks first)
                                                                          │ Hint Ladder H1…H5 (policy engine)
                                                                          ▼
          learner app = Agent + prompt versions ──run──> MA3/MA7 ──eval──> MA6 Evaluation on App Test Set
                                                    └──compare──> MA5 / AIL.3 Experiments (model choice)
                                                                          │ execution records only
                                                                          ▼
      LEARNING EVIDENCE (append-only; + assistance level + execution verification)
                                                                          │ Grader (separate) for explain-back
                                                                          ▼
      LEARNER STATE (same engine) ──> My AI Knowledge ──> PORTFOLIO + COMPLETION REPORT (snapshot, evidence-linked)
                                                                          │
                            post-program: AIL.4 retention · AIL.2 Radar · next Track
PRE-MA9: no-code apps, workflows, experiments, self-reported local code.  POST-MA9: sandboxed coding, tests, browser/MCP labs.
```
