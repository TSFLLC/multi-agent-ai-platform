"""Structured logging with secret redaction — Section L.

Logs stay local (no cloud/paid logging provider); secret-shaped values
are scrubbed before formatting.
"""

import json
import logging

from app.logging_config import JsonFormatter, RedactionFilter, redact


def test_redact_scrubs_openrouter_style_key():
    text = "using key sk-abcdefghijklmnopqrstuvwxyz0123456789 for this call"
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in redact(text)
    assert "***REDACTED***" in redact(text)


def test_redact_scrubs_bearer_token():
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwx1234=="
    assert "abcdefghijklmnopqrstuvwx1234==" not in redact(text)


def test_redact_scrubs_generic_key_value_secrets():
    text = "config: api_key=abc123, password: hunter2xyz, access_token=topsecretvalue"
    redacted = redact(text)
    assert "abc123" not in redacted
    assert "hunter2xyz" not in redacted
    assert "topsecretvalue" not in redacted


def test_redact_leaves_ordinary_text_untouched():
    text = "Agent Run completed in 4.2s with 1200 tokens."
    assert redact(text) == text


def test_redact_does_not_false_positive_on_field_names_containing_token():
    """Regression: the field name fencing_token (a job_queue column, not a
    secret) must survive redaction unscrubbed — a prior version's regex
    matched the bare word "token", which ate the value here."""
    text = "job_claimed fencing_token=3 heartbeat_at=2026-01-01"
    assert redact(text) == text


def test_redaction_filter_renders_before_redacting_never_corrupts_placeholders():
    """Regression: redacting the raw %-format template (before
    substitution) could match innocuous text like "fencing_token=%s" and
    eat the %s placeholder, crashing record.getMessage() downstream with
    "not all arguments converted during string formatting". The filter
    must render (substitute) first, then redact the rendered string."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="job_claimed worker_id=%s job_id=%s fencing_token=%s",
        args=("worker-1", "job-1", 3),
        exc_info=None,
    )
    RedactionFilter().filter(record)
    # Must not raise, and must reflect the substituted values.
    rendered = record.getMessage()
    assert rendered == "job_claimed worker_id=worker-1 job_id=job-1 fencing_token=3"


def test_redaction_filter_scrubs_rendered_message():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="leaked api_key=sk-shouldnotappearanywhere123456",
        args=(),
        exc_info=None,
    )
    RedactionFilter().filter(record)
    assert "sk-shouldnotappearanywhere123456" not in record.getMessage()
    assert "***REDACTED***" in record.getMessage()


def test_redaction_filter_scrubs_secret_passed_as_a_format_arg():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="provider response used key: %s",
        args=("api_key=sk-shouldnotappearinargstoo123",),
        exc_info=None,
    )
    RedactionFilter().filter(record)
    assert "sk-shouldnotappearinargstoo123" not in record.getMessage()


def test_json_formatter_produces_valid_json_with_redaction():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="secret=shouldnotleak999",
        args=(),
        exc_info=None,
    )
    RedactionFilter().filter(record)
    line = formatter.format(record)
    parsed = json.loads(line)  # must be valid JSON, one object per line
    assert parsed["level"] == "INFO"
    assert "shouldnotleak999" not in line


def test_logs_never_configured_to_ship_to_a_cloud_provider():
    """Static check: the logging module never imports a cloud logging
    SDK (e.g. boto3, google-cloud-logging, datadog)."""
    import app.logging_config as module

    with open(module.__file__, encoding="utf-8") as f:
        source = f.read().lower()
    for forbidden in ("boto3", "google.cloud", "datadog", "sentry_sdk", "loggly", "papertrail"):
        assert forbidden not in source
