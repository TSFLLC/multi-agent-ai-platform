# AIL5D.6 — Complete Structured Learning Experience

Branch `feature/ail5d6-workspace` (base AIL5D.5 `f18a8b1`). No schema change: head stays `academy_practice_instances`.

* **Workspace** (`frontend/assets/js/workspace/`): COURSE NAV | CURRENT STEP | AI PROFESSOR on desktop; Lesson | Professor | Course tabs on phones. One step at a time; per-type renderers (teach, example, think/reveal, reflect/reflection, check incl. numeric, explain-back, guided lab, independent practice, engine-lab launcher). Learning / Practicing / Demonstrating is always labelled; "Learning complete" and "Concept demonstrated" are separate statements; the Day's AIL.5C assessment status is derived from canonical attempts/results (`AcademyStepService.assessment_status`).
* **Lab/Practice**: consumes the D5 API and handoff; return restores the exact Day, step and Practice Instance via a server-built path (`returnBanner.js`; only Academy Level 1 paths are ever followed).
* **Program Overview**: weekly grouping, learned vs demonstrated counts, up-next Day, Capstone section.
* **Conversion**: Days 2-20 (`app/academy_day_structure.py`) and Capstone 21-30 (`app/academy_capstone_structure.py`) are deterministic, verbatim mappings pinned by content-preservation tests. Exclusions: Evidence generated/requirements, knowledge-check text (bound to the existing check), Day 29 Grader rubric. Think-about-it reveals/guidance are private. Capstone artifacts carry forward per learner (`capstone_progress`).
* **UAT harness**: `scripts/ail5d6_uat_server.py` (disposable DB, real worker and execution path, only the model provider replaced by a deterministic stand-in).
