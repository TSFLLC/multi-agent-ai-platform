"""Local authentication boundary — MA1B.

The smallest appropriate mechanism for a loopback-bound, single-user
local application: a random per-install token, written to a local file on
first run (analogous to Jupyter Notebook's local token auth), required as
``Authorization: Bearer <token>`` on endpoints that touch identity/project
data. Not a password, not a session, not OAuth — there is exactly one
local Owner user (app.bootstrap), and this only answers "is the caller
allowed to act as that Owner at all."

The FastAPI dependency shape (``get_current_user``) is the seam a future
hosted-auth phase would replace: business services and other route
handlers depend on "give me the current User," never on how that user was
authenticated. Swapping local-token validation for real session/JWT
validation later changes this one function, not every route or service.
"""

import logging
import secrets
from typing import Optional

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.bootstrap import LOCAL_OWNER_USER_ID
from app.config import settings
from app.errors import UnauthorizedError
from app.models.identity import User

logger = logging.getLogger("app.auth")


def ensure_local_auth_token() -> str:
    """Idempotent: reuses the existing token file if present, otherwise
    generates a new cryptographically random token and persists it."""
    path = settings.auth_token_path
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    logger.info(
        "local_auth_token_created path=%s -- this is your local API token; read it from that file "
        "(it is never logged again after creation)",
        path,
    )
    print(f"\nLocal API auth token (save this — it will not be printed again):\n  {token}\n")
    return token


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Validates the Authorization: Bearer <token> header against the
    local install token, then resolves the single local Owner user
    (app.bootstrap.LOCAL_OWNER_USER_ID). Raises 401 on anything else —
    missing header, malformed header, wrong token, or a DB that was never
    bootstrapped."""
    token = _extract_bearer_token(authorization)
    if token is None:
        raise UnauthorizedError("Missing or malformed Authorization: Bearer <token> header.")

    expected = ensure_local_auth_token()
    if not secrets.compare_digest(token, expected):
        raise UnauthorizedError("Invalid local API token.")

    user = db.get(User, LOCAL_OWNER_USER_ID)
    if user is None:
        raise UnauthorizedError(
            "Local bootstrap identity not found — the application has not completed startup bootstrap."
        )
    return user
