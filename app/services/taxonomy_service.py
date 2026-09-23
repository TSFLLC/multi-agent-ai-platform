"""Shared taxonomy vocabulary — AIL.1A owns table creation (AIL architecture
reconciliation, frozen contract #3); AIL.1B and AIL.2A read/write their own
vocabularies (``capability``/``provider``, ``lane``/``topic``) through this
same service/table, never a competing one.

``vocabulary`` is intentionally unvalidated against a closed list here —
the whole point of the frozen contract is that other slices can introduce
new vocabularies without an AIL.1A code or schema change.
"""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.taxonomy import TaxonomyTerm


class TaxonomyService:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create_term(
        self,
        *,
        vocabulary: str,
        key: str,
        label: str,
        parent_id: Optional[str] = None,
        ma_phase: Optional[str] = None,
    ) -> TaxonomyTerm:
        existing = self.get_term(vocabulary=vocabulary, key=key)
        if existing is not None:
            return existing
        term = TaxonomyTerm(
            vocabulary=vocabulary, key=key, label=label, parent_id=parent_id, ma_phase=ma_phase
        )
        self.db.add(term)
        self.db.commit()
        self.db.refresh(term)
        return term

    def get_term(self, *, vocabulary: str, key: str) -> Optional[TaxonomyTerm]:
        stmt = select(TaxonomyTerm).where(TaxonomyTerm.vocabulary == vocabulary, TaxonomyTerm.key == key)
        return self.db.execute(stmt).scalars().first()

    def list_terms(self, *, vocabulary: Optional[str] = None, active_only: bool = True) -> List[TaxonomyTerm]:
        stmt = select(TaxonomyTerm)
        if vocabulary is not None:
            stmt = stmt.where(TaxonomyTerm.vocabulary == vocabulary)
        if active_only:
            stmt = stmt.where(TaxonomyTerm.active.is_(True))
        return list(self.db.execute(stmt).scalars().all())
