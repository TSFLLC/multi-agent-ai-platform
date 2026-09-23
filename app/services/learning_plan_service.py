"""Learning Plan Service — AIL.1A, docs/ail-learning-spec-v1.md Sec 17.

Deterministic planning only (AIL architecture reconciliation, frozen
contract #9): given the same profile, interests, graph and evidence, the
same ordered plan comes out every time — no LLM involvement anywhere in
this module. Replanning never mutates or removes an already-``PLANNED``
(user-accepted) ``learning_plan_items`` row; it only ever proposes new
``PROPOSED`` rows for concepts the plan doesn't already cover, returned
alongside a diff so a caller can decide what to accept (spec Sec 17.4:
"the planner never overwrites user edits").

Radar may later PROPOSE plan changes (``origin=radar`` once AIL.2A's
``developments`` table exists — not this slice, frozen contract #6); it
can never write directly into ``state=planned``.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set

from sqlalchemy import select

from app.db.enums import ConceptLevel, ConceptRelationType, LearnerDepth, PlanItemOrigin, PlanItemState
from app.errors import NotFoundError
from app.models.concepts import Concept, ConceptRelation, ConceptTerm
from app.models.learner import LearningPlanItem
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_profile_service import LearnerProfileService
from app.services.learner_state_service import DEMONSTRATED, LearnerStateService

_DEPTH_ALLOWED_LEVELS: Dict[LearnerDepth, Set[ConceptLevel]] = {
    LearnerDepth.SURVEY: {ConceptLevel.FOUNDATIONAL},
    LearnerDepth.WORKING: {ConceptLevel.FOUNDATIONAL, ConceptLevel.PRACTITIONER},
    LearnerDepth.DEEP: {ConceptLevel.FOUNDATIONAL, ConceptLevel.PRACTITIONER, ConceptLevel.ADVANCED},
}
_LEVEL_ORDER = {ConceptLevel.FOUNDATIONAL: 0, ConceptLevel.PRACTITIONER: 1, ConceptLevel.ADVANCED: 2}


@dataclass
class PlannedConcept:
    concept_id: str
    position: int
    reason: str


@dataclass
class PlanDiff:
    added: List[str] = field(default_factory=list)
    already_planned: List[str] = field(default_factory=list)
    no_longer_candidate: List[str] = field(default_factory=list)
    new_items: List[LearningPlanItem] = field(default_factory=list)


class LearningPlanService:
    def __init__(self, db):
        self.db = db
        self._concepts = ConceptGraphService(db)
        self._profiles = LearnerProfileService(db)
        self._state = LearnerStateService(db)

    # -- Deterministic plan generation (spec Sec 17.3) -----------------------

    def generate_plan(self, user_id: str) -> List[PlannedConcept]:
        profile = self._profiles.get_profile(user_id)
        depth = profile.depth if profile and profile.depth else LearnerDepth.WORKING
        allowed_levels = _DEPTH_ALLOWED_LEVELS[depth]

        goal_ids = self._goal_concept_ids(user_id)
        filtered = self._filter_by_depth(goal_ids, allowed_levels)

        closure: Set[str] = set(filtered)
        for concept_id in filtered:
            closure |= self._concepts.get_prerequisite_closure(concept_id)

        states = self._state.states_for_concepts(user_id, list(closure))
        todo = {cid for cid in closure if states[cid].ladder != DEMONSTRATED}
        if not todo:
            return []

        order = self._topological_order(todo, user_id, goal_ids)
        return [
            PlannedConcept(concept_id=cid, position=i, reason=self._reason(cid, goal_ids))
            for i, cid in enumerate(order)
        ]

    def _goal_concept_ids(self, user_id: str) -> Set[str]:
        interests = self._profiles.list_interests(user_id)
        goal_ids: Set[str] = {i.concept_id for i in interests if i.concept_id is not None}

        track_term_ids = {i.term_id for i in interests if i.term_id is not None}
        if track_term_ids:
            stmt = select(ConceptTerm.concept_id).where(ConceptTerm.term_id.in_(track_term_ids))
            goal_ids |= set(self.db.execute(stmt).scalars().all())
        return goal_ids

    def _filter_by_depth(self, concept_ids: Set[str], allowed_levels: Set[ConceptLevel]) -> Set[str]:
        if not concept_ids:
            return set()
        stmt = select(Concept).where(Concept.id.in_(concept_ids))
        concepts = self.db.execute(stmt).scalars().all()
        filtered = set()
        for concept in concepts:
            if concept.level in allowed_levels:
                filtered.add(concept.id)
            elif concept.level == ConceptLevel.PRACTITIONER and concept.is_core:
                # spec Sec 17.3: survey = foundational + *core* practitioner.
                filtered.add(concept.id)
        return filtered

    def _topological_order(self, todo: Set[str], user_id: str, goal_ids: Set[str]) -> List[str]:
        in_degree = {cid: 0 for cid in todo}
        for cid in todo:
            for prereq in self._concepts.get_prerequisites(cid):
                if prereq in todo:
                    in_degree[cid] += 1

        concepts_by_id = {
            c.id: c for c in self.db.execute(select(Concept).where(Concept.id.in_(todo))).scalars()
        }
        interests = {i.concept_id for i in self._profiles.list_interests(user_id) if i.concept_id}

        def sort_key(cid: str):
            concept = concepts_by_id[cid]
            return (
                0 if cid in interests else 1,
                _LEVEL_ORDER[concept.level],
                concept.name,
            )

        ready = sorted([cid for cid, deg in in_degree.items() if deg == 0], key=sort_key)
        order: List[str] = []
        remaining_edges = {cid: [c for c in self._concepts_that_depend_on(cid, todo)] for cid in todo}

        while ready:
            current = ready.pop(0)
            order.append(current)
            newly_ready = []
            for dependent in remaining_edges.get(current, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    newly_ready.append(dependent)
            ready = sorted(ready + newly_ready, key=sort_key)

        return order

    def _concepts_that_depend_on(self, concept_id: str, todo: Set[str]) -> List[str]:
        stmt = select(ConceptRelation.to_concept_id).where(
            ConceptRelation.from_concept_id == concept_id,
            ConceptRelation.relation_type == ConceptRelationType.PREREQUISITE,
        )
        dependents = self.db.execute(stmt).scalars().all()
        return [d for d in dependents if d in todo]

    @staticmethod
    def _reason(concept_id: str, goal_ids: Set[str]) -> str:
        return (
            "in your declared goals/interests"
            if concept_id in goal_ids
            else "a prerequisite for a goal concept"
        )

    # -- Replan / accept / reject (spec Sec 17.4, 17.6) ----------------------

    def get_plan(self, user_id: str) -> List[LearningPlanItem]:
        stmt = (
            select(LearningPlanItem)
            .where(LearningPlanItem.user_id == user_id, LearningPlanItem.state == PlanItemState.PLANNED)
            .order_by(LearningPlanItem.position)
        )
        return list(self.db.execute(stmt).scalars().all())

    def replan(self, user_id: str) -> PlanDiff:
        candidate = self.generate_plan(user_id)
        existing = list(
            self.db.execute(
                select(LearningPlanItem).where(
                    LearningPlanItem.user_id == user_id,
                    LearningPlanItem.state.in_([PlanItemState.PROPOSED, PlanItemState.PLANNED]),
                )
            )
            .scalars()
            .all()
        )
        existing_concept_ids = {item.concept_id for item in existing}
        planned_concept_ids = {item.concept_id for item in existing if item.state == PlanItemState.PLANNED}

        diff = PlanDiff()
        next_position = (max((item.position for item in existing), default=-1)) + 1

        for planned in candidate:
            if planned.concept_id in planned_concept_ids:
                diff.already_planned.append(planned.concept_id)
                continue
            if planned.concept_id in existing_concept_ids:
                continue
            item = LearningPlanItem(
                user_id=user_id,
                concept_id=planned.concept_id,
                position=next_position,
                state=PlanItemState.PROPOSED,
                origin=PlanItemOrigin.PLANNER,
            )
            self.db.add(item)
            diff.added.append(planned.concept_id)
            diff.new_items.append(item)
            next_position += 1

        candidate_ids = {p.concept_id for p in candidate}
        for item in existing:
            if item.state == PlanItemState.PROPOSED and item.concept_id not in candidate_ids:
                diff.no_longer_candidate.append(item.concept_id)

        self.db.commit()
        for item in diff.new_items:
            self.db.refresh(item)
        return diff

    def accept_item(self, user_id: str, item_id: str) -> LearningPlanItem:
        item = self._get_own_item(user_id, item_id)
        item.state = PlanItemState.PLANNED
        self.db.commit()
        self.db.refresh(item)
        return item

    def reject_item(self, user_id: str, item_id: str) -> LearningPlanItem:
        item = self._get_own_item(user_id, item_id)
        item.state = PlanItemState.REJECTED
        self.db.commit()
        self.db.refresh(item)
        return item

    def _get_own_item(self, user_id: str, item_id: str) -> LearningPlanItem:
        item = self.db.get(LearningPlanItem, item_id)
        if item is None or item.user_id != user_id:
            raise NotFoundError(f"LearningPlanItem {item_id} not found for user {user_id}")
        return item
