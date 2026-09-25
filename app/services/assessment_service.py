"""AIL.5C assessment lifecycle. Code owns every transition; the model owns none.

    ASSESSMENT READY -> START (pin versions, freeze manifest, draw fresh
    challenge) -> DRAFT -> SUBMIT (compare-and-swap freeze + hash + attest)
    -> DETERMINISTIC STAGE -> [GRADER for judgment-only criteria]
    -> AGGREGATE (code) -> FINALIZE (one transaction: final result, evidence,
    project status) -> report / record.

Learner State is never written here; a passing result appends evidence through
``AssessmentEvidenceWriter`` and ``LearnerStateService`` recomputes on read.
Everything is learner-scoped: every query filters on ``user_id``.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.assessment_contract import GRADING_CONTRACT_VERSION, review_fingerprint, sha256_hex
from app.assessment_outcome import (
    GRADING_NOT_NEEDED,
    aggregate,
    source_work_summary,
)
from app.db.enums import (
    AssessmentAttemptStatus,
    AssessmentKind,
    AssessmentOrigin,
    AssessmentOutcome,
    AssessmentResultKind,
    AssessmentReviewTrigger,
    AssistanceLevel,
    EvaluationRunStatus,
    MilestoneAttemptMode,
    MilestoneAttemptStatus,
    ProjectAttemptStatus,
)
from app.db.mixins import new_uuid
from app.errors import AppError, ConflictError, NotFoundError
from app.models.academy import (
    AssessmentReadySubmission,
    CandidateEvidence,
    ExplainBackResponse,
    MilestoneAttempt,
    ProjectAttempt,
    ProjectExperimentLink,
    ProjectTemplate,
)
from app.models.agents import AgentVersion
from app.models.assessment import (
    ACTIVE_ATTEMPT_STATUSES,
    AssessmentAttempt,
    AssessmentDefinition,
    AssessmentDefinitionConcept,
    AssessmentResult,
    AssessmentReview,
)
from app.models.evaluation_runs import EvaluationRun
from app.models.identity import User
from app.models.lab import Experiment
from app.models.learner import LearningEvidence
from app.models.tasks import AgentRun
from app.services import independence_policy as policy
from app.services.assessment_challenge_service import draw_challenge, is_authored_challenge, public_challenge
from app.services.assessment_checks import CheckContext, CheckUnavailable, aware, run_check
from app.services.assessment_definition_service import AssessmentDefinitionService
from app.services.assessment_evidence_writer import AssessmentEvidenceWriter
from app.services.assessment_mode_guard import AssessmentModeGuard
from app.services.audit_service import AuditService
from app.services.concept_graph_service import ConceptGraphService
from app.services.learner_state_service import LearnerStateService

MAX_DRAFT_CHARS = 200_000
ATTESTATION_NOTE_MAX = 500


class AssessmentNotReady(ConflictError):
    code = "assessment_not_ready"


class AssessmentInvalid(AppError):
    status_code = 422
    code = "assessment_invalid"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AssessmentService:
    def __init__(self, db: Session, *, now: Optional[datetime] = None, adapter_factory=None):
        self.db = db
        self._fixed_now = now
        self.adapter_factory = adapter_factory
        self.definitions = AssessmentDefinitionService(db)

    def _clock(self) -> datetime:
        return self._fixed_now or _now()

    # -- reads (always learner-scoped) --------------------------------------------------------

    def get_attempt(self, user_id: str, attempt_id: str) -> AssessmentAttempt:
        attempt = self.db.execute(
            select(AssessmentAttempt).where(
                AssessmentAttempt.id == attempt_id, AssessmentAttempt.user_id == user_id
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise NotFoundError("Assessment attempt not found.")
        return attempt

    def results(self, user_id: str, attempt_id: str) -> List[AssessmentResult]:
        self.get_attempt(user_id, attempt_id)
        return list(
            self.db.execute(
                select(AssessmentResult)
                .where(AssessmentResult.attempt_id == attempt_id, AssessmentResult.user_id == user_id)
                .order_by(AssessmentResult.seq)
            ).scalars()
        )

    def original_final(self, attempt_id: str) -> Optional[AssessmentResult]:
        return (
            self.db.execute(
                select(AssessmentResult).where(
                    AssessmentResult.attempt_id == attempt_id,
                    AssessmentResult.result_kind == AssessmentResultKind.FINAL,
                    AssessmentResult.supersedes_result_id.is_(None),
                )
            )
            .scalars()
            .first()
        )

    def effective_result(self, attempt_id: str) -> Optional[AssessmentResult]:
        """The result currently in force: the original final, or the latest
        human decision in its supersession chain. History is never edited."""
        current = self.original_final(attempt_id)
        while current is not None:
            nxt = (
                self.db.execute(
                    select(AssessmentResult).where(AssessmentResult.supersedes_result_id == current.id)
                )
                .scalars()
                .first()
            )
            if nxt is None:
                return current
            current = nxt
        return None

    def _links(self, definition_id: str) -> List[AssessmentDefinitionConcept]:
        return self.definitions.links(definition_id)

    # -- readiness ----------------------------------------------------------------------------------

    def _project_attempt_for(
        self, user_id: str, definition: AssessmentDefinition, project_attempt_id: Optional[str]
    ) -> Optional[ProjectAttempt]:
        if project_attempt_id:
            pa = self.db.execute(
                select(ProjectAttempt).where(
                    ProjectAttempt.id == project_attempt_id, ProjectAttempt.user_id == user_id
                )
            ).scalar_one_or_none()
            if pa is None:
                raise NotFoundError("Project attempt not found.")
            return pa
        if definition.project_template_id:
            return (
                self.db.execute(
                    select(ProjectAttempt)
                    .where(
                        ProjectAttempt.user_id == user_id,
                        ProjectAttempt.project_template_id == definition.project_template_id,
                    )
                    .order_by(ProjectAttempt.created_at.desc())
                )
                .scalars()
                .first()
            )
        return None

    def readiness(
        self, user: User, definition_key: str, *, project_attempt_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """A deterministic pre-flight. Never a result; every unmet item says what to do."""
        guard = AssessmentModeGuard(self.db, now=self._clock())
        guard.expire_stale(user.id)
        definition = self.definitions.current(definition_key)
        checks: List[Dict[str, Any]] = []

        def add(key: str, label: str, met: bool, detail: str = "", **extra):
            checks.append({"key": key, "label": label, "met": bool(met), "detail": detail, **extra})

        add(
            "published",
            "This assessment is published",
            definition is not None,
            "" if definition else "No published version of this assessment exists.",
        )
        if definition is None:
            return {"ready": False, "checks": checks, "definition": None}

        add(
            "capability",
            "Nothing extra is needed from the platform",
            definition.requires_platform_capability is None,
            "" if definition.requires_platform_capability is None else "Available after MA9.",
        )

        pa = self._project_attempt_for(user.id, definition, project_attempt_id)
        submission = None
        if definition.project_template_id:
            if pa is not None:
                submission = self.db.execute(
                    select(AssessmentReadySubmission).where(
                        AssessmentReadySubmission.project_attempt_id == pa.id,
                        AssessmentReadySubmission.user_id == user.id,
                    )
                ).scalar_one_or_none()
            add(
                "project_submission",
                "Your project is submitted for assessment",
                submission is not None and submission.status == "ready",
                "" if submission else "Submit your project from Build With Me first.",
                link={"kind": "project", "project_attempt_id": pa.id if pa else None},
            )

        indep = definition.independence_policy or {}
        graph_states = LearnerStateService(self.db)
        for req in (indep.get("readiness") or {}).get("prerequisite_states", []):
            state = graph_states.state(user.id, req["concept_id"], now=self._clock())
            add(
                f"prerequisite:{req['concept_id']}",
                f"Reach {req['min_state'].replace('_', ' ')} on a prerequisite Concept",
                state.is_at_least(req["min_state"]),
                f"Currently {state.ladder}.",
                link={"kind": "concept", "concept_id": req["concept_id"]},
            )

        active = (
            self.db.execute(
                select(AssessmentAttempt).where(
                    AssessmentAttempt.user_id == user.id,
                    AssessmentAttempt.definition_id.in_(
                        select(AssessmentDefinition.id).where(
                            AssessmentDefinition.definition_key == definition.definition_key
                        )
                    ),
                    AssessmentAttempt.status.in_(
                        [AssessmentAttemptStatus(s) for s in ACTIVE_ATTEMPT_STATUSES]
                    ),
                )
            )
            .scalars()
            .first()
        )
        add(
            "no_active_attempt",
            "You have no assessment of this kind in progress",
            active is None,
            "" if active is None else "Resume your attempt instead.",
            resume_attempt_id=active.id if active else None,
        )

        cooldown_hours = (definition.grading_policy or {}).get("cooldown_hours", 12)
        available_at = self._cooldown_ends(user.id, definition.definition_key, cooldown_hours)
        add(
            "cooldown",
            "You have waited long enough since your last attempt",
            available_at is None or available_at <= self._clock(),
            "" if available_at is None else f"You can try again after {available_at.isoformat()}.",
            available_at=available_at.isoformat() if available_at else None,
        )

        fresh_policy = indep.get("fresh_required", "if_assisted")
        source = self._source_work(pa) if pa else {"levels": [], "study_mode_used": False}
        levels = [AssistanceLevel(l) for l in source["levels"]]
        fresh, reason = policy.fresh_challenge_required(
            fresh_policy,
            source_levels=levels,
            study_mode_used=source["study_mode_used"],
            kind_is_capstone=definition.assessment_kind == AssessmentKind.CAPSTONE,
        )
        needs_pool = fresh or definition.assessment_kind in (
            AssessmentKind.KNOWLEDGE_CHECK,
            AssessmentKind.EXPLAIN_BACK,
        )
        add(
            "fresh_challenge",
            "A fresh challenge is available" if needs_pool else "No fresh challenge is needed",
            (not needs_pool) or bool(definition.challenge_spec),
            (
                ""
                if (not needs_pool or definition.challenge_spec)
                else "This assessment needs a fresh challenge but none has been authored."
            ),
        )
        return {
            "ready": all(c["met"] for c in checks),
            "checks": checks,
            "definition": AssessmentDefinitionService.learner_view(definition, self._links(definition.id)),
            "fresh_required": bool(fresh),
            "fresh_reason": reason,
            "source_work": {"levels": source["levels"], "study_mode_used": source["study_mode_used"]},
            "project_attempt_id": pa.id if pa else None,
        }

    def _cooldown_ends(self, user_id: str, definition_key: str, hours: int) -> Optional[datetime]:
        """The retry cooldown follows a NEEDS_WORK outcome only. A provisional,
        review-pending or could-not-assess result is not the learner's doing, and a
        human override to PASSED (the effective result) lifts it entirely."""
        last = (
            self.db.execute(
                select(AssessmentAttempt)
                .join(AssessmentDefinition, AssessmentDefinition.id == AssessmentAttempt.definition_id)
                .where(
                    AssessmentAttempt.user_id == user_id,
                    AssessmentDefinition.definition_key == definition_key,
                    AssessmentAttempt.status == AssessmentAttemptStatus.FINALIZED,
                )
                .order_by(AssessmentAttempt.finalized_at.desc())
            )
            .scalars()
            .first()
        )
        if last is None or last.finalized_at is None:
            return None
        effective = self.effective_result(last.id)
        if effective is None or effective.outcome != AssessmentOutcome.NEEDS_WORK:
            return None
        return aware(last.finalized_at) + timedelta(hours=hours)

    # -- source work / manifest ------------------------------------------------------------------------------

    def _source_work(self, pa: ProjectAttempt) -> Dict[str, Any]:
        """Assistance provenance re-derived from platform records (milestone
        attempts and candidate evidence) — never from the 5B snapshot's claims."""
        milestones = list(
            self.db.execute(
                select(MilestoneAttempt).where(MilestoneAttempt.project_attempt_id == pa.id)
            ).scalars()
        )
        levels = sorted(
            {m.max_assistance_level.value for m in milestones if m.max_assistance_level is not None}
        )
        cands = self.db.execute(
            select(CandidateEvidence.assistance_level).where(
                CandidateEvidence.project_attempt_id == pa.id, CandidateEvidence.user_id == pa.user_id
            )
        ).scalars()
        levels = sorted(set(levels) | {c.value for c in cands if c is not None})
        study = any(
            m.mode in (MilestoneAttemptMode.STUDY, MilestoneAttemptMode.VARIANT)
            or m.status == MilestoneAttemptStatus.SKIPPED_STUDY_MODE
            for m in milestones
        )
        return {"levels": levels, "study_mode_used": study}

    def _build_manifest(
        self, user: User, definition: AssessmentDefinition, pa: Optional[ProjectAttempt]
    ) -> Dict[str, Any]:
        manifest: Dict[str, Any] = {
            "schema": "assessment_manifest_v1",
            "definition": {
                "id": definition.id,
                "key": definition.definition_key,
                "version": definition.version,
                "content_hash": definition.content_hash,
            },
            "project": None,
            "source_submission": None,
            "milestone_attempts": [],
            "candidate_evidence": [],
            "learning_evidence_ids": [],
            "explain_back_ids": [],
            "experiments": [],
            "evaluation_runs": [],
            "source_work": {"levels": [], "study_mode_used": False},
            "records": [],
        }
        if pa is None:
            return manifest
        template = self.db.get(ProjectTemplate, pa.project_template_id)
        program_version_id = None
        if pa.enrollment_id:
            from app.models.academy import AcademyEnrollment

            enrollment = self.db.get(AcademyEnrollment, pa.enrollment_id)
            program_version_id = (
                enrollment.program_version_id if enrollment and enrollment.user_id == user.id else None
            )
        agent_version_ids: List[str] = []
        if pa.learner_agent_id:
            agent_version_ids = list(
                self.db.execute(
                    select(AgentVersion.id).where(AgentVersion.agent_id == pa.learner_agent_id)
                ).scalars()
            )
        manifest["project"] = {
            "project_attempt_id": pa.id,
            "template_id": pa.project_template_id,
            "template_version": template.version if template else None,
            "program_version_id": program_version_id,
            "learner_agent_id": pa.learner_agent_id,
            "learner_agent_version_ids": agent_version_ids,
            "is_capstone": pa.is_capstone,
            "brief_snapshot_hash": sha256_hex(pa.brief_snapshot or {}),
        }
        submission = self.db.execute(
            select(AssessmentReadySubmission).where(
                AssessmentReadySubmission.project_attempt_id == pa.id,
                AssessmentReadySubmission.user_id == user.id,
            )
        ).scalar_one_or_none()
        if submission is not None:
            manifest["source_submission"] = {
                "id": submission.id,
                "status": submission.status,
                "snapshot_hash": sha256_hex(submission.snapshot or {}),
            }

        milestones = list(
            self.db.execute(
                select(MilestoneAttempt)
                .where(MilestoneAttempt.project_attempt_id == pa.id)
                .order_by(MilestoneAttempt.created_at)
            ).scalars()
        )
        manifest["milestone_attempts"] = [
            {
                "id": m.id,
                "milestone_id": m.project_milestone_id,
                "status": m.status.value,
                "mode": m.mode.value,
                "max_assistance_level": m.max_assistance_level.value if m.max_assistance_level else None,
            }
            for m in milestones
        ]
        cands = list(
            self.db.execute(
                select(CandidateEvidence).where(
                    CandidateEvidence.project_attempt_id == pa.id, CandidateEvidence.user_id == user.id
                )
            ).scalars()
        )
        manifest["candidate_evidence"] = [
            {
                "id": c.id,
                "source_type": c.source_type,
                "source_id": c.source_id,
                "claimed_passed": c.passed,
                "assistance_level": c.assistance_level.value if c.assistance_level else None,
                "execution_verification_claim": (
                    c.execution_verification.value if c.execution_verification else None
                ),
                "learning_evidence_id": c.learning_evidence_id,
            }
            for c in cands
        ]
        evidence_ids = [c.learning_evidence_id for c in cands if c.learning_evidence_id]
        if evidence_ids:
            owned = self.db.execute(
                select(LearningEvidence.id).where(
                    LearningEvidence.user_id == user.id, LearningEvidence.id.in_(evidence_ids)
                )
            ).scalars()
            manifest["learning_evidence_ids"] = sorted(owned)
        manifest["explain_back_ids"] = sorted(
            self.db.execute(
                select(ExplainBackResponse.id).where(
                    ExplainBackResponse.project_attempt_id == pa.id, ExplainBackResponse.user_id == user.id
                )
            ).scalars()
        )
        for link in self.db.execute(
            select(ProjectExperimentLink).where(ProjectExperimentLink.project_attempt_id == pa.id)
        ).scalars():
            experiment = self.db.get(Experiment, link.experiment_id)
            if experiment is not None and experiment.user_id == user.id:
                manifest["experiments"].append(
                    {
                        "experiment_id": experiment.id,
                        "link_id": link.id,
                        "learner_decision_recorded": bool(link.learner_decision),
                    }
                )
                manifest["records"].append(
                    {
                        "type": "experiment",
                        "id": experiment.id,
                        "facts": {"completed": experiment.status.value == "completed"},
                    }
                )
        if agent_version_ids:
            runs = self.db.execute(
                select(EvaluationRun)
                .join(AgentRun, AgentRun.id == EvaluationRun.subject_agent_run_id)
                .where(
                    EvaluationRun.requested_by_user_id == user.id,
                    AgentRun.agent_version_id.in_(agent_version_ids),
                )
                .order_by(EvaluationRun.created_at)
            ).scalars()
            for run in runs:
                findings = [r.finding.value for r in run.criterion_results]
                manifest["evaluation_runs"].append(
                    {
                        "id": run.id,
                        "status": run.status.value,
                        "subject_artifact_content_hash": run.subject_artifact_content_hash,
                    }
                )
                manifest["records"].append(
                    {
                        "type": "evaluation_run",
                        "id": run.id,
                        "facts": {
                            "has_not_met": "not_met" in findings,
                            "completed": run.status == EvaluationRunStatus.COMPLETED,
                        },
                    }
                )
        for m in milestones:
            manifest["records"].append({"type": "milestone_attempt", "id": m.id, "facts": {}})
        manifest["source_work"] = self._source_work(pa)
        manifest["records"].sort(key=lambda r: (r["type"], r["id"]))
        return manifest

    # -- start ------------------------------------------------------------------------------------------------------

    def start(
        self,
        user: User,
        definition_key: str,
        *,
        project_attempt_id: Optional[str] = None,
        previous_attempt_id: Optional[str] = None,
        origin: AssessmentOrigin = AssessmentOrigin.LEARNER_STARTED,
        idempotency_key: Optional[str] = None,
        bypass_cooldown: bool = False,
    ) -> AssessmentAttempt:
        if idempotency_key:
            replay = self.db.execute(
                select(AssessmentAttempt).where(
                    AssessmentAttempt.user_id == user.id, AssessmentAttempt.idempotency_key == idempotency_key
                )
            ).scalar_one_or_none()
            if replay is not None:
                return replay
        if previous_attempt_id:
            self.get_attempt(
                user.id, previous_attempt_id
            )  # ownership; retries never reach another learner's attempt
            if origin == AssessmentOrigin.LEARNER_STARTED:
                origin = AssessmentOrigin.RETRY

        report = self.readiness(user, definition_key, project_attempt_id=project_attempt_id)
        unmet = [
            c for c in report["checks"] if not c["met"] and not (bypass_cooldown and c["key"] == "cooldown")
        ]
        if unmet:
            raise AssessmentNotReady(
                "This assessment is not ready to start: " + "; ".join(c["label"] for c in unmet),
                detail={"checks": report["checks"]},
            )
        definition = self.definitions.current(definition_key)
        links = self._links(definition.id)
        pa = self._project_attempt_for(user.id, definition, project_attempt_id)
        manifest = self._build_manifest(user, definition, pa)
        fresh, fresh_reason = report["fresh_required"], report["fresh_reason"]

        draw = draw_challenge(self.db, definition, user.id)
        if fresh and draw is None:
            raise AssessmentNotReady("A fresh challenge is required but none could be drawn.")
        if draw is None and definition.assessment_kind in (
            AssessmentKind.KNOWLEDGE_CHECK,
            AssessmentKind.EXPLAIN_BACK,
        ):
            raise AssessmentNotReady("No challenge could be drawn for this assessment.")

        graph = ConceptGraphService(self.db)
        pinned = {
            "definition": {
                "id": definition.id,
                "key": definition.definition_key,
                "version": definition.version,
                "content_hash": definition.content_hash,
            },
            "concepts": [
                {
                    "concept_id": l.concept_id,
                    "concept_version_id": l.concept_version_id,
                    "version": next(
                        (
                            v.version
                            for v in graph.list_versions(l.concept_id)
                            if v.id == l.concept_version_id
                        ),
                        None,
                    ),
                }
                for l in links
            ],
            "project_template": (
                {"id": pa.project_template_id, "version": (manifest["project"] or {}).get("template_version")}
                if pa
                else None
            ),
            "program_version_id": (manifest["project"] or {}).get("program_version_id"),
            "grading_contract_version": GRADING_CONTRACT_VERSION,
            "rubric_version": f"{definition.definition_key}@{definition.version}",
            "challenge": {"generator": draw.instance["generator"], "seed": draw.seed} if draw else None,
        }
        now = self._clock()
        expiry_hours = (definition.grading_policy or {}).get("expiry_hours", 24)
        attempt = AssessmentAttempt(
            user_id=user.id,
            definition_id=definition.id,
            origin=origin,
            project_attempt_id=pa.id if pa else None,
            source_submission_id=(manifest["source_submission"] or {}).get("id"),
            enrollment_id=pa.enrollment_id if pa else None,
            previous_attempt_id=previous_attempt_id,
            status=AssessmentAttemptStatus.DRAFT,
            pinned_versions=pinned,
            fresh_required=bool(fresh),
            fresh_reason=fresh_reason,
            challenge_seed=draw.seed if draw else None,
            challenge_instance=draw.instance if draw else None,
            draft={},
            input_manifest=manifest,
            input_manifest_hash=sha256_hex(manifest),
            started_at=now,
            expires_at=now + timedelta(hours=expiry_hours),
            idempotency_key=idempotency_key,
        )
        self.db.add(attempt)
        self.db.flush()
        self._audit(
            user,
            "assessment.started",
            attempt,
            {"definition": definition.definition_key, "version": definition.version, "origin": origin.value},
        )
        self.db.commit()
        self.db.refresh(attempt)
        return attempt

    # -- draft / abandon ---------------------------------------------------------------------------------------------------

    def save_draft(self, user_id: str, attempt_id: str, draft: Dict[str, Any]) -> AssessmentAttempt:
        attempt = self.get_attempt(user_id, attempt_id)
        AssessmentModeGuard(self.db, now=self._clock()).expire_stale(user_id)
        self.db.refresh(attempt)
        if not isinstance(draft, dict) or len(json.dumps(draft, default=str)) > MAX_DRAFT_CHARS:
            raise AssessmentInvalid("The draft must be a JSON object of reasonable size.")
        changed = self.db.execute(
            update(AssessmentAttempt)
            .where(
                AssessmentAttempt.id == attempt_id,
                AssessmentAttempt.user_id == user_id,
                AssessmentAttempt.status == AssessmentAttemptStatus.DRAFT,
            )
            .values(draft=draft)
            .execution_options(synchronize_session=False)
        ).rowcount
        if not changed:
            raise ConflictError("This attempt can no longer be edited.")
        self.db.commit()
        self.db.refresh(attempt)
        return attempt

    def abandon(self, user: User, attempt_id: str) -> AssessmentAttempt:
        """ "Leave assessment": releases the Mentor lock. The row and draft are kept."""
        attempt = self.get_attempt(user.id, attempt_id)
        changed = self.db.execute(
            update(AssessmentAttempt)
            .where(
                AssessmentAttempt.id == attempt_id,
                AssessmentAttempt.user_id == user.id,
                AssessmentAttempt.status == AssessmentAttemptStatus.DRAFT,
            )
            .values(status=AssessmentAttemptStatus.ABANDONED)
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed:
            self._audit(user, "assessment.abandoned", attempt, {})
            self.db.commit()
        self.db.refresh(attempt)
        return attempt

    # -- submit ----------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _valid_attestation(attestation: Any) -> Dict[str, Any]:
        if (
            not isinstance(attestation, dict)
            or attestation.get("declaration") not in policy.ATTESTATION_CHOICES
        ):
            raise AssessmentInvalid("Choose a declaration about the help you used before submitting.")
        note = attestation.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > ATTESTATION_NOTE_MAX):
            raise AssessmentInvalid("The declaration note is too long.")
        return {"declaration": attestation["declaration"], "note": note}

    def submit(
        self, user: User, attempt_id: str, attestation: Any, *, budget_id: Optional[str] = None
    ) -> AssessmentAttempt:
        attempt = self.get_attempt(user.id, attempt_id)
        if attempt.status == AssessmentAttemptStatus.ABANDONED and attempt.submitted_at is None:
            raise ConflictError("You left this assessment. Start a new attempt when you are ready.")
        if attempt.status != AssessmentAttemptStatus.DRAFT:
            return self._resume_processing(user, attempt, budget_id=budget_id)  # idempotent replay
        AssessmentModeGuard(self.db, now=self._clock()).expire_stale(user.id)
        self.db.refresh(attempt)
        if attempt.status != AssessmentAttemptStatus.DRAFT:
            raise ConflictError("This attempt has expired. Start a new attempt when you are ready.")
        attest = self._valid_attestation(attestation)
        if attempt.input_manifest_hash != sha256_hex(attempt.input_manifest):
            raise ConflictError("The frozen inputs of this attempt no longer match their hash.")
        submission = attempt.draft or {}
        frozen = self.db.execute(
            update(AssessmentAttempt)
            .where(
                AssessmentAttempt.id == attempt_id,
                AssessmentAttempt.user_id == user.id,
                AssessmentAttempt.status == AssessmentAttemptStatus.DRAFT,
            )
            .values(
                status=AssessmentAttemptStatus.SUBMITTED,
                submitted_at=self._clock(),
                submission=submission,
                submission_hash=sha256_hex(submission),
                attestation=attest,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if frozen:
            self._audit(user, "assessment.submitted", attempt, {"submission_hash": sha256_hex(submission)})
        self.db.commit()
        self.db.refresh(attempt)
        return self._resume_processing(user, attempt, budget_id=budget_id)

    def grade(self, user: User, attempt_id: str, *, budget_id: Optional[str] = None) -> AssessmentAttempt:
        """Resume / retry processing of a submitted attempt. Idempotent."""
        attempt = self.get_attempt(user.id, attempt_id)
        if attempt.status == AssessmentAttemptStatus.DRAFT:
            raise ConflictError("Submit the attempt before grading it.")
        return self._resume_processing(user, attempt, budget_id=budget_id)

    # -- pipeline ---------------------------------------------------------------------------------------------------------------------

    def _resume_processing(
        self, user: User, attempt: AssessmentAttempt, *, budget_id: Optional[str]
    ) -> AssessmentAttempt:
        if attempt.status in (AssessmentAttemptStatus.FINALIZED, AssessmentAttemptStatus.ABANDONED):
            return attempt
        definition = self.definitions.get(attempt.definition_id)
        links = self._links(definition.id)
        det = self._deterministic_result(attempt.id)
        if det is None:
            self._set_status(attempt, AssessmentAttemptStatus.CHECKING)
            det = self._run_deterministic(user, attempt, definition)
            if det is None:  # a check could not be evaluated: UNABLE_TO_ASSESS was finalized inside
                self.db.refresh(attempt)
                return attempt
        judged_defs = [c for c in definition.criteria if c["method"] == "grader"]
        required_det_failed = any(
            c["required"] and c["finding"] in ("partial", "not_met") for c in det.criteria
        )
        grading_status = GRADING_NOT_NEEDED
        judged_rows: List[Dict[str, Any]] = []
        grader_result: Optional[AssessmentResult] = None
        skipped: List[Dict[str, Any]] = []
        crosscheck_required = self._crosscheck_required(definition)
        if judged_defs and required_det_failed:
            skipped = [self._skipped_row(c) for c in judged_defs]
        elif judged_defs:
            from app.services.assessment_grader_service import AssessmentGraderService

            self._set_status(attempt, AssessmentAttemptStatus.GRADING)
            round_ = AssessmentGraderService(self.db, adapter_factory=self.adapter_factory).grade(
                user, attempt, definition, crosscheck_required=crosscheck_required, budget_id=budget_id
            )
            grading_status, judged_rows, grader_result = round_.status, round_.judged, round_.result

        independence = source_work_summary(
            attempt.input_manifest, is_authored_challenge(attempt.challenge_instance), attempt.attestation
        )
        independence["fresh_required"] = attempt.fresh_required
        independence["fresh_reason"] = attempt.fresh_reason
        agg = aggregate(
            deterministic=det.criteria,
            judged=judged_rows,
            grading_status=grading_status,
            crosscheck_required=crosscheck_required,
            independence=independence,
            attestation=attempt.attestation,
            evidence_is_execution=definition.produces_evidence_type in policy.EXECUTION_EVIDENCE_TYPES,
            definition_criteria=definition.criteria,
        )
        if agg.outcome is None:
            self._set_status(attempt, AssessmentAttemptStatus.AWAITING_GRADING)
            self.db.commit()
            self.db.refresh(attempt)
            return attempt
        return self._finalize(user, attempt, definition, links, det, judged_rows, skipped, grader_result, agg)

    @staticmethod
    def _crosscheck_required(definition: AssessmentDefinition) -> bool:
        mode = (definition.grading_policy or {}).get("crosscheck", "deciding")
        grader_required = any(
            c["method"] == "grader" and c.get("required", True) for c in definition.criteria
        )
        return mode == "always" or (mode == "deciding" and grader_required)

    @staticmethod
    def _skipped_row(criterion: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "key": criterion["key"],
            "label": criterion["label"],
            "method": "grader",
            "required": criterion.get("required", True),
            "finding": None,
            "status": "skipped",
            "detail": "Not judged, because a required platform check was not met.",
        }

    def _set_status(self, attempt: AssessmentAttempt, status: AssessmentAttemptStatus) -> None:
        self.db.execute(
            update(AssessmentAttempt)
            .where(
                AssessmentAttempt.id == attempt.id,
                AssessmentAttempt.status != AssessmentAttemptStatus.FINALIZED,
            )
            .values(status=status)
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        self.db.refresh(attempt)

    def _next_seq(self, attempt_id: str) -> int:
        return (
            self.db.execute(
                select(func.max(AssessmentResult.seq)).where(AssessmentResult.attempt_id == attempt_id)
            ).scalar()
            or 0
        ) + 1

    def _deterministic_result(self, attempt_id: str) -> Optional[AssessmentResult]:
        return (
            self.db.execute(
                select(AssessmentResult).where(
                    AssessmentResult.attempt_id == attempt_id,
                    AssessmentResult.result_kind == AssessmentResultKind.DETERMINISTIC,
                    AssessmentResult.round == 0,
                )
            )
            .scalars()
            .first()
        )

    def _run_deterministic(
        self, user: User, attempt: AssessmentAttempt, definition: AssessmentDefinition
    ) -> Optional[AssessmentResult]:
        ctx = CheckContext(
            db=self.db,
            user_id=user.id,
            attempt=attempt,
            definition=definition,
            manifest=attempt.input_manifest,
            submission=attempt.submission or {},
            challenge=attempt.challenge_instance or {},
        )
        rows: List[Dict[str, Any]] = []
        try:
            for criterion in definition.criteria:
                if criterion["method"] != "deterministic":
                    continue
                outcome = run_check(ctx, criterion)
                rows.append(
                    {
                        "key": criterion["key"],
                        "label": criterion["label"],
                        "method": "deterministic",
                        "required": criterion.get("required", True),
                        "check_type": criterion["check"]["type"],
                        "finding": outcome.finding.value,
                        "detail": outcome.detail,
                        "facts": outcome.facts,
                        "refs": outcome.refs,
                    }
                )
        except CheckUnavailable as exc:
            self._finalize_unable(user, attempt, definition, str(exc))
            return None
        result = AssessmentResult(
            attempt_id=attempt.id,
            user_id=user.id,
            seq=self._next_seq(attempt.id),
            round=0,
            result_kind=AssessmentResultKind.DETERMINISTIC,
            criteria=rows,
            facts={"manifest_hash": attempt.input_manifest_hash, "submission_hash": attempt.submission_hash},
        )
        self.db.add(result)
        self.db.commit()
        self.db.refresh(result)
        return result

    # -- finalize (one transaction) -----------------------------------------------------------------------------------------------------------------

    def _finalize_unable(
        self, user: User, attempt: AssessmentAttempt, definition: AssessmentDefinition, detail: str
    ) -> None:
        from app.assessment_outcome import Aggregate
        from app.db.enums import DemonstrationEffect

        agg = Aggregate(
            outcome=AssessmentOutcome.UNABLE_TO_ASSESS,
            effect=DemonstrationEffect.NONE,
            reason_code="check_unavailable",
            independence={},
        )
        agg.remediation = [{"kind": "retry", "label": "Try again later"}]
        self._finalize(
            user, attempt, definition, self._links(definition.id), None, [], [], None, agg, note=detail
        )

    def _finalize(
        self,
        user: User,
        attempt: AssessmentAttempt,
        definition: AssessmentDefinition,
        links: List[AssessmentDefinitionConcept],
        det: Optional[AssessmentResult],
        judged_rows: List[Dict[str, Any]],
        skipped_rows: List[Dict[str, Any]],
        grader_result: Optional[AssessmentResult],
        agg,
        note: str = "",
    ) -> AssessmentAttempt:
        from app.db.enums import AssessmentResultKind as Kind
        from app.models.assessment import AssessmentResult as Result
        from app.services.assessment_report_service import AssessmentReportService

        # Compare-and-swap: exactly one finalizer wins; a replay returns the same final.
        won = self.db.execute(
            update(AssessmentAttempt)
            .where(
                AssessmentAttempt.id == attempt.id,
                AssessmentAttempt.user_id == user.id,
                AssessmentAttempt.status.in_(
                    [
                        AssessmentAttemptStatus.SUBMITTED,
                        AssessmentAttemptStatus.CHECKING,
                        AssessmentAttemptStatus.GRADING,
                        AssessmentAttemptStatus.AWAITING_GRADING,
                    ]
                ),
            )
            .values(status=AssessmentAttemptStatus.FINALIZED, finalized_at=self._clock())
            .execution_options(synchronize_session=False)
        ).rowcount
        if not won:
            self.db.rollback()
            self.db.refresh(attempt)
            return attempt

        try:
            states = LearnerStateService(self.db)
            concept_ids = [l.concept_id for l in links]
            before = {cid: states.state(user.id, cid, now=self._clock()).ladder for cid in concept_ids}
            criteria_rows = (det.criteria if det else []) + judged_rows + skipped_rows
            facts = {
                "manifest_hash": attempt.input_manifest_hash,
                "submission_hash": attempt.submission_hash,
                "pinned_versions": attempt.pinned_versions,
                "independence": agg.independence,
                "execution_verification": agg.execution_verification.value,
                "reason_code": agg.reason_code,
                "note": note,
                "grader": (grader_result.facts if grader_result is not None else None),
            }
            final = Result(
                id=new_uuid(),  # known up front: the evidence rows cite it before the row is flushed
                attempt_id=attempt.id,
                user_id=user.id,
                seq=self._next_seq(attempt.id),
                round=0,
                result_kind=Kind.FINAL,
                outcome=agg.outcome,
                demonstration_effect=agg.effect,
                criteria=criteria_rows,
                facts=facts,
                gaps=agg.gaps,
                remediation=agg.remediation,
                grader_agent_run_ids=(
                    grader_result.grader_agent_run_ids if grader_result is not None else None
                ),
                grader_agent_version_id=(
                    grader_result.grader_agent_version_id if grader_result is not None else None
                ),
                grading_contract_version=(GRADING_CONTRACT_VERSION if grader_result is not None else None),
            )
            evidence = AssessmentEvidenceWriter(self.db).write(
                attempt=attempt, definition=definition, links=links, result=final
            )
            after = {cid: states.state(user.id, cid, now=self._clock()) for cid in concept_ids}
            AssessmentReportService(self.db).populate(
                final=final,
                attempt=attempt,
                definition=definition,
                links=links,
                evidence=evidence,
                state_before=before,
                state_after=after,
                now=self._clock(),
            )
            self.db.add(final)
            self.db.flush()
            if agg.outcome == AssessmentOutcome.HUMAN_REVIEW_REQUIRED:
                # Opened WITHOUT consent: a reviewer sees no content until the learner agrees.
                self.db.add(
                    AssessmentReview(
                        attempt_id=attempt.id,
                        result_id=final.id,
                        user_id=user.id,
                        trigger=AssessmentReviewTrigger.MODEL_DISAGREEMENT,
                        fingerprint=review_fingerprint(
                            final.id, attempt.input_manifest_hash, attempt.submission_hash
                        ),
                    )
                )
                self.db.flush()
            self._apply_project_effects(user, attempt, definition, agg)
            self._audit(
                user,
                "assessment.finalized",
                attempt,
                {
                    "result_id": final.id,
                    "outcome": agg.outcome.value,
                    "effect": agg.effect.value,
                    "reason": agg.reason_code,
                },
            )
            if evidence:
                self._audit(
                    user,
                    "assessment.evidence_written",
                    attempt,
                    {"result_id": final.id, "evidence_ids": [e.id for e in evidence]},
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(attempt)
        return attempt

    def _apply_project_effects(
        self, user: User, attempt: AssessmentAttempt, definition: AssessmentDefinition, agg
    ) -> None:
        """Only 5C writes 5B's project status and submission finalization."""
        if (
            definition.assessment_kind not in (AssessmentKind.PROJECT, AssessmentKind.CAPSTONE)
            or not attempt.project_attempt_id
        ):
            return
        pa = self.db.get(ProjectAttempt, attempt.project_attempt_id)
        if pa is None or pa.user_id != user.id:
            return
        if agg.outcome == AssessmentOutcome.PASSED:
            pa.status = ProjectAttemptStatus.PASSED
            if attempt.source_submission_id:
                submission = self.db.get(AssessmentReadySubmission, attempt.source_submission_id)
                if (
                    submission is not None
                    and submission.user_id == user.id
                    and submission.finalized_at is None
                ):
                    submission.finalized_at = self._clock()
        elif agg.outcome == AssessmentOutcome.NEEDS_WORK:
            pa.status = ProjectAttemptStatus.NEEDS_WORK

    def _audit(self, user: User, event: str, attempt: AssessmentAttempt, detail: Dict[str, Any]) -> None:
        # Detail carries ids / hashes / outcomes only — never learner content.
        AuditService(self.db).record(
            org_id=user.org_id,
            event_type=event,
            actor_user_id=user.id,
            target_ref=f"assessment_attempt:{attempt.id}",
            detail=detail,
            commit=False,
        )

    # -- learner-safe views -----------------------------------------------------------------------------------------------------------------------------

    def attempt_view(self, attempt: AssessmentAttempt) -> Dict[str, Any]:
        definition = self.definitions.get(attempt.definition_id)
        return {
            "id": attempt.id,
            "definition": AssessmentDefinitionService.learner_view(definition, self._links(definition.id)),
            "status": attempt.status.value,
            "origin": attempt.origin.value,
            "fresh_required": attempt.fresh_required,
            "fresh_reason": attempt.fresh_reason,
            "challenge": public_challenge(attempt.challenge_instance),
            "draft": attempt.draft if attempt.status == AssessmentAttemptStatus.DRAFT else None,
            "started_at": attempt.started_at,
            "expires_at": attempt.expires_at,
            "submitted_at": attempt.submitted_at,
            "finalized_at": attempt.finalized_at,
            "previous_attempt_id": attempt.previous_attempt_id,
            "mentor_locked": attempt.status == AssessmentAttemptStatus.DRAFT,
            "pinned_versions": attempt.pinned_versions,
            "grader_agent_version_id": attempt.grader_agent_version_id,
            "project_attempt_id": attempt.project_attempt_id,
        }
