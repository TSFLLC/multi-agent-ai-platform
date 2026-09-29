"""AIL.5C Grader golden-set release gate (CLI).

    python -m scripts.ail5c_grader_gate PATH_TO_DB_COPY [--definition KEY ...] [--json OUT]

Runs the candidate Grader configuration recorded in the database (its Academy
Grader Agent Version / model policy and the registered provider models) against
the golden set of every approved assessment definition, through the real grading
packet, contract and cross-check, and prints a report. Exit status is 0 only if
the candidate is release-ready, 1 if it is not, 2 for a usage error.

The database is COPIED to a temporary file first and the copy is opened; the
original is opened read-only and never written. The gate makes real provider
calls (that is the point) against the models the copy registers, so it costs
what those models cost. Never point it at a live database, and do not treat a
"not ready" as a request to change the golden set: change the candidate.
"""

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assessment_golden import render, run_golden_gate
from app.db.enums import OrgRole
from app.db.session import build_engine
from app.models.identity import User


def _copy_readonly(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target.as_posix())
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def main(
    argv: Optional[List[str]] = None,
    *,
    adapter_factory: Optional[Callable] = None,
    stdout: Optional[TextIO] = None,
) -> int:
    out = stdout or sys.stdout
    parser = argparse.ArgumentParser(description="AIL.5C Grader golden-set release gate")
    parser.add_argument("database", help="path to a COPY of a platform database")
    parser.add_argument("--definition", action="append", help="limit the run to this definition key")
    parser.add_argument("--json", dest="json_out", help="also write the JSON report to this path")
    args = parser.parse_args(argv)

    source = Path(args.database)
    if not source.is_file():
        print(f"error: {source} is not a file", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "grader-gate.db"
        _copy_readonly(source, work)
        engine = build_engine(f"sqlite:///{work.as_posix()}")
        try:
            with Session(engine, expire_on_commit=False) as db:
                user = db.execute(select(User).where(User.role == OrgRole.OWNER)).scalars().first()
                if user is None:
                    print("error: the database has no owner user to run the gate as", file=sys.stderr)
                    return 2
                report = run_golden_gate(
                    db,
                    user,
                    adapter_factory=adapter_factory,
                    definition_keys=args.definition,
                )
        finally:
            engine.dispose()

    text = render(report)
    print(text, file=out)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    verdict = "RELEASE-READY" if report.release_ready else "NOT READY"
    print(f"\nAIL.5C Grader golden-set gate: {verdict}", file=out)
    return 0 if report.release_ready else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
