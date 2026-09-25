"""AIL.5C authored assessment content for the "AI Foundations Builder" program.

A deliberately small, real set (content volume is a known risk): knowledge
checks, an explain-back, a modification challenge, a project assessment, an
experiment interpretation and the capstone. Every definition is versioned and
immutable once published; a content change is a NEW version.

All challenge pools are AUTHORED here (never model-generated) and hold at least
3x the draw size. Answer keys and reference points stay server-side.

Seeding requires the Concept Graph slugs and published project templates this
content references; it refuses (listing what is missing) rather than inventing
Concepts — the same rule ``ProjectTemplateService.seed_foundation_projects`` follows.
"""

from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import AssessmentKind
from app.models.academy import ProjectTemplate
from app.services.assessment_definition_service import AssessmentDefinitionService
from app.services.concept_graph_service import ConceptGraphService


def _choice(key: str, prompt: str, options: List[str]) -> Dict:
    """First option is the correct one in this authoring shorthand; it is stored
    as an answer key (index 0) and the options are NOT reordered server-side, so
    authors should vary the position through ``answer_key`` when they need to."""
    return {"entry_key": key, "prompt": prompt, "options": options, "answer_key": [0]}


def _shuffled(entry: Dict, order: List[int]) -> Dict:
    """Re-order the options (so the right answer is not always first) and keep the key correct."""
    options = [entry["options"][i] for i in order]
    return {**entry, "options": options, "answer_key": [order.index(0)]}


_KC_STRUCTURED_OUTPUT = [
    _shuffled(_choice("so1", "What does requesting structured output (a JSON schema) change about a model's reply?", [
        "It makes the reply follow a defined shape so software can parse it",
        "It makes the facts in the reply verified", "It removes the need to test the output", "It makes the model use fewer tokens"]), [2, 0, 1, 3]),
    _shuffled(_choice("so2", "A reply parses as valid JSON and matches your schema. What does that tell you?", [
        "The shape is valid, but the content can still be wrong",
        "The content is definitely correct", "The model checked its sources", "No further checks are ever needed"]), [1, 2, 0, 3]),
    _shuffled(_choice("so3", "Why define required fields in a schema?", [
        "So missing information is detected instead of silently skipped",
        "So the model becomes more creative", "So the reply is always shorter", "So the model can browse the web"]), [3, 1, 2, 0]),
    _shuffled(_choice("so4", "Your extractor sometimes wraps the JSON in extra text. What is the most reliable fix?", [
        "Ask for a strict schema and validate the reply in code, rejecting or retrying on failure",
        "Ignore the extra text and hope it parses", "Switch to a cheaper price tier", "Add more decorative instructions"]), [0, 3, 1, 2]),
    _shuffled(_choice("so5", "Which field type suits a value that can only be one of a few known options?", [
        "An enumerated (enum) field", "A free-text string", "A very long paragraph", "A deeply nested object"]), [1, 0, 3, 2]),
    _shuffled(_choice("so6", "Why test a structured-output prompt on several different inputs?", [
        "A prompt that works on one example can still fail on others",
        "Testing changes the model's weights", "Each test makes the schema stricter", "Only the first model version needs testing"]), [2, 3, 0, 1]),
]

_KC_GROUNDING = [
    _shuffled(_choice("hg1", "What is a hallucination in an AI answer?", [
        "A fluent statement not supported by the provided sources or facts",
        "Any answer longer than a paragraph", "An answer written in another language", "A response that took too long"]), [1, 0, 2, 3]),
    _shuffled(_choice("hg2", "What does grounding an answer mean?", [
        "Tying claims to specific source material that can be checked",
        "Making the answer sound more confident", "Using shorter sentences", "Adding random citations"]), [3, 2, 0, 1]),
    _shuffled(_choice("hg3", "A model cites a source that does not exist. What does this show?", [
        "Citation-shaped text can be generated without being true, so citations must be verified",
        "The model verified the source", "The source is probably new", "Citations are always reliable"]), [2, 1, 3, 0]),
    _shuffled(_choice("hg4", "Which practice best reduces ungrounded claims in a document Q&A app?", [
        "Require answers to quote the passage they rely on and say 'not found' otherwise",
        "Tell the model to be more confident", "Hide the sources from users", "Increase the answer length"]), [0, 2, 3, 1]),
    _shuffled(_choice("hg5", "Fluent, confident wording is evidence that an answer is...", [
        "Not evidence of accuracy by itself", "Correct", "Grounded", "Verified"]), [3, 0, 1, 2]),
    _shuffled(_choice("hg6", "What should an app do when the retrieved passages do not answer the question?", [
        "Say the answer is not in the sources instead of guessing",
        "Invent a plausible answer", "Return the longest passage", "Ask the model to sound sure"]), [1, 3, 0, 2]),
]

_EXPLAIN_PROMPTS = [
    {"entry_key": "eb1", "prompt_md": "In your own words: why did you ask the model for a schema, and what is ONE thing a schema does not protect you from?",
     "reference_points": ["A schema makes the reply follow a defined shape so software can parse and validate it",
                          "A valid shape does not make the content true"]},
    {"entry_key": "eb2", "prompt_md": "Describe a case where your extractor's output was valid but wrong. How would you catch it?",
     "reference_points": ["Shape validation is separate from checking content against the source",
                          "Catches it with an evaluation over representative inputs or a source-quote check"]},
    {"entry_key": "eb3", "prompt_md": "How would you adapt your extractor for a new document type, and what would you re-test?",
     "reference_points": ["Updates the schema and prompt for the new fields",
                          "Re-tests on representative new inputs, including missing or ambiguous values"]},
]

_MOD_VARIANTS = [
    {"entry_key": "mv1", "title": "A new document",
     "statement_md": "Change your extractor so it also handles this input, then run it: `invoice #4471 total $312.50 due 2026-11-02`.",
     "parameters": {"input_text": "invoice #4471 total $312.50 due 2026-11-02"}},
    {"entry_key": "mv2", "title": "A missing field",
     "statement_md": "Make your extractor handle a missing value gracefully, then run it on: `receipt #9020 total (not shown)`.",
     "parameters": {"input_text": "receipt #9020 total (not shown)"}},
    {"entry_key": "mv3", "title": "An ambiguous value",
     "statement_md": "Make your extractor handle an ambiguous value, then run it on: `order #5533 quantity: two dozen`.",
     "parameters": {"input_text": "order #5533 quantity: two dozen"}},
]

_CAPSTONE_PROMPTS = [
    {"entry_key": f"cp{i}", "prompt_md": text, "reference_points": points}
    for i, (text, points) in enumerate(
        [
            ("Explain ONE design choice you made and why you chose it over an alternative.",
             ["Names a specific choice and a real alternative", "Justifies the choice with evidence from the project"]),
            ("Explain a second design choice, and what you would change if the inputs were very different.",
             ["Names a second, different design choice", "Reasons about how the choice depends on the inputs"]),
            ("Describe one failure you found, how you fixed it, and how you know it is fixed.",
             ["Describes a real failure from the project", "Explains a verification that the fix worked"]),
        ]
    )
]

FOUNDATION_ASSESSMENTS: List[Dict] = [
    {
        "key": "kc-structured-output", "kind": AssessmentKind.KNOWLEDGE_CHECK, "title": "Check: Structured Output",
        "concepts": ["structured-output"], "instructions_md": "Answer each question. This is a check of what you learned; the Mentor is paused while it is open.",
        "allowed_resources": ["Your lessons and notes", "The platform documentation"],
        "criteria": [{"key": "answers_correct", "label": "You answered every question correctly", "method": "deterministic", "required": True,
                      "description": "Each drawn question is checked by the platform against a reviewed answer key.",
                      "check": {"type": "choice_match", "min_correct": "all"},
                      "on_not_met": [{"kind": "learning_item", "ref": "structured-output", "label": "Revisit the Structured Output lesson"}]}],
        "challenge": {"entry_kind": "choice", "draw_size": 2, "pool": _KC_STRUCTURED_OUTPUT},
    },
    {
        "key": "kc-hallucination-grounding", "kind": AssessmentKind.KNOWLEDGE_CHECK, "title": "Check: Hallucination and Grounding",
        "concepts": ["hallucination-grounding"], "instructions_md": "Answer each question. The Mentor is paused while it is open.",
        "allowed_resources": ["Your lessons and notes"],
        "criteria": [{"key": "answers_correct", "label": "You answered every question correctly", "method": "deterministic", "required": True,
                      "check": {"type": "choice_match", "min_correct": "all"},
                      "on_not_met": [{"kind": "learning_item", "ref": "hallucination-grounding", "label": "Revisit the Hallucination and Grounding lesson"}]}],
        "challenge": {"entry_kind": "choice", "draw_size": 2, "pool": _KC_GROUNDING},
    },
    {
        "key": "eb-structured-output", "kind": AssessmentKind.EXPLAIN_BACK, "title": "Explain: Structured Output",
        "concepts": ["structured-output"], "instructions_md": "Explain your understanding in your own words. Reread your lessons and your own project if you like; AI assistants are not allowed.",
        "allowed_resources": ["Your lessons", "Your own project and results"],
        "grading": {"crosscheck": "deciding"},
        "criteria": [
            {"key": "long_enough", "label": "Your explanation has enough detail", "method": "deterministic", "required": True,
             "check": {"type": "length_bounds", "field": "fields.explanation", "min_chars": 200, "max_chars": 6000}},
            {"key": "accuracy", "label": "Your explanation is accurate", "method": "grader", "required": True,
             "description": "What structured output is and what validation adds.",
             "anchors": {"met": "Accurate and specific", "partial": "Mostly right with a gap", "not_met": "Inaccurate or missing"},
             "reference_points": ["Structured output makes a model's reply follow a defined schema so software can parse and validate it",
                                  "Validating the reply in code catches output that does not match the schema"],
             "facts": ["long_enough"],
             "on_not_met": [{"kind": "learning_item", "ref": "structured-output", "label": "Revisit the Structured Output lesson"}, {"kind": "professor", "label": "Ask the Professor to explain it another way"}]},
            {"key": "limits", "label": "You state what a schema does not guarantee", "method": "grader", "required": True,
             "anchors": {"met": "Names a real limit", "not_met": "No limit stated"},
             "reference_points": ["A valid shape does not guarantee the content is true", "Missing or ambiguous inputs still need handling"],
             "on_not_met": [{"kind": "learning_item", "ref": "structured-output", "label": "Re-read the limits section"}]},
            {"key": "own_work", "label": "You connect it to your own project", "method": "grader", "required": False,
             "reference_points": ["Refers to a real result or failure from the learner's own project"]},
        ],
        "challenge": {"entry_kind": "prompt", "draw_size": 1, "pool": _EXPLAIN_PROMPTS},
    },
    {
        "key": "mod-structured-output", "kind": AssessmentKind.MODIFICATION, "title": "Modify your extractor",
        "concepts": ["structured-output"], "instructions_md": "Adapt your own extractor to the new requirement and run it on the input shown. Your run is checked by the platform.",
        "allowed_resources": ["Your lessons", "Your own project"],
        "criteria": [
            {"key": "ran", "label": "You ran your changed extractor", "method": "deterministic", "required": True,
             "check": {"type": "run_exists_owned_terminal", "field": "run_ids", "min": 1},
             "on_not_met": [{"kind": "milestone", "ref": "p2-structured-extractor", "label": "Return to the Structured Extractor project"}]},
            {"key": "fresh_run", "label": "The run was made during this challenge", "method": "deterministic", "required": True,
             "check": {"type": "run_after_challenge_start", "field": "run_ids", "min": 1}},
            {"key": "used_input", "label": "The run used the challenge input", "method": "deterministic", "required": True,
             "check": {"type": "inputs_match_challenge", "field": "run_ids", "expected_path": "parameters.input_text"}},
        ],
        "challenge": {"entry_kind": "variant", "draw_size": 1, "pool": _MOD_VARIANTS},
    },
    {
        "key": "proj-p2-structured-extractor", "kind": AssessmentKind.PROJECT, "title": "Assess: Structured Extractor project",
        "concepts": ["prompt-structure", "structured-output", "json-basics", "evaluation"], "template_key": "p2-structured-extractor",
        "instructions_md": "Your submitted project is checked against your recorded milestones and your app's evaluation results.",
        "allowed_resources": ["Your own project"], "fresh": "if_assisted",
        "criteria": [
            {"key": "milestones", "label": "Every milestone has real evidence", "method": "deterministic", "required": True,
             "check": {"type": "milestones_evidenced", "min": "all"},
             "on_not_met": [{"kind": "milestone", "ref": "p2-structured-extractor", "label": "Finish the milestones with real results"}]},
            {"key": "evaluated", "label": "Your app was evaluated and met the criteria", "method": "deterministic", "required": True,
             "check": {"type": "evaluation_run_findings", "field": "evaluation_run_ids", "after_start": False}},
            {"key": "frozen", "label": "Your submission is unchanged", "method": "deterministic", "required": True, "check": {"type": "manifest_unchanged"}},
            {"key": "fresh_run", "label": "You ran your project during the fresh challenge", "method": "deterministic", "required": True,
             "check": {"type": "run_after_challenge_start", "field": "run_ids", "min": 1}},
            {"key": "used_input", "label": "The run used the challenge input", "method": "deterministic", "required": True,
             "check": {"type": "inputs_match_challenge", "field": "run_ids", "expected_path": "parameters.input_text"}},
        ],
        "challenge": {"entry_kind": "variant", "draw_size": 1, "pool": _MOD_VARIANTS},
    },
    {
        "key": "capstone-foundations", "kind": AssessmentKind.CAPSTONE, "title": "Capstone: Project Direction",
        "concepts": ["problem-framing-requirements", "evaluation", "model-pricing-token-economics", "privacy-responsible-ai-use"],
        "template_key": "p4-project-direction", "instructions_md": "Show that your project works, then explain and adapt it in fresh work. Assessment Mode applies to the fresh challenge and the explain-back.",
        "allowed_resources": ["Your own project and results", "Your lessons"], "grading": {"crosscheck": "always"},
        "criteria": [
            {"key": "milestones", "label": "Every milestone has real evidence", "method": "deterministic", "required": True, "check": {"type": "milestones_evidenced", "min": "all"}},
            {"key": "evaluated", "label": "Your project was evaluated and met the criteria", "method": "deterministic", "required": True,
             "check": {"type": "evaluation_run_findings", "field": "evaluation_run_ids", "after_start": False}},
            {"key": "frozen", "label": "Your submission is unchanged", "method": "deterministic", "required": True, "check": {"type": "manifest_unchanged"}},
            {"key": "readme", "label": "Your write-up has the required sections", "method": "deterministic", "required": True,
             "check": {"type": "section_present", "field": "fields.readme", "sections": ["Problem", "Evidence", "Limits"]}},
            {"key": "fresh_run", "label": "You adapted and ran your project during the challenge", "method": "deterministic", "required": True,
             "check": {"type": "run_after_challenge_start", "field": "run_ids", "min": 1}},
            {"key": "used_input", "label": "The run used the challenge input", "method": "deterministic", "required": True,
             "check": {"type": "inputs_match_challenge", "field": "run_ids", "expected_path": "parameters.input_text"}},
            {"key": "design_choices", "label": "You can explain and defend your design choices", "method": "grader", "required": True,
             "anchors": {"met": "Specific, reasoned and grounded in your project"},
             "reference_points": ["Names specific design choices with real alternatives", "Grounds the reasoning in the project's own results"]},
        ],
        # a seeded fresh modification (mandatory for a capstone) plus the fixed explain-back set
        "challenge": {"entry_kind": "variant", "draw_size": 1,
                      "pool": [{**v, "entry_key": v["entry_key"].replace("mv", "cv")} for v in _MOD_VARIANTS],
                      "fixed": _CAPSTONE_PROMPTS},
    },
]


def seed_foundation_assessments(db: Session, author_user_id: str) -> List:
    """Idempotently publish the authored set. Raises ``ValueError`` (creating
    nothing) if a referenced Concept or published project template is missing."""
    graph = ConceptGraphService(db)
    service = AssessmentDefinitionService(db)
    todo = [spec for spec in FOUNDATION_ASSESSMENTS if service.current(spec["key"]) is None]

    missing_concepts, missing_templates = set(), set()
    resolved: Dict[str, str] = {}
    templates: Dict[str, str] = {}
    for spec in todo:
        for slug in spec["concepts"]:
            concept = graph.get_concept_by_slug(slug)
            if concept is None or graph.get_current_version(concept.id) is None:
                missing_concepts.add(slug)
            else:
                resolved[slug] = concept.id
        key = spec.get("template_key")
        if key:
            template = db.execute(
                select(ProjectTemplate).where(
                    ProjectTemplate.template_key == key, ProjectTemplate.version == 1, ProjectTemplate.status == "published"
                )
            ).scalar_one_or_none()
            if template is None:
                missing_templates.add(key)
            else:
                templates[key] = template.id
    if missing_concepts or missing_templates:
        raise ValueError(
            "Missing Concept Graph slugs / published templates: "
            + ", ".join(sorted(missing_concepts | missing_templates))
        )

    created = []
    for spec in todo:
        keys = [c["key"] for c in spec["criteria"]]
        defn = service.create_draft(
            author_user_id=author_user_id, definition_key=spec["key"], kind=spec["kind"], title=spec["title"],
            instructions_md=spec["instructions_md"], criteria=spec["criteria"],
            concept_links=[{"concept_id": resolved[slug], "criterion_keys": keys} for slug in spec["concepts"]],
            challenge_spec=spec.get("challenge"), grading_policy=spec.get("grading"),
            independence_policy={"fresh_required": spec["fresh"]} if spec.get("fresh") else None,
            allowed_resources=spec.get("allowed_resources"), project_template_id=templates.get(spec.get("template_key")),
        )
        created.append(service.publish(defn.id))
    return created
