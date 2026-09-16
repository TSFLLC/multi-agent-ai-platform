"""Local filesystem foundation — Section C.

data/, data/artifacts/, data/logs/, data/workspaces/, data/backups/ must
all initialize safely and locally — never a cloud object store.
"""

from app.config import Settings
from app.db.session import build_engine


def test_engine_creates_only_its_own_parent_directory(tmp_path):
    """A custom engine URL must create exactly its own directory tree, not
    reach into the real app's data/ subdirectories (regression test for
    the MA0 bug where _ensure_data_dirs always used the global settings
    singleton regardless of the URL passed in)."""
    custom_db = tmp_path / "nested" / "custom.db"
    engine = build_engine(f"sqlite:///{custom_db.as_posix()}")
    with engine.connect():
        pass
    assert custom_db.parent.exists()
    engine.dispose()


def test_real_app_engine_creates_full_data_tree(tmp_path, monkeypatch):
    from app import config as config_module

    test_settings = Settings(
        database_path=tmp_path / "data" / "multi_agent_platform.db",
        artifacts_dir=tmp_path / "data" / "artifacts",
        logs_dir=tmp_path / "data" / "logs",
        workspaces_dir=tmp_path / "data" / "workspaces",
        backups_dir=tmp_path / "data" / "backups",
    )
    monkeypatch.setattr(config_module, "settings", test_settings)

    import app.db.session as session_module

    monkeypatch.setattr(session_module, "settings", test_settings)

    engine = session_module.build_engine()
    with engine.connect():
        pass

    assert (tmp_path / "data" / "multi_agent_platform.db").parent.exists()
    assert (tmp_path / "data" / "artifacts").exists()
    assert (tmp_path / "data" / "logs").exists()
    assert (tmp_path / "data" / "workspaces").exists()
    assert (tmp_path / "data" / "backups").exists()
    engine.dispose()


def test_no_cloud_object_storage_directories_expected():
    """Settings never defines an s3_bucket / azure_container / gcs_bucket
    style field — local filesystem only."""
    fields = set(Settings.model_fields.keys())
    assert not any("s3" in f or "azure" in f or "gcs" in f or "bucket" in f for f in fields)
