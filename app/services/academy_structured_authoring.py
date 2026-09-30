"""AIL.5D.3 — explicit, versioned authoring of a structured Day.

A Day becomes structured only through this operation, never through routine provisioning. It publishes the structured
Day as the next immutable ``LearningItem`` version in the same lineage (``create_learning_item_version``), so:

* the legacy version stays in history, untouched;
* authored content is versioned like every other Learning Item, with no second content store;
* learner progress and responses recorded against the old version are never rewritten (unchanged steps carry over by
  fingerprint);
* the operation is idempotent: if the current version already equals the built structure, nothing is written.

Only Day 1 has a structural mapping so far (``app.academy_day1_structure``); every other Day reports that plainly.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict

from sqlalchemy import update

from app.academy_day1_structure import CONVERTER, build_day1_structure, legacy_body_md
from app.academy_steps import STEP_SCHEMA_VERSION, is_structured
from app.errors import ConflictError, NotFoundError
from app.models.academy import AcademyProgramItem
from app.services.academy_level1_service import AcademyLevel1Service, _curriculum_text
from app.services.concept_graph_service import ConceptGraphService

AUTHORED_DAYS = (1,)


def author_day_structure(db, day: int) -> Dict[str, Any]:
    if day not in AUTHORED_DAYS:
        raise ConflictError("This Day has no structured authoring yet.", detail={"code": "not_authored", "authored_days": list(AUTHORED_DAYS)})
    level1 = AcademyLevel1Service(db)
    current = next((row for row in level1._current_academy_items() if row.spec["day"] == day), None)
    if current is None:
        raise NotFoundError("Academy Level 1 day is not provisioned")

    built = build_day1_structure(_curriculum_text())
    spec = copy.deepcopy(current.spec or {})
    spec.update(
        objectives=built["objectives"], step_schema_version=STEP_SCHEMA_VERSION, steps=built["steps"],
        authoring={"converter": CONVERTER, "source": "docs/ail5-level1-practical-ai-foundations-curriculum-v2.md", "source_sha256": built["source_sha256"]},
    )
    questions = []
    for question in spec.get("knowledge_check", []):
        question = dict(question)
        match = re.fullmatch(r"KC-1-classification-(\d+)", str(question.get("id")))
        if match:
            question["explanation"] = built["explanations"][int(match.group(1))]
        questions.append(question)
    spec["knowledge_check"] = questions
    body = legacy_body_md(built["steps"], built["objectives"])

    if is_structured(current.spec) and current.spec == spec and current.body_md == body:
        return {"day": day, "authored": False, "item_id": current.id, "version": current.version, "steps": len(built["steps"])}

    graph = ConceptGraphService(db)
    new = graph.create_learning_item_version(
        lineage_id=current.lineage_id, expected_current_version=current.version, item_type=current.item_type, title=current.title,
        body_md=body, spec=spec, grading_mode=current.grading_mode, reviewed=True, est_minutes=current.est_minutes,
        requires_capability_term_id=current.requires_capability_term_id,
    )
    # The published program schedules this Day by item id; point it at the new version of the same lineage.
    db.execute(update(AcademyProgramItem).where(AcademyProgramItem.learning_item_id == current.id).values(learning_item_id=new.id))
    db.commit()
    return {"day": day, "authored": True, "item_id": new.id, "version": new.version, "previous_item_id": current.id, "steps": len(built["steps"])}
