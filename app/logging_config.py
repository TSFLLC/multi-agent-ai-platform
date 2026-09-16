"""Structured local application logging — Section L.

Logs stay local (console + ``data/logs/app.log``). No cloud/paid logging
provider is ever introduced. A redaction filter scrubs common
secret-shaped values out of every log record before it is formatted, so a
stray ``logger.info("provider response: %s", full_payload)`` call can't
leak a credential into a log file the same way Section 20.1/20.2 already
require domain tables to never hold one.
"""

import json
import logging
import logging.handlers
import re
from datetime import datetime, timezone
from typing import Any, Dict

from app.config import settings

_REDACTED = "***REDACTED***"

# Patterns for values that should never appear in a log line even if a
# caller forgot to scrub them first — provider API keys, bearer tokens,
# generic secret-looking assignments. Deliberately does NOT match on the
# bare word "token" (only "access_token"/"api_token"-style credential
# names) — job_queue's own fencing_token/heartbeat fields are not secrets,
# and a broader "token" match would eat them.
_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|secret|password|access[_-]?token|api[_-]?token)\s*[=:]\s*\S+"),
]


def redact(text: str) -> str:
    if not text:
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


class RedactionFilter(logging.Filter):
    """Renders the record's message (``%``-substitution against the
    original ``msg``/``args``) exactly once, then redacts the *rendered*
    text — never the raw ``%s``-style template. Redacting the template
    directly is what silently corrupts placeholders like
    ``"fencing_token=%s"`` (a false-positive secret match eating the
    ``%s``), which then crashes formatting downstream with
    "not all arguments converted during string formatting". Rendering
    first sidesteps that class of bug entirely.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            record.msg = redact(rendered)
            record.args = ()
        except Exception:  # noqa: BLE001, S110 — a logging Filter must never itself raise
            pass
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line — easy to grep/parse locally, no external
    log shipping involved."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key in ("task_run_id", "agent_run_id", "worker_id", "job_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()

    redaction_filter = RedactionFilter()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(JsonFormatter())
    console_handler.addFilter(redaction_filter)
    root.addHandler(console_handler)

    if settings.log_to_file:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            settings.logs_dir / "app.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(redaction_filter)
        root.addHandler(file_handler)
