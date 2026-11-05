"""Transactional analyst review workflow for reports and verified incidents.

AI fields are advisory only. This service is invoked only after an authenticated
admin/analyst decision in the API layer.
"""
from __future__ import annotations

import secrets
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Incident, Report, ReportIncident, User, utcnow
from app.services.audit import record_audit
from app.services.events import publish_event
from app.services.notifications import notify_user

REVIEWABLE_REPORT_STATUSES = {"submitted", "analyzed", "needs_info"}
LINKABLE_INCIDENT_STATUSES = {"verified", "monitoring"}


def _lock_report(db: Session, report_id: str) -> Report:
    report = db.execute(
        select(Report).where(Report.id == report_id).with_for_update()
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


def _lock_incident(db: Session, incident_id: str) -> Incident:
    incident = db.execute(
        select(Incident).where(Incident.id == incident_id).with_for_update()
    ).scalar_one_or_none()
    if incident is None:
        raise HTTPException(status_code=404, detail="Target incident not found")
    if incident.status not in LINKABLE_INCIDENT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Reports can only be linked to verified or monitoring incidents; target is {incident.status}.",
        )
    return incident


def _attach_report(db: Session, report: Report, incident: Incident) -> None:
    """Attach exactly once while the incident row is locked by the caller."""
    if db.get(ReportIncident, (report.id, incident.id)) is not None:
        raise HTTPException(status_code=409, detail="This report is already linked to that incident")

    db.add(ReportIncident(
        report_id=report.id,
        incident_id=incident.id,
        relationship_type="supports",
    ))
    db.flush()

    # Derive the count from the association table instead of blindly incrementing.
    # The incident row lock serializes concurrent report links on PostgreSQL.
    incident.source_count = db.query(func.count(ReportIncident.report_id)).filter(
        ReportIncident.incident_id == incident.id
    ).scalar() or 0
    incident.updated_at = utcnow()
    if incident.provenance != report.provenance:
        incident.provenance = "mixed"
    if incident.latitude is None and report.latitude is not None:
        incident.latitude = report.latitude
        incident.longitude = report.longitude
        incident.location_text = (report.location_text or "")[:220] or None


def review_report(
    db: Session,
    report_id: str,
    *,
    decision: str,
    reviewer: User,
    request: Request,
    incident_id: str | None = None,
    incident_title: str | None = None,
    note: str | None = None,
) -> tuple[Report, Incident | None]:
    """Apply an analyst decision atomically and return the updated records."""
    report = _lock_report(db, report_id)
    previous_status = report.status
    clean_note = note.strip() if note else None
    incident: Incident | None = None

    if report.status == "rejected":
        raise HTTPException(status_code=409, detail="Rejected reports are terminal and cannot be reviewed again")
    if report.status == "verified" and decision != "link":
        raise HTTPException(
            status_code=409,
            detail="This report is already linked to a verified incident; use the link decision to associate another incident.",
        )
    if report.status not in REVIEWABLE_REPORT_STATUSES and not (report.status == "verified" and decision == "link"):
        raise HTTPException(status_code=409, detail=f"Report cannot be reviewed from status '{report.status}'")

    if decision == "reject":
        report.status = "rejected"
        if report.submitted_by:
            submitter = db.get(User, report.submitted_by)
            if submitter and submitter.is_active:
                notify_user(
                    db, submitter, "Report not accepted",
                    clean_note or "An analyst reviewed your report and did not accept it as an incident.",
                    "report", "/reports",
                )

    elif decision == "request_info":
        if report.submitted_by is None:
            raise HTTPException(
                status_code=409,
                detail="Anonymous and external-source reports cannot receive a direct follow-up request",
            )
        submitter = db.get(User, report.submitted_by)
        if not submitter or not submitter.is_active:
            raise HTTPException(status_code=409, detail="The report submitter is no longer an active account")
        report.status = "needs_info"
        notify_user(
            db, submitter, "Additional information requested",
            clean_note or "An analyst needs more information to assess your report. Open My reports to respond.",
            "report", "/reports",
        )

    elif decision in {"verify", "link"}:
        created_new_incident = False
        if decision == "link" and not incident_id:
            raise HTTPException(status_code=422, detail="incident_id is required when decision is 'link'")

        if incident_id:
            incident = _lock_incident(db, incident_id)
        else:
            # A verify decision without a target creates a new incident.
            created_new_incident = True
            incident = Incident(
                id=str(uuid4()),
                incident_code=f"CIG-I-{secrets.token_hex(4).upper()}",
                title=((incident_title or "").strip() or report.title)[:180],
                summary=report.description,
                disaster_type=report.ai_category or report.disaster_type,
                severity=report.ai_urgency or report.urgency,
                status="verified",
                latitude=report.latitude,
                longitude=report.longitude,
                location_text=(report.location_text or "")[:220] or None,
                source_count=0,
                confidence=report.ai_confidence if report.ai_confidence is not None else 0.5,
                provenance=report.provenance,
                verified_by=reviewer.id,
                verified_at=utcnow(),
            )
            db.add(incident)
            db.flush()

        _attach_report(db, report, incident)
        report.status = "verified"

        # Notify responder roles when a verified incident gains a new source.
        responders = db.query(User).filter(
            User.role == "responder", User.is_active.is_(True)
        ).all()
        notification_title = "Verified incident available" if created_new_incident else "Incident evidence updated"
        notification_body = (
            f"A report was linked to {incident.incident_code}: {incident.title}"
        )
        for responder in responders:
            notify_user(
                db, responder, notification_title, notification_body,
                "incident", "/incidents",
            )

        if report.submitted_by:
            submitter = db.get(User, report.submitted_by)
            if submitter and submitter.is_active:
                notify_user(
                    db, submitter, "Report reviewed",
                    f"Your report was linked to verified incident {incident.incident_code}.",
                    "report", "/reports",
                )
    else:
        raise HTTPException(status_code=422, detail="Unsupported review decision")

    record_audit(
        db,
        f"report.{decision}",
        "report",
        report.id,
        reviewer.id,
        request,
        {
            "note": clean_note,
            "incident_id": incident.id if incident else incident_id,
            "previous_status": previous_status,
            "new_status": report.status,
        },
    )
    db.commit()

    publish_event(
        "report.reviewed",
        {
            "report_id": report.id,
            "incident_id": incident.id if incident else None,
            "decision": decision,
            "status": report.status,
        },
    )
    if incident:
        publish_event("incident.updated", {"incident_id": incident.id, "report_id": report.id})

    return report, incident
