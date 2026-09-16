"""Shared shape for MA0's empty service skeletons.

Section 11 (Component Architecture) enumerates the Control Plane services
by responsibility; MA0's job is to reserve their names/boundaries, not
implement them (Section 31 MA0 scope: "empty service skeletons"). Every
method deliberately raises ``NotImplementedError`` — a later phase
(MA1-MA9, per the phase noted on each service below) replaces the body,
never the signature/boundary, unless the contract itself changes.
"""

from sqlalchemy.orm import Session


class BaseService:
    def __init__(self, db: Session):
        self.db = db
