"""AIL.5D.1 test factories: a valid structured Day (all eight step types) and helpers to persist it as an ordinary
versioned Learning Item. Kept apart from conftest like ``ail1a_factories`` / ``ail5c_factories``."""

import copy

from app.db.enums import LearningItemType
from app.services.concept_graph_service import ConceptGraphService
from tests.ail1a_factories import make_concept, make_published_version

REVEAL = "SECRET-REVEAL: traditional software, the rule is exact and auditable."
CLAIM_WHY = "SECRET-WHY: confidence is not correlated with accuracy."

_MD = "Some authored teaching text."


def sample_steps():
    """Nine steps, three optional, mirroring the approved Day 1 prototype shape (structure only)."""
    return [
        {"key": "baseline", "type": "reflect", "title": "You already use AI", "estimated_minutes": 5, "required": False,
         "content": {"prompt_md": "What do you think AI is?", "min_chars": 8}},
        {"key": "what-is-ai", "type": "teach", "title": "What exactly is AI?", "estimated_minutes": 5, "required": True,
         "content": {"blocks": [
             {"kind": "md", "text": _MD},
             {"kind": "callout", "tone": "key", "text": "Rules versus learned patterns."},
             {"kind": "compare", "head": ["Traditional software", "AI system"], "rows": [["Explicit rules", "Learned patterns"]]},
             {"kind": "tabs", "items": [{"label": "Rule-based", "text": _MD}, {"label": "Machine learning", "text": _MD}]},
         ]}},
        {"key": "rules-vs-patterns", "type": "example", "title": "Sales tax vs sentiment", "estimated_minutes": 6, "required": True,
         "content": {"lead_md": "Two systems.", "cases": [
             {"tag": "Problem 1", "title": "Sales tax", "verdict": "Traditional software wins", "body_md": _MD},
             {"tag": "Problem 2", "title": "Sentiment", "verdict": "Machine learning wins", "body_md": _MD}]}},
        {"key": "bp-reminder", "type": "think", "title": "A blood-pressure reminder", "estimated_minutes": 3, "required": True,
         "content": {"scenario_md": "A hospital wants reminders.", "question_md": "Traditional software or AI? Why?"},
         "private": {"reveal_md": REVEAL}},
        {"key": "three-claims", "type": "think", "title": "Three claims", "estimated_minutes": 4, "required": True,
         "content": {"scenario_md": "Mark each claim.", "statements": [{"id": "c1", "text": "Claim one."}, {"id": "c2", "text": "Claim two."}]},
         "private": {"claims": {"c1": {"answer": "depends", "why_md": CLAIM_WHY}, "c2": {"answer": "false", "why_md": CLAIM_WHY}}}},
        {"key": "ai-or-not", "type": "check", "title": "AI or not AI?", "estimated_minutes": 5, "required": True,
         "content": {"intro_md": "Classify each scenario."}, "binding": {"kind": "item_knowledge_check"}},
        {"key": "explain-ai", "type": "explain_back", "title": "Explain AI in your own words", "estimated_minutes": 5, "required": False,
         "content": {"prompt_md": "Explain AI to a friend.", "points": [{"key": "what-it-is", "label": "What AI actually is", "hint": "Not 'it's complicated'."}]},
         "binding": {"kind": "assessment", "definition_key": "level1-day-01-explain-ai"}},
        {"key": "compare-answers", "type": "reflection", "title": "How has your answer changed?", "estimated_minutes": 3, "required": False,
         "content": {"prompt_md": "Compare.", "compare_to": "baseline"}},
        {"key": "lab-run", "type": "lab", "title": "Run it in Personal Lab", "estimated_minutes": 10, "required": True,
         "content": {"problem_md": "What decides good vs bad?", "predictions": [{"id": "p1", "text": "I predict..."}], "prompt_text": "Who won?"},
         "binding": {"kind": "personal_lab", "engine": "personal_lab_experiment"}, "private": {"authored_check_md": "Correct answer: X."}},
    ]


def sample_spec(*, day=1, kind="lab", key=None, steps=None, **over):
    spec = {
        "academy_key": key or f"level1-v2-day-{day}", "curriculum": "ail5-level1-practical-ai-foundations-v2",
        "day": day, "week": (day - 1) // 5 + 1, "kind": kind, "objectives": ["Do the thing."],
        "knowledge_check": [{"id": "q1", "prompt": "Q?", "answer": "a"}],
        "assessment_definition_key": "level1-day-01-explain-ai", "engine_binding": "personal_lab_experiment",
        "step_schema_version": 1, "steps": copy.deepcopy(steps if steps is not None else sample_steps()),
    }
    spec.update(over)
    return spec


def make_structured_item(db, *, slug="ail5d1-concept", spec=None, title="Day 1: What AI Is and Isn't", est_minutes=60):
    concept = make_concept(db, slug=slug, name=slug)
    make_published_version(db, concept=concept)
    item = ConceptGraphService(db).create_learning_item(
        concept_id=concept.id, item_type=LearningItemType.RESOURCE, title=title, body_md="legacy body",
        spec=spec if spec is not None else sample_spec(), reviewed=True, est_minutes=est_minutes,
    )
    return item


def new_version(db, item, *, spec, title=None):
    return ConceptGraphService(db).create_learning_item_version(
        lineage_id=item.lineage_id, expected_current_version=item.version, item_type=item.item_type,
        title=title or item.title, body_md=item.body_md, spec=spec, reviewed=True, est_minutes=item.est_minutes,
    )
