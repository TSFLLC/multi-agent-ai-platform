"""Irreversible migrations are deliberate, declared, and refuse without side effects."""

import ast
import hashlib
import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from alembic import command
from app.config import settings
from app.db.session import build_engine

ROOT = Path(__file__).resolve().parent.parent
POLICY_DOC = ROOT / "docs" / "deployment" / "migration-rollback-policy.md"

# Adding a revision here is a review decision: rollback for it is backup/restore.
EXPECTED_IRREVERSIBLE = {"academy_learning_item_lineage"}


def _cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def _downgrade_only_raises(path: str) -> bool:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            body = [
                n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            ]
            return len(body) == 1 and isinstance(body[0], ast.Raise)
    return False


def _declares_irreversible(path: str) -> bool:
    spec = importlib.util.spec_from_file_location("_policy_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "IRREVERSIBLE", False) is True


def test_every_unconditionally_refusing_downgrade_is_declared_and_expected():
    script = ScriptDirectory.from_config(_cfg())
    refusing = {r.revision for r in script.walk_revisions() if _downgrade_only_raises(r.path)}
    declared = {r.revision for r in script.walk_revisions() if _declares_irreversible(r.path)}
    assert refusing == declared == EXPECTED_IRREVERSIBLE


def test_policy_document_names_every_irreversible_revision_and_backup_restore():
    text_ = POLICY_DOC.read_text(encoding="utf-8")
    assert "restore of the pre-migration backup" in text_
    for revision in EXPECTED_IRREVERSIBLE:
        assert revision in text_


def _snapshot(engine) -> str:
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")).fetchall()
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        items = conn.execute(text("SELECT * FROM learning_items ORDER BY id")).fetchall()
    return hashlib.sha256(repr((rows, version, items)).encode()).hexdigest()


def test_downgrading_an_irreversible_revision_refuses_and_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "rollback-policy.db"
    monkeypatch.setattr(settings, "database_path", path)
    command.upgrade(_cfg(), "head")
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO concepts (id, slug, name, level, kind, is_core, created_at) VALUES ('c-rb', 'c-rb', 'C', 'foundational', 'mechanism', 0, '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO learning_items (id, concept_id, item_type, title, reviewed, lineage_id, version, created_at) VALUES ('li-rb', 'c-rb', 'check_question', 'T', 1, '55555555-5555-4555-8555-555555555555', 1, '2026-01-01')"
            )
        )
    before = _snapshot(engine)
    with pytest.raises(RuntimeError, match="restoring a pre-lineage backup is required"):
        command.downgrade(_cfg(), "-1")
    assert _snapshot(engine) == before
    with engine.begin() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "academy_learning_item_lineage"
        )
    engine.dispose()
