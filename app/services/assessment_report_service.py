"""AIL.5C assessment report + Demonstration Record.

The report is a rendered view of the FINAL result with four separately
labelled blocks that are never blended:

    PLATFORM FACT      deterministic checks, verification, assistance provenance
    GRADER JUDGMENT    per-criterion finding, categorical confidence, model lineage
    LEARNER REFLECTION the learner's own attestation / reflection (stored, never graded)
    PROFESSOR COACHING a separate, on-demand panel — never part of the result

It answers the learner's questions in order (what did I demonstrate, what
proved it, what was independent, where I needed help, what needs work, what to
practise next, did my Learner State change, what to review later).

The Demonstration Record is an evidence-linked snapshot frozen with the result
(``record_snapshot`` + ``record_hash``). It is NOT a certificate or credential;
language is validator-enforced. Its *status* (valid / superseded by review /
changed since / review due) is derived on read — history is never edited.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assessment_contract import GRADING_CONTRACT_VERSION, sha256_hex
from app.db.enums import AssessmentOutcome, AssistanceLevel, DemonstrationEffect
from app.models.academy import ExplainBackResponse, ProjectTemplate
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentDefinition,
    AssessmentDefinitionConcept,
    AssessmentResult,
)
from app.models.concepts import Concept
from app.models.learner import LearningEvidence
from app.services import independence_policy as policy
from app.services.learner_state_service import CHANGED, LearnerStateService
from app.services.review_retention_service import REVIEW_DUE

NOTICE = "Not a certificate or credential."
BLOCKED_TERMS = re.compile(
    r"\b(certified|certification|accredited|accreditation|mastered|mastery|expert|professional-level|credential(?:ed)?)\b",
    re.IGNORECASE,
)
_HEADLINES = {
    (
        AssessmentOutcome.PASSED,
        DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED,
    ): "You passed this assessment independently. It is recorded as evidence for the Concept(s) below.",
    (
        AssessmentOutcome.PASSED,
        DemonstrationEffect.COUNTS_TOWARD_PRACTICED_ONLY,
    ): "You passed, and it counts as practice rather than demonstration, because the source work had substantial help or could not be fully verified.",
    (
        AssessmentOutcome.PASSED,
        DemonstrationEffect.FORMATIVE_ONLY,
    ): "You passed. This attempt is feedback only and is not recorded as evidence.",
    (
        AssessmentOutcome.NEEDS_WORK,
        DemonstrationEffect.NONE,
    ): "This needs more work. Here is exactly what to strengthen.",
    (
        AssessmentOutcome.PROVISIONAL,
        DemonstrationEffect.NONE,
    ): "There was not enough confidence to decide. Nothing changed. You can ask for a review or try again.",
    (
        AssessmentOutcome.HUMAN_REVIEW_REQUIRED,
        DemonstrationEffect.NONE,
    ): "Two judgments disagreed, so a person needs to review this. Nothing changed.",
    (
        AssessmentOutcome.UNABLE_TO_ASSESS,
        DemonstrationEffect.NONE,
    ): "We could not assess this right now. Nothing changed. You can try again.",
}


def assert_truthful_language(text: str) -> None:
    """Blocked terms: certified, accredited, mastered, expert, professional-level."""
    scrubbed = text.replace(NOTICE, "")
    found = BLOCKED_TERMS.search(scrubbed)
    if found:
        raise ValueError(f"Untruthful record language: {found.group(0)!r}")


def _level_phrase(level: Optional[str]) -> str:
    return {
        "h0": "independently (H0)",
        "h1": "with a conceptual clue (H1)",
        "h2": "with a targeted pointer (H2)",
        "h3": "with partial structure (H3)",
        "h4": "with a guided walkthrough (H4)",
        "h5": "after seeing the solution (H5)",
    }.get(level or "", "with no help recorded")


class AssessmentReportService:
    def __init__(self, db: Session):
        self.db = db

    # -- populate (called once, before the FINAL row is first flushed) -------------------------

    def populate(
        self,
        *,
        final: AssessmentResult,
        attempt: AssessmentAttempt,
        definition: AssessmentDefinition,
        links: List[AssessmentDefinitionConcept],
        evidence: List[LearningEvidence],
        state_before: Dict[str, str],
        state_after: Dict[str, Any],
        now: datetime,
    ) -> None:
        report = self._report(final, attempt, definition, links, evidence, state_before, state_after)
        # Only PLATFORM-AUTHORED sentences are validated: never the learner's own
        # words, the Grader's quotes, or authored Concept / project names.
        for sentence in (
            [report["headline"]]
            + report["answers"]["what_i_did_independently"]
            + report["answers"]["where_i_needed_help"]
        ):
            assert_truthful_language(sentence)
        final.report = report
        if (
            final.outcome == AssessmentOutcome.PASSED
            and final.demonstration_effect == DemonstrationEffect.COUNTS_TOWARD_DEMONSTRATED
        ):
            record = self._record(final, attempt, definition, links, evidence, state_after, now)
            for sentence in [record["title"], record["notice"]] + record["provenance"]["self_reported"]:
                assert_truthful_language(sentence)
            final.record_snapshot = record
            final.record_hash = sha256_hex(record)

    # -- the report ------------------------------------------------------------------------------

    def _concept_names(self, links) -> Dict[str, str]:
        return {
            l.concept_id: (
                self.db.get(Concept, l.concept_id).name
                if self.db.get(Concept, l.concept_id)
                else l.concept_id
            )
            for l in links
        }

    def _report(
        self, final, attempt, definition, links, evidence, state_before, state_after
    ) -> Dict[str, Any]:
        facts = final.facts or {}
        independence = facts.get("independence") or {}
        names = self._concept_names(links)
        det = [c for c in final.criteria if c.get("method") == "deterministic"]
        judged = [c for c in final.criteria if c.get("method") == "grader"]
        grader_facts = facts.get("grader") or {}
        levels = independence.get("source_levels") or []
        helped = [l for l in levels if policy.classify_assistance(AssistanceLevel(l)) != policy.FULL]

        formative = self._formative_explain_back(attempt)
        reflection = ((attempt.submission or {}).get("fields") or {}).get("reflection")

        answers = {
            "what_i_demonstrated": self._what_demonstrated(final, names),
            "what_evidence_proved_it": [
                {"label": c["label"], "detail": c.get("detail", ""), "source": "platform"}
                for c in det
                if c["finding"] == "met"
            ]
            + [
                {"label": c["label"], "detail": c.get("rationale", ""), "source": "grader"}
                for c in judged
                if c.get("finding") == "met"
            ],
            "what_i_did_independently": self._independent(independence),
            "where_i_needed_help": [f"Your source work needed help {_level_phrase(l)}." for l in helped]
            or (["No substantial help is recorded on your source work."] if levels else []),
            "what_needs_more_work": final.gaps or [],
            "what_to_practice_next": final.remediation or [],
            "learner_state": {
                cid: {
                    "concept_name": names.get(cid),
                    "before": state_before.get(cid),
                    "after": state_after[cid].ladder,
                    "overlays": sorted(state_after[cid].overlays),
                    "changed": state_before.get(cid) != state_after[cid].ladder,
                }
                for cid in state_after
            },
            "review_later": {
                cid: (
                    {
                        "eligible": r.eligible,
                        "next_review_at": r.due_at.isoformat() if r.due_at else None,
                        "due_now": r.due,
                    }
                    if (r := state_after[cid].review) is not None
                    else None
                )
                for cid in state_after
            },
        }
        return {
            "schema": "assessment_report_v1",
            "outcome": final.outcome.value,
            "demonstration_effect": final.demonstration_effect.value,
            "headline": _HEADLINES.get(
                (final.outcome, final.demonstration_effect),
                _HEADLINES[(AssessmentOutcome.NEEDS_WORK, DemonstrationEffect.NONE)],
            ),
            "reason_code": facts.get("reason_code"),
            "platform_fact": {
                "label": "PLATFORM FACT",
                "deterministic_checks": [
                    {
                        "key": c["key"],
                        "label": c["label"],
                        "required": c["required"],
                        "finding": c["finding"],
                        "detail": c.get("detail", ""),
                        "refs": c.get("refs", []),
                    }
                    for c in det
                ],
                "assistance": {
                    "source_levels": levels,
                    "source_max_assistance": independence.get("source_max_assistance"),
                    "source_classes": independence.get("source_classes", []),
                    "study_mode_used": independence.get("study_mode_used", False),
                },
                "independence": {
                    "basis": independence.get("independence_basis"),
                    "challenge_issued": independence.get("challenge_issued", False),
                    "fresh_required": independence.get("fresh_required", False),
                    "fresh_reason": independence.get("fresh_reason"),
                    "declaration": independence.get("declaration"),
                },
                "execution_verification": facts.get("execution_verification"),
                "manifest_hash": facts.get("manifest_hash"),
                "submission_hash": facts.get("submission_hash"),
            },
            "grader_judgment": {
                "label": "GRADER JUDGMENT",
                "ran": bool(judged),
                "criteria": [
                    {
                        k: c.get(k)
                        for k in (
                            "key",
                            "label",
                            "required",
                            "finding",
                            "confidence",
                            "rationale",
                            "quotes",
                            "gap",
                            "agreement",
                            "status",
                        )
                    }
                    for c in judged
                ],
                "grader_agent_version_id": final.grader_agent_version_id,
                "grading_contract_version": final.grading_contract_version
                or (GRADING_CONTRACT_VERSION if judged else None),
                "runs": grader_facts.get("runs", []),
                "crosscheck_ran": len(grader_facts.get("runs", [])) > 1,
            },
            "learner_reflection": {
                "label": "LEARNER REFLECTION",
                "attestation": attempt.attestation,
                "reflection": reflection,
                "earlier_explain_back": formative,
            },
            "professor_coaching": {
                "label": "PROFESSOR COACHING",
                "available_after_result": True,
                "note": "Coaching is separate and on demand. It is never part of this result and cannot change it.",
            },
            "answers": answers,
            "evidence_ids": [e.id for e in evidence],
        }

    @staticmethod
    def _what_demonstrated(final, names: Dict[str, str]) -> Dict[str, Any]:
        return {
            "outcome": final.outcome.value,
            "effect": final.demonstration_effect.value,
            "concepts": sorted(names.values()),
        }

    @staticmethod
    def _independent(independence: Dict[str, Any]) -> List[str]:
        out = []
        if independence.get("challenge_issued"):
            out.append(
                "You completed a fresh challenge in Assessment Mode with the Mentor locked (recorded as independent, H0)."
            )
        top = independence.get("source_max_assistance")
        if top and policy.classify_assistance(AssistanceLevel(top)) == policy.FULL:
            out.append(
                f"Your project work was done independently or with light help (up to H{policy.MAX_DEMONSTRATION_ASSISTANCE_RANK})."
            )
        return out

    def _formative_explain_back(self, attempt: AssessmentAttempt) -> List[Dict[str, Any]]:
        ids = (attempt.input_manifest or {}).get("explain_back_ids") or []
        if not ids:
            return []
        rows = self.db.execute(
            select(ExplainBackResponse).where(
                ExplainBackResponse.user_id == attempt.user_id, ExplainBackResponse.id.in_(ids)
            )
        ).scalars()
        return [
            {
                "id": r.id,
                "question": r.question,
                "response": r.response[:1200],
                "label": "Your earlier explanation (formative, never graded)",
                "assistance_level": r.assistance_level.value if r.assistance_level else None,
            }
            for r in rows
        ]

    # -- the Demonstration Record ----------------------------------------------------------------------

    def _record(
        self, final, attempt, definition, links, evidence, state_after, now: datetime
    ) -> Dict[str, Any]:
        facts = final.facts or {}
        independence = facts.get("independence") or {}
        names = self._concept_names(links)
        pinned = attempt.pinned_versions or {}
        template = None
        if definition.project_template_id:
            t = self.db.get(ProjectTemplate, definition.project_template_id)
            template = (
                {"template_id": t.id, "template_key": t.template_key, "title": t.title, "version": t.version}
                if t
                else None
            )
        det = [c for c in final.criteria if c.get("method") == "deterministic" and c["finding"] == "met"]
        refs = []
        for c in det:
            refs.extend(c.get("refs") or [])
        pinned_versions = {c["concept_id"]: c for c in pinned.get("concepts", [])}
        return {
            "schema": "demonstration_record_v1",
            "title": "Demonstration Record",
            "notice": NOTICE,
            "result_id": final.id,
            "attempt_id": attempt.id,
            "assessment_date": now.isoformat(),
            "assessment": {
                "kind": definition.assessment_kind.value,
                "definition_key": definition.definition_key,
                "definition_version": definition.version,
                "definition_content_hash": definition.content_hash,
                "rubric_version": pinned.get("rubric_version"),
                "grading_contract_version": final.grading_contract_version,
            },
            "concepts": [
                {
                    "concept_id": l.concept_id,
                    "concept_name": names[l.concept_id],
                    "concept_version_id": l.concept_version_id,
                    "concept_version": (pinned_versions.get(l.concept_id) or {}).get("version"),
                }
                for l in links
            ],
            "project": template,
            "program_version_id": pinned.get("program_version_id"),
            "evidence": [
                {
                    "learning_evidence_id": e.id,
                    "concept_id": e.concept_id,
                    "evidence_type": e.evidence_type.value,
                    "assistance_level": e.assistance_level.value if e.assistance_level else None,
                    "execution_verification": (
                        e.execution_verification.value if e.execution_verification else None
                    ),
                    "grader": e.grader.value,
                }
                for e in evidence
            ],
            "provenance": {
                "independence_basis": independence.get("independence_basis"),
                "challenge_issued": independence.get("challenge_issued"),
                "source_levels": independence.get("source_levels", []),
                "declaration": independence.get("declaration"),
                "verified_by_platform": [c["label"] for c in det],
                "self_reported": [
                    "Your declaration about the help you used is self-reported and was not verified."
                ],
            },
            "execution_verification": facts.get("execution_verification"),
            "grader": {
                "ran": bool(facts.get("grader")),
                "agent_version_id": final.grader_agent_version_id,
                "runs": (facts.get("grader") or {}).get("runs", []),
                "crosscheck_ran": len((facts.get("grader") or {}).get("runs", [])) > 1,
            },
            "artifact_refs": refs,
            "learner_state_at_record": {
                cid: {"ladder": s.ladder, "overlays": sorted(s.overlays)} for cid, s in state_after.items()
            },
        }

    @staticmethod
    def render_markdown(record: Dict[str, Any]) -> str:
        lines = [f"# {record['title']}", "", f"_{record['notice']}_", ""]
        assessment = record["assessment"]
        lines += [
            f"- Assessment: {assessment['kind'].replace('_', ' ')} ({assessment['definition_key']} v{assessment['definition_version']})",
            f"- Date: {record['assessment_date']}",
        ]
        for c in record["concepts"]:
            lines.append(f"- Concept: {c['concept_name']} (Concept Version {c.get('concept_version')})")
        if record.get("project"):
            p = record["project"]
            lines.append(f"- Project: {p['title']} (template v{p['version']})")
        lines += ["", "## Evidence", ""]
        for e in record["evidence"]:
            lines.append(
                f"- {e['evidence_type']} evidence, help level {e.get('assistance_level') or 'none recorded'}, "
                f"verification {e.get('execution_verification') or 'not applicable'}, judged by {e['grader']}"
            )
        prov = record["provenance"]
        lines += ["", "## Verified by the platform", ""] + [f"- {x}" for x in prov["verified_by_platform"]]
        lines += ["", "## Self-reported", ""] + [f"- {x}" for x in prov["self_reported"]]
        g = record["grader"]
        if g["ran"]:
            lines += [
                "",
                "## Grader",
                "",
                f"- Grader Agent Version: {g.get('agent_version_id')}",
                f"- Cross-check ran: {g['crosscheck_ran']}",
            ]
        return "\n".join(lines) + "\n"

    # -- derived-on-read record status ---------------------------------------------------------------------

    def record_status(
        self, user_id: str, final: AssessmentResult, *, superseded: bool, now: datetime
    ) -> Dict[str, Any]:
        """valid | superseded_by_review | changed_since | review_due. Derived, never stored."""
        if superseded:
            return {
                "status": "superseded_by_review",
                "reasons": ["A later review decision replaced this result. The history is kept."],
            }
        reasons: List[str] = []
        status = "valid"
        states = LearnerStateService(self.db)
        for concept in (final.record_snapshot or {}).get("concepts", []):
            state = states.state(user_id, concept["concept_id"], now=now)
            if CHANGED in state.overlays:
                status = "changed_since"
                reasons.append(
                    f"{concept['concept_name']} has a material change since Concept Version {concept.get('concept_version')}."
                )
            elif REVIEW_DUE in state.overlays and status == "valid":
                status = "review_due"
                reasons.append(f"A review of {concept['concept_name']} is due.")
        return {"status": status, "reasons": reasons}
