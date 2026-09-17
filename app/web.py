"""Local Operator Console — static SPA shell (MA5-UI).

Serves the operator console (``frontend/``) as a same-origin static site
so its JavaScript can call the JSON API with plain relative fetches (no
CORS configuration needed — Section 10.5 local-first). The only dynamic
piece is the local MA1B auth token (app.auth.ensure_local_auth_token):
this route reads the same token every other endpoint already validates
and injects it into the served HTML, so the operator never copies/pastes
a Bearer token by hand (PV-01) and the token is never hard-coded into
frontend source. Safe under the same loopback-only assumption the rest of
this application already relies on (app.config.Settings — binding beyond
127.0.0.1 requires an explicit opt-in) and the same single-local-Owner
model app.auth documents; this route adds no new trust boundary, it only
saves the operator a manual file-read + copy/paste of a value a local
process could already read directly off disk.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.auth import ensure_local_auth_token

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_INDEX_PATH = FRONTEND_DIR / "index.html"
_TOKEN_PLACEHOLDER = "%%MAP_LOCAL_TOKEN_VALUE%%"

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def serve_console() -> HTMLResponse:
    token = ensure_local_auth_token()
    html = _INDEX_PATH.read_text(encoding="utf-8")
    # Token never printed to the page as visible text — it lands only in an
    # inline <script> global the app's own JS reads once at load, the same
    # way any other page-scoped JS constant would be defined.
    html = html.replace(_TOKEN_PLACEHOLDER, token)
    return HTMLResponse(content=html)
