"""Executable Level 1 Academy orchestration.

The Academy owns schedule/content binding only.  Runs, experiments,
evaluations, grading, evidence, and state remain owned by their existing
engines.  Curriculum text is deliberately read from the authoritative
document so the learner receives the authored lecture rather than a card.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid4, uuid5
from typing import Any, Optional, List, Dict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.enums import (AssistanceLevel, AcademyProgramItemKind, AcademyProgramVersionStatus,
                          EvidenceType, EvidenceRefType, GradingMode, LearningItemType,
                          ProjectAudienceLevel, ProjectLadderLevel, ProjectTemplateBuildMode,
                          QuestionOrigin, ConceptRelationType)
from app.academy_curriculum import FOUNDATIONS_PROGRAM
from app.practical_ai_foundations_manifest import CANONICAL_FOUNDATIONS_SLUGS
from app.errors import ConflictError, NotFoundError
from app.models.academy import (AcademyProgram, AcademyProgramItem, AcademyProgramVersion,
                                 MilestoneAttempt, ProjectAttempt, ProjectMilestone,
                                 ProjectTemplate, ProjectTemplateConceptLink)
from app.models.assessment import AssessmentAttempt, AssessmentDefinition, AssessmentResult
from app.models.concepts import Concept, ConceptRelation, LearningItem
from app.models.identity import User
from app.services.concept_graph_service import ConceptGraphService
from app.services.academy_service import AcademyService
from app.services.learning_evidence_service import LearningEvidenceService
from app.services.learner_state_service import LearnerStateService
from app.services.independence_policy import counts_toward_demonstrated, counts_toward_practiced
from app.services.practical_ai_foundations_provisioning import reconcile_curriculum_relation_corrections

CANONICAL_SLUGS = CANONICAL_FOUNDATIONS_SLUGS

DAY_TITLES = {
    1: "What AI Is and Isn't", 2: "Generative AI, LLMs & Models", 3: "Tokens, Context, Inference, Hallucinations & Grounding",
    4: "AI Behavior Lab — Make the Model Succeed and Fail", 5: "Grounded vs. Ungrounded AI Experiment",
    6: "Anatomy of a Good Prompt", 7: "Context, Instructions, Examples & Structured Outputs",
    8: "Model Selection, Cost, Quality & Evaluation", 9: "Prompt Engineering Experiment",
    10: "Build a Structured Information Extractor", 11: "How Applications Call AI Models",
    12: "Embeddings, Retrieval & RAG", 13: "Memory, Tools, Agents & Workflows",
    14: "Build a Small AI Application", 15: "Build a Document Q&A / RAG System",
    16: "Designing Agents & Multi-Agent Systems", 17: "AI Evaluation, Testing & Reliability",
    18: "Human Oversight, Safety, Privacy & Production Thinking", 19: "Build an Agent Workflow",
    20: "Break, Evaluate & Improve Your AI System", 21: "Problem Selection & Requirements",
    22: "Architecture & Design", 23: "Build V1", 24: "Test V1", 25: "Diagnose Failures",
    26: "Improve / Build V2", 27: "Formal Evaluation", 28: "Explain the System",
    29: "Final Demonstration / Assessment", 30: "Professor Review + Personalized Next-Learning Plan",
}
DAY_SLUGS = {
    1: "what-ai-is-and-isnt", 2: "generative-ai-llms", 3: "tokens", 4: "what-ai-is-and-isnt", 5: "hallucination-grounding",
    6: "prompt-structure", 7: "structured-output", 8: "choosing-a-model", 9: "prompt-structure", 10: "structured-output",
    11: "how-apps-call-models", 12: "rag", 13: "workflows", 14: "how-apps-call-models", 15: "rag",
    16: "agents", 17: "evaluation", 18: "privacy-responsible-ai-use", 19: "workflows", 20: "application-evaluation",
    21: "problem-framing-requirements", 22: "problem-framing-requirements", 23: "how-apps-call-models", 24: "evaluation",
    25: "application-evaluation", 26: "iterating-debugging-ai-outputs", 27: "evaluation", 28: "application-evaluation",
    29: "application-evaluation", 30: "problem-framing-requirements",
}
LAB_DAYS = {4: "experiment", 5: "experiment", 9: "experiment", 10: "agent", 14: "agent", 15: "bounded_grounding", 19: "workflow", 20: "evaluation"}
CAPSTONE_STAGES = {21: "DEFINE", 22: "DESIGN", 23: "BUILD", 24: "TEST", 25: "DIAGNOSE", 26: "IMPROVE", 27: "EVALUATE", 28: "EXPLAIN", 29: "DEMONSTRATE", 30: "REVIEW"}
AUTHORED_EXPLAIN_BACK_DAYS = (1, 4, 5, 9, 10, 14, 15, 19, 20, 29)
EXISTING_ASSESSMENT_BINDINGS = {
    1: "level1-day-01-explain-ai", 4: "level1-day-04-experiment-reflection",
    5: "level1-day-05-grounding-reflection", 9: "level1-day-09-prompt-reflection",
    10: "eb-structured-output", 14: "level1-day-14-application-reflection",
    15: "level1-day-15-grounded-qa-reflection", 19: "level1-day-19-workflow-reflection",
    20: "level1-day-20-evaluation-reflection", 29: "capstone-foundations",
}
LEVEL1_CAPSTONE_TEMPLATE_KEY = "level1-practical-ai-foundations-capstone"


def _curriculum_text() -> str:
    path = Path(__file__).resolve().parents[2] / "docs" / "ail5-level1-practical-ai-foundations-curriculum-v2.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _day_body(day: int) -> str:
    text = _curriculum_text()
    match = re.search(rf"^### (?:DAY {day:02d}|DAY {day})[^\n]*\n", text, re.M | re.I)
    if not match:
        return f"# Day {day}\n\n{DAY_TITLES[day]}"
    end = re.search(r"^### (?:DAY \d+|WEEK |CAPSTONE|## )", text[match.end():], re.M | re.I)
    body = text[match.start(): match.end() + end.start() if end else len(text)].strip()
    # The source document includes instructor answer reveals.  They remain
    # authoritative data in the item spec, but must not be shown in the
    # learner-facing lecture before submission.
    body = re.sub(r"^\*Correct(?: answers)?:.*$", "", body, flags=re.MULTILINE)
    body = re.sub(r"^\*Correct answers:\*.*(?:\n.*?)(?=\n\n|$)", "", body, flags=re.MULTILINE | re.DOTALL)
    body = re.sub(r"^→ \(a\).*?$", "", body, flags=re.MULTILINE)
    return body.strip()


def _authored_knowledge_checks(day: int) -> List[Dict]:
    """Parse the approved KC blocks without rewriting their wording.

    The curriculum is the source of truth.  This parser intentionally only
    accepts the authored multiple-choice/answer-key shape.  Day 1's authored
    two-axis classification and Day 17's two numeric fields are represented
    explicitly because they are not ordinary A/B/C/D blocks.
    """
    body = _curriculum_text()
    day_match = re.search(rf"^### DAY {day}\b.*?(?=^### DAY |^## [A-Z]\.)", body, re.M | re.S)
    if not day_match:
        return []
    section = day_match.group(0)
    checks: List[Dict] = []
    block_re = re.compile(
        r"\*\*(KC-[^:*]+)(?:\s*\([^)]*\))?:\*\*\s*(.*?)\n(?P<rest>.*?)(?=\n\*\*KC-|\n#### Vocabulary|\n#### Evidence generated|\Z)",
        re.S,
    )
    for match in block_re.finditer(section):
        rest = match.group("rest")
        correct = re.search(r"\*Correct:\s*([A-D])\.\s*(.*?)(?:\*\s*$|\n\n)", rest, re.S)
        if not correct:
            continue
        choices = [{"key": key, "text": text.strip()} for key, text in re.findall(r"^- \(([A-D])\)\s*(.*?)\s*$", rest, re.M)]
        checks.append({
            "id": match.group(1), "prompt": match.group(2).strip(), "choices": choices,
            "answer": correct.group(1), "explanation": correct.group(2).strip(),
            "pass_criteria": "exact authored answer",
        })
    if day == 1:
        # Preserve the authored scenario text and classifications verbatim;
        # this is a two-axis deterministic activity, not invented shorthand.
        scenario_re = re.compile(
            r'\*\*Scenario (\d+):\*\*\s*"([^"]+)"\s*\n'
            r'.*?\(a\)\s*(.*?)\.\s*\(b\)\s*(.*?)(?=\n\n|\n\*\*Scenario|\Z)',
            re.S,
        )
        for match in scenario_re.finditer(section):
            number = int(match.group(1))
            system_text = match.group(3).strip().lower()
            claim_text = match.group(4).strip().lower()
            system = "traditional_software" if "traditional software" in system_text or "likely traditional" in system_text else "ai"
            claim = "overclaimed" if "overclaimed" in claim_text else "realistic"
            checks.append({
                "id": f"KC-1-classification-{number}",
                "prompt": match.group(2).strip(),
                "choices": {"system": ["ai", "traditional_software", "combination"], "claim": ["realistic", "overclaimed"]},
                "answer": {"system": system, "claim": claim},
                "explanation": "Authored Day 1 classification explanation from the curriculum.",
                "pass_criteria": "both authored classifications exact",
            })
    if day == 17:
        numeric = re.search(
            r'\*\*KC-17\.1:\*\*\s*(.*?)\n\nWhat is the model.s:\s*\n- Recall.*?\n- Precision.*?\n\n\*Correct answers:\*\s*\n- Recall.*?\n- Precision.*?\n- Interpretation:\s*(.*?)(?=\n\n|\Z)',
            section, re.S,
        )
        checks.insert(0, {
            "id": "KC-17.1", "prompt": numeric.group(1).strip() if numeric else "", 
            "choices": None, "answer": {"recall": 0.80, "precision": 0.2857142857},
            "tolerance": 0.001, "explanation": "Recall = 80/100 = 80%; precision = 80/(80+200) ≈ 28.6%.",
            "pass_criteria": "both numeric values within authored rounding tolerance",
        })
    return checks


def ensure_level1_capstone_template(db: Session, author_user_id: str, items: Dict[int, LearningItem]) -> ProjectTemplate:
    """Provision the immutable AIL.5B template for the Level 1 capstone."""
    existing = db.execute(
        select(ProjectTemplate).where(
            ProjectTemplate.template_key == LEVEL1_CAPSTONE_TEMPLATE_KEY,
            ProjectTemplate.version == 1,
            ProjectTemplate.status == "published",
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    template = ProjectTemplate(
        id=str(uuid4()), template_key=LEVEL1_CAPSTONE_TEMPLATE_KEY, version=1, status="published",
        title="Practical AI Foundations Level 1 Capstone",
        audience_level=ProjectAudienceLevel.BEGINNER, ladder_level=ProjectLadderLevel.L4,
        build_mode=ProjectTemplateBuildMode.WORKFLOW,
        brief_md="Build and demonstrate one original AI-powered system through the authored DEFINE → DESIGN → BUILD → TEST → DIAGNOSE → IMPROVE → EVALUATE → EXPLAIN → DEMONSTRATE → REVIEW sequence.",
        difficulty_profile={"academy": "practical-ai-foundations", "level": 1},
        evidence_requirements={"requires_actual_result": True, "required_stage_positions": list(range(1, 9))},
        variant_spec={"authored": True, "curriculum": "ail5-level1-practical-ai-foundations-v2"},
        est_minutes_min=600, est_minutes_max=1200, capstone_eligible=True,
        author_user_id=author_user_id, published_at=datetime.utcnow(),
    )
    db.add(template)
    db.flush()
    concept_ids = sorted({items[day].concept_id for day in range(21, 31)})
    for concept_id in concept_ids:
        db.add(ProjectTemplateConceptLink(id=str(uuid4()), project_template_id=template.id, concept_id=concept_id, role="applied"))
    stages = {
        21: ("DEFINE", "requirements", "Selected problem, target user, requirements, success criteria, and constraints."),
        22: ("DESIGN", "design", "Architecture, components, data/context flow, oversight point, and evaluation plan."),
        23: ("BUILD V1", "working_artifact_v1", "A working V1 artifact/configuration with execution provenance."),
        24: ("TEST V1", "test_results_v1", "Authored test inputs, captured outputs, and test/evaluation results."),
        25: ("DIAGNOSE", "diagnosis", "Observed failures or weaknesses with supporting evidence and a prioritized improvement."),
        26: ("IMPROVE V2", "working_artifact_v2", "Explicit V1 change, V2 artifact/configuration, rerun results, and comparison."),
        27: ("EVALUATE", "evaluation_report", "Formal evaluation against success criteria and remaining limitations."),
        28: ("EXPLAIN", "readme_and_explanation", "Architecture explanation, decisions, limitations, and assessment readiness."),
        29: ("DEMONSTRATE", "demonstration_record", "Assessment Center / Grader demonstration result; no Professor grading."),
        30: ("REVIEW", "next_learning_plan", "Professor evidence review and next-learning plan; not a grade or mastery event."),
    }
    for position, day in enumerate(range(21, 31), 1):
        stage, artifact_kind, requirement = stages[day]
        db.add(ProjectMilestone(
            id=str(uuid4()), project_template_id=template.id, position=position,
            title=f"Day {day} — {stage}", instructions_md=requirement,
            check_spec={"academy_day": day, "stage": stage, "required_artifact": artifact_kind, "requires_result": day <= 27},
            learning_item_id=items[day].id, expected_artifact_kind=artifact_kind,
            evidence_type="lab" if day <= 28 else ("project_assessment" if day == 29 else "review"),
            reference_solution_ref=f"academy/practical-ai-foundations-v2/day-{day}",
            hint_content={"academy_day": day, "stage": stage},
        ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(ProjectTemplate).where(
                ProjectTemplate.template_key == LEVEL1_CAPSTONE_TEMPLATE_KEY,
                ProjectTemplate.version == 1,
                ProjectTemplate.status == "published",
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return winner
    db.refresh(template)
    return template


class AcademyLevel1Service:
    def __init__(self, db: Session):
        self.db = db
        self.graph = ConceptGraphService(db)

    def _concept(self, slug: str) -> Concept:
        concept = self.db.execute(select(Concept).where(Concept.slug == slug)).scalar_one_or_none()
        if concept is None:
            raise ConflictError(f"Canonical concept is not provisioned: {slug}")
        return concept

    def provision(self, user: User) -> List[LearningItem]:
        """Create immutable, reviewed Academy V2 items idempotently.

        Existing rows are never edited; the academy_key marker identifies a
        previously provisioned V2 item and permits safe repeated startup/API
        calls. No concept is created here, especially no Concept 29.
        """
        created: list[LearningItem] = []
        reconcile_curriculum_relation_corrections(self.db)
        program = self.db.execute(select(AcademyProgram).where(AcademyProgram.slug == FOUNDATIONS_PROGRAM["slug"])).scalar_one_or_none()
        if program is None:
            try:
                program = AcademyService(self.db).create_program(user, slug=FOUNDATIONS_PROGRAM["slug"], title=FOUNDATIONS_PROGRAM["title"], description=FOUNDATIONS_PROGRAM["description"])
            except ConflictError:
                self.db.rollback()
                program = self.db.execute(select(AcademyProgram).where(AcademyProgram.slug == FOUNDATIONS_PROGRAM["slug"])).scalar_one_or_none()
                if program is None:
                    raise
        published = self.db.execute(select(AcademyProgramVersion).where(AcademyProgramVersion.program_id == program.id, AcademyProgramVersion.status == AcademyProgramVersionStatus.PUBLISHED).order_by(AcademyProgramVersion.version.desc())).scalars().first()
        existing_level = None
        for version in self.db.execute(select(AcademyProgramVersion).where(AcademyProgramVersion.program_id == program.id)).scalars().all():
            if (version.completion_rules or {}).get("academy_curriculum") == "ail5-level1-practical-ai-foundations-v2":
                existing_level = version
                break
        version = existing_level
        if version is None:
            version = AcademyProgramVersion(program_id=program.id, version=max((v.version for v in program.versions), default=0) + 1, status=AcademyProgramVersionStatus.DRAFT, duration_days=30, completion_rules={"academy_curriculum": "ail5-level1-practical-ai-foundations-v2", "required_threshold": "practiced"})
            self.db.add(version)
            try:
                self.db.flush()
            except IntegrityError:
                self.db.rollback()
                version = next(
                    (
                        candidate for candidate in self.db.execute(
                            select(AcademyProgramVersion).where(AcademyProgramVersion.program_id == program.id)
                        ).scalars().all()
                        if (candidate.completion_rules or {}).get("academy_curriculum") == "ail5-level1-practical-ai-foundations-v2"
                    ),
                    None,
                )
                if version is None:
                    raise
        for day, title in DAY_TITLES.items():
            slug = DAY_SLUGS[day]
            concept = self._concept(slug)
            key = f"level1-v2-day-{day}"
            item_type = LearningItemType.LAB if day in LAB_DAYS else LearningItemType.RESOURCE
            spec = {
                "academy_key": key, "curriculum": "ail5-level1-practical-ai-foundations-v2", "day": day,
                "week": (day - 1) // 5 + 1, "kind": "lab" if day in LAB_DAYS else "lecture",
                "objectives": [f"Complete the authored Day {day} learning activity and produce its evidence."],
                "sections": ["objectives", "prerequisite", "teaching_or_setup", "guided_practice", "knowledge_check", "explain_back", "recap", "evidence", "next_activity"],
                "execution": LAB_DAYS.get(day, "academy_lecture"),
                "capstone_stage": CAPSTONE_STAGES.get(day),
                "engine_binding": {4: "personal_lab_experiment", 5: "personal_lab_experiment", 9: "personal_lab_experiment", 10: "agent_version", 14: "agent_version", 15: "bounded_manual_grounding", 19: "ma7_workflow", 20: "evaluation_or_personal_lab", 29: "assessment_center_grader", 30: "professor_review"}.get(day),
                "explain_back": {"required": day in AUTHORED_EXPLAIN_BACK_DAYS, "assessment_definition_key": EXISTING_ASSESSMENT_BINDINGS.get(day), "binding_status": "bound" if day in EXISTING_ASSESSMENT_BINDINGS else ("needs_binding" if day in AUTHORED_EXPLAIN_BACK_DAYS else "not_applicable")},
                "assessment_definition_key": EXISTING_ASSESSMENT_BINDINGS.get(day),
                "capability_boundary": "real vector RAG deferred to MA9" if day == 15 else None,
                "body_source": "docs/ail5-level1-practical-ai-foundations-curriculum-v2.md",
                "knowledge_check": _authored_knowledge_checks(day),
            }
            existing = next((row for row in self.db.execute(select(LearningItem)).scalars() if (row.spec or {}).get("academy_key") == key and row.version == max(r.version for r in self.db.execute(select(LearningItem)).scalars() if (r.spec or {}).get("academy_key") == key)), None)
            if existing:
                existing.title = f"Day {day}: {title}"
                existing.body_md = _day_body(day)
                # AIL.5D.1: re-provisioning refreshes the legacy fields but must never drop authored structured steps.
                existing.spec = {**spec, **{k: existing.spec[k] for k in ("step_schema_version", "steps") if existing.spec and k in existing.spec}}
                existing.item_type = item_type
                existing.reviewed = True
                existing.est_minutes = 90 if item_type == LearningItemType.LAB else 60
                existing.grading_mode = GradingMode.DETERMINISTIC if item_type == LearningItemType.LAB else None
                self._program_item(version, day, existing, title)
                continue
            spec = {
                "academy_key": key, "curriculum": "ail5-level1-practical-ai-foundations-v2", "day": day,
                "week": (day - 1) // 5 + 1, "kind": "lab" if day in LAB_DAYS else "lecture",
                "objectives": [f"Complete the authored Day {day} learning activity and produce its evidence."],
                "sections": ["objectives", "prerequisite", "teaching_or_setup", "guided_practice", "knowledge_check", "explain_back", "recap", "evidence", "next_activity"],
                "execution": LAB_DAYS.get(day, "academy_lecture"),
                "capstone_stage": CAPSTONE_STAGES.get(day),
                "engine_binding": {4: "personal_lab_experiment", 5: "personal_lab_experiment", 9: "personal_lab_experiment", 10: "agent_version", 14: "agent_version", 15: "bounded_manual_grounding", 19: "ma7_workflow", 20: "evaluation_or_personal_lab", 29: "assessment_center_grader", 30: "professor_review"}.get(day),
                "explain_back": {"required": day in AUTHORED_EXPLAIN_BACK_DAYS, "assessment_definition_key": EXISTING_ASSESSMENT_BINDINGS.get(day), "binding_status": "bound" if day in EXISTING_ASSESSMENT_BINDINGS else ("needs_binding" if day in AUTHORED_EXPLAIN_BACK_DAYS else "not_applicable")},
                "assessment_definition_key": EXISTING_ASSESSMENT_BINDINGS.get(day),
                "capability_boundary": "real vector RAG deferred to MA9" if day == 15 else None,
                "body_source": "docs/ail5-level1-practical-ai-foundations-curriculum-v2.md",
                "knowledge_check": _authored_knowledge_checks(day),
            }
            lineage_id = str(uuid5(NAMESPACE_URL, f"practical-ai-foundations:{key}"))
            try:
                v1 = self.graph.create_learning_item(concept_id=concept.id, item_type=item_type, title=f"Day {day}: {title} (historical seed)", body_md=None, spec={"academy_seed": key}, grading_mode=None, reviewed=True, est_minutes=60, lineage_id=lineage_id)
                item = self.graph.create_learning_item_version(lineage_id=v1.lineage_id, expected_current_version=v1.version, item_type=item_type, title=f"Day {day}: {title}", body_md=_day_body(day), spec=spec, grading_mode=GradingMode.DETERMINISTIC if item_type == LearningItemType.LAB else None, reviewed=True, est_minutes=90 if item_type == LearningItemType.LAB else 60)
            except IntegrityError:
                self.db.rollback()
                item = self.graph.get_current_learning_item(lineage_id)
                if item is not None and item.version == 1 and (item.spec or {}).get("academy_seed") == key:
                    item = self.graph.create_learning_item_version(lineage_id=lineage_id, expected_current_version=1, item_type=item_type, title=f"Day {day}: {title}", body_md=_day_body(day), spec=spec, grading_mode=GradingMode.DETERMINISTIC if item_type == LearningItemType.LAB else None, reviewed=True, est_minutes=90 if item_type == LearningItemType.LAB else 60)
                if item is None or (item.spec or {}).get("academy_key") != key:
                    raise
            created.append(item)
            self._program_item(version, day, item, title)
        ensure_level1_capstone_template(
            self.db, user.id, {row.spec["day"]: row for row in self._current_academy_items()}
        )
        from app.assessment_curriculum import seed_level1_capstone_assessment, seed_level1_explain_back_assessments
        capstone_template = self.db.execute(
            select(ProjectTemplate).where(
                ProjectTemplate.template_key == LEVEL1_CAPSTONE_TEMPLATE_KEY,
                ProjectTemplate.version == 1,
                ProjectTemplate.status == "published",
            )
        ).scalar_one()
        seed_level1_capstone_assessment(self.db, user.id, capstone_template.id)
        seed_level1_explain_back_assessments(self.db, user.id)
        from app.services.academy_level1_fixtures import ensure_day10, ensure_day14, ensure_day15, ensure_day19, ensure_day20
        current_items = {row.spec["day"]: row for row in self._current_academy_items()}
        ensure_day10(self.db, user, current_items[10])
        ensure_day14(self.db, user, current_items[14])
        ensure_day15(self.db, user, current_items[15])
        ensure_day19(self.db, user, current_items[19])
        ensure_day20(self.db, user, current_items[20])
        # Reconciliation also runs against an already-published Level 1
        # version.  Persist authored-content repairs before returning; the
        # request transaction must not roll them back merely because there is
        # no draft version to publish.
        was_draft = version.status == AcademyProgramVersionStatus.DRAFT
        self.db.commit()
        if was_draft:
            AcademyService(self.db).publish_version(user, version.id)
        return created

    def validate_level1_contract(self) -> dict:
        """Validate the persisted authored Level 1 contract, not row counts."""
        items = self._current_academy_items()
        if len(items) != 30 or {row.spec.get("day") for row in items} != set(range(1, 31)):
            raise ConflictError("Level 1 requires exactly 30 current Academy days")
        if sum(len((row.spec or {}).get("knowledge_check", [])) for row in items) != 34:
            raise ConflictError("Level 1 authored knowledge-check bank is incomplete")
        canonical_present = {
            slug for slug, in self.db.execute(
                select(Concept.slug).where(Concept.slug.in_(CANONICAL_SLUGS))
            ).all()
        }
        if canonical_present != set(CANONICAL_SLUGS) or not {row.concept.slug for row in items}.issubset(canonical_present):
            raise ConflictError("Level 1 is not bound to the canonical 28 concepts")
        agents = self._concept("agents")
        workflows = self._concept("workflows")
        related = self.db.execute(select(ConceptRelation).where(
            ConceptRelation.from_concept_id == agents.id,
            ConceptRelation.to_concept_id == workflows.id,
            ConceptRelation.relation_type == ConceptRelationType.RELATED,
            ConceptRelation.label == "co_taught_with",
        )).scalar_one_or_none()
        prerequisite = self.db.execute(select(ConceptRelation).where(
            ConceptRelation.from_concept_id == agents.id,
            ConceptRelation.to_concept_id == workflows.id,
            ConceptRelation.relation_type == ConceptRelationType.PREREQUISITE,
        )).scalar_one_or_none()
        if related is None or prerequisite is not None:
            raise ConflictError("agents -> workflows must be RELATED/co_taught_with only")
        # ``eb-structured-output`` is an existing AIL.5C definition owned by
        # the platform assessment seed; the Level 1 provisioner binds to it
        # but does not recreate it.  All other Level 1 definitions are seeded
        # by this curriculum provisioner.
        required_definitions = set(EXISTING_ASSESSMENT_BINDINGS.values()) - {"eb-structured-output"}
        present_definitions = {
            key for key, in self.db.execute(select(AssessmentDefinition.definition_key).where(
                AssessmentDefinition.definition_key.in_(required_definitions),
                AssessmentDefinition.status == "published",
            )).all()
        }
        if present_definitions != required_definitions:
            raise ConflictError("Level 1 Assessment Definitions are incomplete")
        template = self.db.execute(select(ProjectTemplate).where(
            ProjectTemplate.template_key == LEVEL1_CAPSTONE_TEMPLATE_KEY,
            ProjectTemplate.version == 1,
            ProjectTemplate.status == "published",
        )).scalar_one_or_none()
        if template is None or self.db.query(ProjectMilestone).filter_by(project_template_id=template.id).count() != 10:
            raise ConflictError("Level 1 capstone template/milestones are incomplete")
        milestones = self.db.query(ProjectMilestone).filter_by(project_template_id=template.id).all()
        if {m.check_spec.get("academy_day") for m in milestones} != set(range(21, 31)):
            raise ConflictError("Level 1 capstone stages are incomplete")

        from app.models.tasks import Task
        from app.models.workflow import Workflow, WorkflowVersion
        tasks = self.db.query(Task).all()
        requirements = [task.requirements or {} for task in tasks]
        fixture_keys = {req.get("academy_fixture") for req in requirements}
        required_fixtures = {
            "level1-day10-structured-extraction", "level1-day14-small-ai-application",
            "level1-day15-bounded-grounding", "level1-day19-agent-handoff-workflow",
            "level1-day20-broken-system-evaluation",
        }
        if not required_fixtures.issubset(fixture_keys):
            raise ConflictError("Level 1 governed fixture definitions are incomplete")
        if self.db.query(ProjectTemplate).filter_by(template_key="level1-day14-small-ai-application", version=1, status="published").count() != 1:
            raise ConflictError("Day 14 Build With Me template is incomplete")
        workflow = self.db.query(Workflow).filter_by(name="Level 1 Agent Handoff Workflow").one_or_none()
        if workflow is None or self.db.query(WorkflowVersion).filter_by(workflow_id=workflow.id, status="active").count() < 1:
            raise ConflictError("Day 19 MA7 workflow definition is incomplete")
        if not any(req.get("academy_fixture") == "level1-day15-bounded-grounding" and req.get("not_vector_rag") is True for req in requirements):
            raise ConflictError("Day 15 bounded grounding definition is incomplete")
        if not any(req.get("academy_fixture") == "level1-day20-broken-system-evaluation" and req.get("requires_baseline_and_improved") is True for req in requirements):
            raise ConflictError("Day 20 evaluation definition is incomplete")

        program_version = next(
            (
                candidate for candidate in self.db.execute(
                    select(AcademyProgramVersion).where(
                        AcademyProgramVersion.status == AcademyProgramVersionStatus.PUBLISHED
                    )
                ).scalars().all()
                if (candidate.completion_rules or {}).get("academy_curriculum") == "ail5-level1-practical-ai-foundations-v2"
            ),
            None,
        )
        if program_version is not None:
            program_items = list(self.db.execute(select(AcademyProgramItem).where(
                AcademyProgramItem.program_version_id == program_version.id
            )).scalars())
            AcademyService(self.db)._validate_items(program_version, program_items)
        return {"days": 30, "canonical_concepts": 28, "knowledge_checks": 34,
                "assessment_definitions": len(present_definitions), "capstone_milestones": 10,
                "agents_workflows": "related:co_taught_with"}

    def _program_item(self, version, day: int, item: LearningItem, title: str) -> None:
        existing = self.db.execute(select(AcademyProgramItem).where(AcademyProgramItem.program_version_id == version.id, AcademyProgramItem.day == day, AcademyProgramItem.learning_item_id == item.id)).scalar_one_or_none()
        if existing:
            return
        self.db.add(AcademyProgramItem(program_version_id=version.id, week=(day - 1) // 5 + 1, day=day, module_key=f"level-1-week-{(day - 1) // 5 + 1}", position=day - 1, item_kind=AcademyProgramItemKind.LEARNING_ITEM, concept_id=item.concept_id, learning_item_id=item.id, required=True, estimated_minutes=item.est_minutes, purpose_text=title, completion_requirement={"evidence": "authored_activity"}))

    def item(self, item_id: str) -> LearningItem:
        item = self.db.get(LearningItem, item_id)
        if not item or not (item.spec or {}).get("academy_key") or item.version != self._current_version_for_key(item.spec["academy_key"]):
            raise NotFoundError("Academy learning item not found")
        return item

    def _current_version_for_key(self, key: str) -> int:
        return max(
            (row.version for row in self.db.execute(select(LearningItem)).scalars() if (row.spec or {}).get("academy_key") == key),
            default=0,
        )

    def _current_academy_items(self) -> list[LearningItem]:
        rows = [row for row in self.db.execute(select(LearningItem)).scalars() if (row.spec or {}).get("curriculum") == "ail5-level1-practical-ai-foundations-v2"]
        current: dict[str, LearningItem] = {}
        for row in sorted(rows, key=lambda value: (value.spec.get("academy_key", ""), value.version, value.id)):
            current[row.spec["academy_key"]] = row
        return sorted(current.values(), key=lambda row: row.spec["day"])

    def days(self, user_id: str) -> list[dict[str, Any]]:
        items = self._current_academy_items()
        evidence = LearningEvidenceService(self.db).list_evidence(user_id)
        by_item = {e.learning_item_id for e in evidence}
        return [{"day": i.spec["day"], "week": i.spec["week"], "title": i.title, "kind": i.spec["kind"], "item_id": i.id, "estimated_minutes": i.est_minutes, "state": LearnerStateService(self.db).state(user_id, i.concept_id).ladder, "evidence_earned": i.id in by_item, "next": i.spec["day"] + 1 if i.spec["day"] < 30 else None, "capstone_stage": i.spec.get("capstone_stage")} for i in sorted(items, key=lambda x: x.spec["day"])]

    def expose(self, user_id: str, item_id: str):
        item = self.item(item_id); version = self.graph.get_current_version(item.concept_id)
        existing = [e for e in LearningEvidenceService(self.db).list_evidence(user_id, concept_id=item.concept_id) if e.learning_item_id == item.id and e.evidence_type == EvidenceType.LESSON_COMPLETED]
        if existing: return existing[0]
        return LearningEvidenceService(self.db).record_evidence(user_id=user_id, concept_id=item.concept_id, concept_version_id=version.id, learning_item_id=item.id, evidence_type=EvidenceType.LESSON_COMPLETED, grader=GradingMode.SELF, passed=True)

    def graduation(self, user_id: str) -> dict:
        states = {slug: LearnerStateService(self.db).state(user_id, self._concept(slug).id) for slug in CANONICAL_SLUGS}
        missing_concepts = [slug for slug, state in states.items() if not state.is_at_least("understood")]
        evidence = LearningEvidenceService(self.db).list_evidence(user_id)
        items = self._current_academy_items()
        by_day = {i.spec["day"]: i.id for i in items}
        day_by_item = {item_id: day for day, item_id in by_day.items()}
        milestone_rows = self.db.execute(
            select(ProjectMilestone.learning_item_id, MilestoneAttempt.id)
            .join(MilestoneAttempt, MilestoneAttempt.project_milestone_id == ProjectMilestone.id)
            .join(ProjectAttempt, ProjectAttempt.id == MilestoneAttempt.project_attempt_id)
            .where(
                ProjectAttempt.user_id == user_id,
                ProjectAttempt.is_capstone.is_(True),
                ProjectMilestone.learning_item_id.in_([by_day.get(day) for day in range(21, 29)]),
            )
        ).all()
        capstone_milestones = {
            day_by_item[item_id]: milestone_id
            for item_id, milestone_id in milestone_rows
            if item_id in day_by_item
        }
        capstone_milestone_ids = {
            milestone_id for milestone_id in capstone_milestones.values()
        }
        capstone_assessment_attempt_ids = {
            attempt_id
            for (attempt_id,) in self.db.execute(
                select(AssessmentAttempt.id)
                .join(AssessmentDefinition, AssessmentDefinition.id == AssessmentAttempt.definition_id)
                .where(
                    AssessmentAttempt.user_id == user_id,
                    AssessmentDefinition.definition_key == "capstone-foundations",
                )
            ).all()
        }
        capstone_assessment_result_ids = {
            result_id
            for (result_id,) in self.db.execute(
                select(AssessmentResult.id)
                .where(AssessmentResult.attempt_id.in_(capstone_assessment_attempt_ids))
            ).all()
        } if capstone_assessment_attempt_ids else set()
        missing_days = [
            day for day in LAB_DAYS
            if not any(
                e.learning_item_id == by_day.get(day)
                and e.evidence_type in (EvidenceType.LAB, EvidenceType.OBSERVATION)
                and e.ref_type == EvidenceRefType.EXPERIMENT
                and e.passed
                and e.assistance_level is not None
                and counts_toward_practiced(e)
                for e in evidence
            )
        ]
        project_evidence = [
            e for e in evidence
            if e.passed and e.evidence_type == EvidenceType.LAB
            and e.milestone_attempt_id is not None
            and e.milestone_attempt_id in capstone_milestone_ids
            and e.assistance_level is not None
            and counts_toward_demonstrated(e)
        ]
        assessment_evidence = [
            e for e in evidence
            if e.passed and e.evidence_type == EvidenceType.PROJECT_ASSESSMENT
            and e.ref_type == EvidenceRefType.ASSESSMENT_RESULT
            and e.ref_id in capstone_assessment_result_ids
            and e.assistance_level is not None
            and counts_toward_demonstrated(e)
        ]
        capstone_days = [
            day for day, milestone_attempt_id in capstone_milestones.items()
            if any(e.milestone_attempt_id == milestone_attempt_id for e in project_evidence)
        ]
        if assessment_evidence:
            capstone_days.append(29)
        capstone_complete = all(day in capstone_days for day in range(21, 29)) and bool(assessment_evidence)
        missing_capstone = [day for day in range(21, 30) if day not in capstone_days]
        return {"eligible": not missing_concepts and not missing_days and capstone_complete, "required_concepts_complete": not missing_concepts, "required_hands_on_complete": not missing_days, "capstone_complete": capstone_complete, "missing_concepts": missing_concepts, "missing_days": missing_days, "missing_capstone_stages": missing_capstone, "note": "Eligibility is derived from canonical evidence and assessment results, never lesson completion percentage. Day 30 reviews existing evidence and plans next learning; it is not another grading event."}

    def review(self, user_id: str) -> dict:
        """Evidence-only Day 30 review; it never writes grades or mastery."""
        states = {slug: LearnerStateService(self.db).state(user_id, self._concept(slug).id) for slug in CANONICAL_SLUGS}
        strengths = [slug for slug, state in states.items() if state.ladder in ("practiced", "demonstrated")]
        revisit = [slug for slug, state in states.items() if state.ladder in ("understood", "practiced")]
        recommendations = [f"Revisit {slug} with a fresh hands-on activity." for slug in revisit]
        from app.models.assessment import AssessmentResult
        result_count = self.db.query(AssessmentResult).filter(AssessmentResult.user_id == user_id).count()
        return {"day": 30, "strengths": strengths, "revisit": revisit, "recommendations": recommendations, "assessment_results_consulted": result_count}

    def knowledge_check(self, user_id: str, item_id: str, answers: dict):
        """Run only authored deterministic questions.

        The answer key lives in the reviewed V2 item snapshot and is never
        returned by the read endpoint.  No Professor call or direct state
        mutation occurs here; the canonical evidence service is the sole write
        path.
        """
        item = self.item(item_id)
        questions = (item.spec or {}).get("knowledge_check", [])
        if not questions:
            raise ConflictError("This Academy item has no authored knowledge check")
        results = []
        for question in questions:
            key = str(question["id"])
            expected = question.get("answer")
            actual = answers.get(key)
            if isinstance(expected, dict) and isinstance(actual, dict):
                tolerance = float(question.get("tolerance", 0))
                passed_item = all(
                    (abs(float(actual.get(name)) - float(value)) <= tolerance)
                    if isinstance(value, (int, float)) and tolerance
                    else actual.get(name) == value
                    for name, value in expected.items()
                )
            else:
                passed_item = actual == expected
            results.append({"id": key, "passed": passed_item})
        passed = all(row["passed"] for row in results)
        version = self.graph.get_current_version(item.concept_id)
        evidence = LearningEvidenceService(self.db).record_evidence(
            user_id=user_id, concept_id=item.concept_id, concept_version_id=version.id,
            learning_item_id=item.id, evidence_type=EvidenceType.KNOWLEDGE_CHECK,
            grader=GradingMode.DETERMINISTIC, passed=passed,
            score={"raw": sum(r["passed"] for r in results), "max": len(results), "items": results},
            question_origin=QuestionOrigin.REVIEWED,
        )
        return evidence, results, LearnerStateService(self.db).state(user_id, item.concept_id)
