from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.db.enums import ArtifactType


class ArtifactRead(BaseModel):
    """Metadata only (Section 24.4 #18) — content is fetched separately
    via GET /artifacts/{id}/content so a large artifact never has to be
    inlined into a JSON list response."""

    id: str
    agent_run_id: str
    type: ArtifactType
    content_hash: Optional[str] = None
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    created_at: datetime
