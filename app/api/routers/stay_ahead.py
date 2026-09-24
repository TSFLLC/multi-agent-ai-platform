"""AIL.4A Personal Stay-Ahead Today — one owner-authorized, read-only endpoint.

The authenticated user is the only identity used; there is no ``user_id``
parameter (an unknown query parameter, including ``user_id``, is a 422). The
handler never commits: it composes existing records on read
(``app.services.stay_ahead_service``) and persists nothing.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auth import get_current_user
from app.models.identity import User
from app.schemas.stay_ahead import StayAheadToday
from app.services.stay_ahead_service import DEFAULT_WINDOW_DAYS, MAX_WINDOW_DAYS, StayAheadService

router = APIRouter(prefix="/stay-ahead", tags=["stay-ahead"])

_ALLOWED_QUERY = {"window_days"}


@router.get("/today", response_model=StayAheadToday)
def get_stay_ahead_today(
    request: Request,
    window_days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    unexpected = sorted(set(request.query_params.keys()) - _ALLOWED_QUERY)
    if unexpected:
        raise HTTPException(status_code=422, detail=f"Unsupported query parameter(s): {', '.join(unexpected)}.")
    return StayAheadService(db).today(user.id, window_days=window_days)
