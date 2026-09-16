"""Identity/tenancy resource boundary — Section 25.2, 25.5."""

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, not_implemented
from app.schemas.identity import (
    OrganizationCreate,
    OrganizationRead,
    ProjectCreate,
    ProjectMembershipCreate,
    ProjectMembershipRead,
    ProjectRead,
)

router = APIRouter(tags=["projects"])


@router.get("/organizations", response_model=List[OrganizationRead])
def list_organizations(db: Session = Depends(get_db)):
    not_implemented()


@router.post("/organizations", response_model=OrganizationRead, status_code=201)
def create_organization(body: OrganizationCreate, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/projects", response_model=List[ProjectRead])
def list_projects(db: Session = Depends(get_db)):
    not_implemented()


@router.post("/projects", response_model=ProjectRead, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    not_implemented()


@router.get("/projects/{project_id}/members", response_model=List[ProjectMembershipRead])
def list_project_members(project_id: str, db: Session = Depends(get_db)):
    not_implemented()


@router.post("/projects/{project_id}/members", response_model=ProjectMembershipRead, status_code=201)
def add_project_member(project_id: str, body: ProjectMembershipCreate, db: Session = Depends(get_db)):
    not_implemented()


@router.delete("/projects/{project_id}/members/{membership_id}", status_code=204)
def remove_project_member(project_id: str, membership_id: str, db: Session = Depends(get_db)):
    not_implemented()
