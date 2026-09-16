"""Declarative base and shared naming convention.

A fixed naming convention is required so that Alembic autogeneration
produces stable, portable constraint/index names (SQLite in particular is
sloppy about default constraint names) — this matters for the "no
PostgreSQL-only shortcuts" portability requirement (spec Section 10.5.5).
"""

from typing import ClassVar

from sqlalchemy import JSON, MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # dict/list-typed columns are structured JSON *snapshots/payloads*
    # (Section 10.5.5) — SQLite's JSON/TEXT type via SQLAlchemy's portable
    # JSON type, which maps to JSONB under Postgres later without a schema
    # redesign. Anything queried/joined relationally is a normalized
    # column/table instead, never one of these.
    type_annotation_map: ClassVar[dict] = {
        dict: JSON,
        list: JSON,
    }
