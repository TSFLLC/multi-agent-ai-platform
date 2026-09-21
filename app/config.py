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

# MA7.4c: bounds of ``worker_concurrency`` (also the ``--concurrency`` CLI flag).
MIN_WORKER_CONCURRENCY = 1
MAX_WORKER_CONCURRENCY = 4


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

    # Pre-MA3 checkpoint (Owner requirement): a WAL-safe local backup
    # (app.backup, VACUUM INTO) taken automatically at startup, bounded by
    # this many most-recent backups kept in data/backups/.
    backup_retention_count: int = 10
    backup_on_startup: bool = True

    sqlite_busy_timeout_ms: int = 5000

    log_level: str = "INFO"
    log_to_file: bool = True

    # Worker foundation (Section F) — local in-process/CLI worker polling
    # the SQLite job_queue table. No external broker (Section 10.5.3).
    worker_poll_interval_seconds: float = 1.0
    worker_lease_seconds: int = 30
    worker_heartbeat_interval_seconds: float = 10.0
    worker_id_prefix: str = "worker"
    # MA7.4a: while the worker is idle it re-runs the (idempotent) workflow
    # reconciliation sweep at most this often, so a transient failure after
    # a commit (e.g. a locked database) is recovered without a restart.
    # 0 disables the periodic sweep (the startup sweep always runs).
    worker_reconcile_interval_seconds: float = 60.0
    # MA7.4c: how many jobs ONE worker process may run at once. Each lane is a
    # thread with its own DB sessions on the same SQLite queue (same leases,
    # heartbeats and fencing). 1 (the default) is exactly the historical
    # single-loop worker. Bounded small on purpose: SQLite has one writer, so
    # a handful of lanes is what a local-first V1 can usefully feed.
    worker_concurrency: int = 1

    # MA7.4b: the most direct outgoing branches (edges) any single workflow
    # node may have. Every branch is one Agent Run -- a provider call and a
    # budget reservation -- so an unbounded fan-out is an unbounded cost/
    # contention multiplier. 8 comfortably covers a realistic decomposition
    # (e.g. backend/frontend/database/docs/test/security/review) while
    # keeping the worst case for one node small. Enforced at publish and
    # again at start; provider-agnostic on purpose.
    workflow_max_fan_out: int = 8

    # MA7.4c: hard platform cap on the rendered upstream evidence a workflow
    # node's Agent may be given (characters, not tokens: model-independent and
    # exactly measurable). Fan-in can multiply prompt size, so evidence larger
    # than this FAILS the node before any provider call -- it is never
    # truncated or summarized. ~200k characters is on the order of 50k tokens.
    # A model's known context window can only LOWER the effective limit, never
    # raise it (see AgentExecutionService).
    workflow_upstream_context_max_chars: int = 200_000

    # Agent Run / Task Run defaults (Owner decision, frozen) — overridable
    # per-row (agent_runs.timeout_seconds / task_runs.timeout_seconds),
    # these are only the defaults new rows are created with.
    default_agent_run_timeout_seconds: int = 30 * 60
    default_task_run_timeout_seconds: int = 90 * 60
    max_agent_run_attempts: int = 2

    # Budget estimation (MA3) — deliberately simple, deterministic
    # heuristics, not MA8 optimization. Used only to size a
    # budget_reservations row *before* an invocation; the actual recorded
    # cost always comes from real token usage once the call completes.
    budget_estimate_tokens_in: int = 2000
    budget_estimate_tokens_out: int = 500
    # Reserved amount for a manually-selected UNKNOWN-priced model when a
    # budget applies — conservative on purpose so an unpriced model can
    # never quietly bypass budget enforcement. Never used to answer "is
    # this model free" — only to size a reservation.
    unknown_pricing_reserve_amount: str = "1.00"

    # Agent-to-Agent Review (MA4) — Owner-frozen default: a review cycle
    # repairs at most this many times before the Task Run terminates
    # without a reviewer-approved result (never silently marked accepted).
    # Overridable per Task Run (task_runs.config_snapshot.max_repair_iterations),
    # this is only the default new reviewed Task Runs are created with.
    default_max_repair_iterations: int = 2

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {v!r}")
        return upper

    @field_validator("worker_reconcile_interval_seconds")
    @classmethod
    def _validate_reconcile_interval(cls, v: float) -> float:
        # Bounded: either disabled (0) or no more often than once a second --
        # never a busy loop.
        if v != 0 and v < 1.0:
            raise ValueError("worker_reconcile_interval_seconds must be 0 (disabled) or >= 1.0")
        return v

    @field_validator("worker_concurrency")
    @classmethod
    def _validate_worker_concurrency(cls, v: int) -> int:
        if not MIN_WORKER_CONCURRENCY <= v <= MAX_WORKER_CONCURRENCY:
            raise ValueError(
                f"worker_concurrency must be between {MIN_WORKER_CONCURRENCY} and {MAX_WORKER_CONCURRENCY}"
            )
        return v

    @field_validator("workflow_upstream_context_max_chars")
    @classmethod
    def _validate_upstream_context_max_chars(cls, v: int) -> int:
        # A floor so a typo cannot make every fan-in fail; a ceiling so the
        # cap cannot be silently disabled.
        if not 1_000 <= v <= 10_000_000:
            raise ValueError("workflow_upstream_context_max_chars must be between 1000 and 10000000")
        return v

    @field_validator("workflow_max_fan_out")
    @classmethod
    def _validate_workflow_max_fan_out(cls, v: int) -> int:
        # At least 1 (0 would forbid every edge) and bounded above so a typo
        # cannot silently disable the cap.
        if not 1 <= v <= 64:
            raise ValueError("workflow_max_fan_out must be between 1 and 64")
        return v

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
