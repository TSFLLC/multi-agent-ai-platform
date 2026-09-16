"""Application settings.

Local-first by construction: the only supported database in V1 is a local
SQLite file, and the API binds to loopback only by default. Nothing here
reads cloud credentials or remote connection strings.
"""

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAP_", env_file=".env", extra="ignore")

    environment: str = "local"

    # Local-only by default. Changing this to 0.0.0.0 (or anything else
    # non-loopback) requires the explicit allow_remote_bind=True opt-in
    # below — this is the "fail clearly on invalid configuration" /
    # "local unless explicitly required to leave the machine" requirement,
    # not just a default that's easy to silently override.
    host: str = "127.0.0.1"
    port: int = 8000
    allow_remote_bind: bool = False

    database_path: Path = DATA_DIR / "multi_agent_platform.db"
    artifacts_dir: Path = DATA_DIR / "artifacts"
    logs_dir: Path = DATA_DIR / "logs"
    workspaces_dir: Path = DATA_DIR / "workspaces"
    backups_dir: Path = DATA_DIR / "backups"
    # Local authentication boundary (MA1B) — a random per-install token,
    # never a password/OAuth flow. Not a "secret" in the Section 20.1/20.2
    # sense (no provider credential is derived from or grants access to
    # it), so it deliberately does not go through secret_references — it
    # is this local install's own front-door key, analogous to Jupyter's
    # local token auth.
    auth_token_path: Path = DATA_DIR / "local_auth_token"

    sqlite_busy_timeout_ms: int = 5000

    log_level: str = "INFO"
    log_to_file: bool = True

    # Worker foundation (Section F) — local in-process/CLI worker polling
    # the SQLite job_queue table. No external broker (Section 10.5.3).
    worker_poll_interval_seconds: float = 1.0
    worker_lease_seconds: int = 30
    worker_heartbeat_interval_seconds: float = 10.0
    worker_id_prefix: str = "worker"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {v!r}")
        return upper

    @model_validator(mode="after")
    def _reject_unexplained_remote_bind(self) -> "Settings":
        if self.host not in LOOPBACK_HOSTS and not self.allow_remote_bind:
            raise ValueError(
                f"host={self.host!r} is not a loopback address. This platform is "
                "local-first by design (Owner instruction) — binding beyond "
                "127.0.0.1 requires setting MAP_ALLOW_REMOTE_BIND=true explicitly, "
                "which this configuration does not do."
            )
        return self

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"


settings = Settings()
