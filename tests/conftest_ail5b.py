"""AIL.5B test fixtures with proper FK setup."""

import pytest
from datetime import datetime
from uuid import uuid4


@pytest.fixture
def test_user(db):
    """Create a test user for FK references."""
    from app.models.identity import User

    user_id = str(uuid4())
    user = User(
        id=user_id,
        email=f"test-{user_id[:8]}@example.com",
        password_hash="test_hash",
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def test_program_version(db, test_user):
    """Create program version for enrollment."""
    from app.models.academy import AcademyProgram, AcademyProgramVersion
    from app.db.enums import AcademyProgramVersionStatus

    program = AcademyProgram(
        id=str(uuid4()),
        slug="test-program",
        title="Test Program",
        author_user_id=test_user.id,
    )
    db.add(program)
    db.flush()

    version = AcademyProgramVersion(
        id=str(uuid4()),
        program_id=program.id,
        version=1,
        status=AcademyProgramVersionStatus.PUBLISHED,
        duration_days=30,
        completion_rules={},
        published_at=datetime.utcnow(),
    )
    db.add(version)
    db.commit()
    return version


@pytest.fixture
def test_enrollment(db, test_user, test_program_version):
    """Create enrollment for project attempts."""
    from app.models.academy import AcademyEnrollment
    from app.db.enums import AcademyPace, AcademyEnrollmentStatus

    enrollment = AcademyEnrollment(
        id=str(uuid4()),
        user_id=test_user.id,
        program_version_id=test_program_version.id,
        pace=AcademyPace.SCHEDULED,
        status=AcademyEnrollmentStatus.ACTIVE,
    )
    db.add(enrollment)
    db.commit()
    return enrollment


@pytest.fixture
def test_project_template(db, test_user):
    """Create published project template."""
    from app.models.academy import ProjectTemplate
    from app.db.enums import ProjectAudienceLevel, ProjectLadderLevel, ProjectTemplateBuildMode

    template = ProjectTemplate(
        id=str(uuid4()),
        template_key="test-template",
        version=1,
        status="published",
        title="Test Project",
        audience_level=ProjectAudienceLevel.BEGINNER,
        ladder_level=ProjectLadderLevel.L2,
        build_mode=ProjectTemplateBuildMode.NO_CODE,
        brief_md="# Test Brief",
        author_user_id=test_user.id,
        published_at=datetime.utcnow(),
    )
    db.add(template)
    db.commit()
    return template


@pytest.fixture
def test_project_attempt(db, test_user, test_project_template, test_enrollment):
    """Create project attempt for milestone tests."""
    from app.models.academy import ProjectAttempt
    from app.db.enums import ProjectAttemptStatus

    attempt = ProjectAttempt(
        id=str(uuid4()),
        user_id=test_user.id,
        project_template_id=test_project_template.id,
        enrollment_id=test_enrollment.id,
        is_capstone=False,
        status=ProjectAttemptStatus.ACTIVE,
        brief_snapshot={},
    )
    db.add(attempt)
    db.commit()
    return attempt


@pytest.fixture
def test_project_milestone(db, test_project_template):
    """Create milestone for attempt."""
    from app.models.academy import ProjectMilestone

    milestone = ProjectMilestone(
        id=str(uuid4()),
        project_template_id=test_project_template.id,
        position=1,
        title="Test Milestone",
        instructions_md="# Instructions",
        check_spec={"type": "test", "cases": []},
    )
    db.add(milestone)
    db.commit()
    return milestone


@pytest.fixture
def test_milestone_attempt(db, test_project_attempt, test_project_milestone):
    """Create milestone attempt."""
    from app.models.academy import MilestoneAttempt
    from app.db.enums import MilestoneAttemptStatus, MilestoneAttemptMode

    milestone_attempt = MilestoneAttempt(
        id=str(uuid4()),
        project_attempt_id=test_project_attempt.id,
        project_milestone_id=test_project_milestone.id,
        status=MilestoneAttemptStatus.NOT_STARTED,
        attempts_count=0,
        mode=MilestoneAttemptMode.NORMAL,
    )
    db.add(milestone_attempt)
    db.commit()
    return milestone_attempt
