from pydantic import BaseModel

from app.db.enums import OrgRole, ProjectRole
from app.schemas.common import ORMModel, TimestampedRead


class OrganizationCreate(BaseModel):
    name: str


class OrganizationRead(TimestampedRead):
    name: str


class ProjectCreate(BaseModel):
    org_id: str
    name: str


class ProjectRead(TimestampedRead):
    org_id: str
    name: str


class UserRead(TimestampedRead):
    org_id: str
    email: str
    role: OrgRole


class ProjectMembershipCreate(BaseModel):
    user_id: str
    role: ProjectRole


class ProjectMembershipRead(ORMModel):
    id: str
    project_id: str
    user_id: str
    role: ProjectRole
