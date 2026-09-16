"""Shared SQLAlchemy column-type helpers."""

from typing import Type

from sqlalchemy import Enum as SAEnum


def sa_enum(enum_cls: Type) -> SAEnum:
    """A portable, check-constrained VARCHAR enum column (Section 10.5.5 / ADR-10).

    ``native_enum=False`` avoids a SQLite/Postgres-native enum type — this
    renders as ``VARCHAR`` + ``CHECK`` constraint, portable either way.
    ``values_callable`` stores the enum's ``.value`` (e.g. ``"repair_loop"``)
    rather than SQLAlchemy's default of the member ``.name``
    (``"REPAIR_LOOP"``), matching the lowercase snake_case values used
    throughout the spec and this codebase's own CHECK constraints/tests.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        validate_strings=True,
        # SQLAlchemy 2.0 defaults create_constraint=False for non-native
        # Enum columns (changed to avoid autogenerate/reflection churn) —
        # without this, native_enum=False silently produces a bare VARCHAR
        # with *no* DB-level CHECK constraint at all, leaving enum
        # enforcement entirely to the ORM/Python layer. Explicit True here
        # is what makes the "defense-in-depth against raw SQL" claim in
        # this codebase's docstrings/tests actually true.
        create_constraint=True,
        values_callable=lambda cls: [member.value for member in cls],
    )
