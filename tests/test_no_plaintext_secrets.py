"""No provider secret stored in ordinary domain tables — Section 20.1/20.2, 24.4 #19.

Static schema checks: every table other than secret_references must not
carry a column whose name suggests a raw credential value; the pointer
column on secret_references itself must be named/typed as an opaque
reference, not a value.
"""

from sqlalchemy import inspect

SUSPICIOUS_SUBSTRINGS = ("api_key", "apikey", "password", "secret_value", "credential_value", "access_token")


def test_no_domain_table_carries_a_plaintext_secret_column(engine):
    inspector = inspect(engine)
    offenders = []
    for table_name in inspector.get_table_names():
        if table_name in ("secret_references", "alembic_version"):
            continue
        for col in inspector.get_columns(table_name):
            name = col["name"].lower()
            if any(s in name for s in SUSPICIOUS_SUBSTRINGS):
                offenders.append(f"{table_name}.{col['name']}")
    assert offenders == []


def test_secret_references_stores_a_reference_not_a_value(engine):
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("secret_references")}
    assert "secret_store_ref" in columns
    assert "value" not in columns
    assert "secret_value" not in columns
    assert "plaintext" not in columns


def test_providers_table_has_no_direct_credential_column(engine):
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("providers")}
    assert not any("credential" in c or "key" in c for c in columns)
