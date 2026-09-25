"""AIL.5C dry-run: which learner-concept states change under the corrected
Learner State rules?

    python -m scripts.ail5c_learner_state_diff PATH_TO_DB_COPY [--include-unchanged]

The DB is COPIED to a temporary file first and the copy is opened; the
original is never opened for writing and nothing is ever written back. Point
this at a copy of a real database (or a backup) to see, before merge, which
(user, concept) ladders / overlays would differ between:

* legacy rules  — assistance, verification, demo data and supersession ignored
                  for DEMONSTRATED; CHANGED is a single-hop check;
* corrected rules — the AIL.5C platform floor, supersession, v2 constraints and
                  multi-hop CHANGED.

Read-only, deterministic, prints a table. Never run against a live database.
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.db.enums import EvidenceType, GradingMode, QuestionOrigin
from app.models.learner import LearningEvidence
from app.services.learner_state_service import CHANGED, DEMONSTRATED, LearnerStateService


class LegacyLearnerStateService(LearnerStateService):
    """The pre-AIL.5C rules, kept here only so the diff can be computed."""

    def state(self, user_id, concept_id, *, now=None):
        result = super().state(user_id, concept_id, now=now)
        evidence = result.evidence
        graded = [e for e in evidence if e.evidence_type != EvidenceType.SELF_REPORT]
        version = self._concepts.get_current_version(concept_id)
        ladder = result.ladder
        overlays = set(result.overlays) - {CHANGED}
        legacy_demonstrated = version is not None and self._legacy_demonstrated(graded, version)
        # Recompute only the rungs the correction can change.
        practiced = any(e.evidence_type in (EvidenceType.OBSERVATION, EvidenceType.LAB) and e.passed for e in graded)
        if legacy_demonstrated:
            ladder = DEMONSTRATED
        elif ladder == DEMONSTRATED:
            ladder = "practiced" if practiced else "understood"
        if ladder == DEMONSTRATED and self._legacy_changed(evidence, version):
            overlays.add(CHANGED)
        result.ladder, result.overlays = ladder, overlays
        return result

    def _legacy_demonstrated(self, evidence: List[LearningEvidence], version) -> bool:
        from app.services.learner_state_service import default_evidence_requirements

        requirements = version.evidence_requirements or default_evidence_requirements(
            self._concepts.get_concept(version.concept_id).kind
        )

        def matches(requirement):
            et = EvidenceType(requirement["evidence_type"])
            rows = [e for e in evidence if e.evidence_type == et and e.passed]
            if et == EvidenceType.KNOWLEDGE_CHECK and not requirement.get("allow_generated", False):
                rows = [e for e in rows if e.question_origin != QuestionOrigin.GENERATED]
            return rows

        def satisfied(reqs):
            rows = []
            for r in reqs.get("requires_all", []):
                m = matches(r)
                if len(m) < r.get("min_passed", 1):
                    return False, False
                rows.extend(m)
            for group in reqs.get("requires_any_of", []):
                hit = None
                for r in group:
                    m = matches(r)
                    if len(m) >= r.get("min_passed", 1):
                        hit = m
                        break
                if not hit:
                    return False, False
                rows.extend(hit)
            return True, any(e.grader == GradingMode.DETERMINISTIC for e in rows)

        ok, det = satisfied(requirements)
        if ok and det:
            return True
        alt = requirements.get("alternative")
        if alt:
            ok, det = satisfied(alt)
            return ok and det
        return False

    @staticmethod
    def _legacy_changed(evidence, current) -> bool:
        if current is None:
            return False
        if current.id in {e.concept_version_id for e in evidence}:
            return False
        return current.change_severity is not None and current.change_severity.value == "material"


def diff(session: Session):
    pairs = sorted(
        set(session.execute(select(LearningEvidence.user_id, LearningEvidence.concept_id)).all())
    )
    legacy, corrected = LegacyLearnerStateService(session), LearnerStateService(session)
    for user_id, concept_id in pairs:
        old, new = legacy.state(user_id, concept_id), corrected.state(user_id, concept_id)
        yield (
            user_id,
            concept_id,
            (old.ladder, sorted(old.overlays & {CHANGED})),
            (new.ladder, sorted(new.overlays & {CHANGED})),
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db_path", type=Path, help="path to a SQLite database (it is copied, never modified)")
    parser.add_argument("--include-unchanged", action="store_true")
    args = parser.parse_args(argv)
    if not args.db_path.is_file():
        print(f"not a file: {args.db_path}", file=sys.stderr)
        return 2
    tmp = tempfile.mkdtemp(prefix="ail5c-diff-")
    try:
        copy = Path(tmp) / "copy.db"
        shutil.copyfile(args.db_path, copy)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(args.db_path) + suffix)
            if sidecar.is_file():
                shutil.copyfile(sidecar, Path(str(copy) + suffix))
        engine = create_engine(f"sqlite:///{copy.as_posix()}")
        changed = total = 0
        with Session(engine) as session:
            if not inspect(engine).has_table("learning_evidence"):
                print("This database has no learning_evidence table (pre-AIL schema): nothing to diff.")
                engine.dispose()
                return 0
            for user_id, concept_id, old, new in diff(session):
                total += 1
                if old != new:
                    changed += 1
                elif not args.include_unchanged:
                    continue
                marker = "CHANGE " if old != new else "same   "
                print(f"{marker}user={user_id[:8]} concept={concept_id[:8]} legacy={old} corrected={new}")
        engine.dispose()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # Windows may hold the SQLite file briefly
    print(f"\n{changed} of {total} (user, concept) states would change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
