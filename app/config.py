"""Application settings.

Local-first by construction: the only supported database in V1 is a local
SQLite file, and the API binds to loopback only by default. Nothing here
reads cloud credentials or remote connection strings.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAP_", env_file=".env", extra="ignore")

    # Local-only by default. Changing this to 0.0.0.0 is a conscious,
    # explicit act outside of V1's default posture (see spec Section 20.8 /
    # Owner's "local unless explicitly required to leave the machine").
    host: str = "127.0.0.1"
    port: int = 8000

    database_path: Path = DATA_DIR / "multi_agent_platform.db"
    artifacts_dir: Path = DATA_DIR / "artifacts"
    logs_dir: Path = DATA_DIR / "logs"
    workspaces_dir: Path = DATA_DIR / "workspaces"
    backups_dir: Path = DATA_DIR / "backups"

    sqlite_busy_timeout_ms: int = 5000

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"


settings = Settings()
