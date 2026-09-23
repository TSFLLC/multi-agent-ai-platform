"""Shared controlled vocabulary — AIL architecture reconciliation, frozen
contract #3 (docs/ail-learning-spec-v1.md Sec 28.3).

``taxonomy_terms`` is a single, data-driven vocabulary table shared by
AIL.1A (this module's owner: ``track``/``role`` and consumer of
``platform_component``), AIL.1B (Model & Provider Intelligence:
``capability``/``provider``) and AIL.2A (Radar: ``lane``/``topic``/
``platform_component``). ``vocabulary`` is intentionally a plain indexed
string, not a Python enum — new vocabularies must be addable by another
slice without a schema change here. AIL.1A creates the table (it needs
``track``/``role`` first) but does not seed capability/provider/lane/topic
rows; only ``track`` and ``role`` are seeded, and only enough to validate
this slice (spec instruction: "do not over-seed content").
"""

from typing import List, Optional

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class TaxonomyTerm(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "taxonomy_terms"
    __table_args__ = (UniqueConstraint("vocabulary", "key", name="uq_taxonomy_terms_vocabulary_key"),)

    vocabulary: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("taxonomy_terms.id"), nullable=True)
    # e.g. "MA9" — spec Sec 28.3's "relates to MA9" resolution. Nullable:
    # most vocabulary entries (track, role, capability, provider) carry no
    # MA-phase gate at all.
    ma_phase: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    parent: Mapped[Optional["TaxonomyTerm"]] = relationship(
        remote_side="TaxonomyTerm.id", back_populates="children"
    )
    children: Mapped[List["TaxonomyTerm"]] = relationship(back_populates="parent")
