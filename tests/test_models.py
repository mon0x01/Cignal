from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Evidence, Incident, IncidentActivity, Report, ReportIncident, User


def test_reports_evidence_and_incidents_are_separate_entities():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        report = Report(
            id="report-1", tracking_code="CIG-R-0001", title="Flood report",
            description="Water is rising near the market", disaster_type="flood",
            urgency="high", status="submitted", language="en", source_type="citizen",
            source_name="Citizen submission", provenance="submitted",
        )
        db.add(report)
        db.flush()
        evidence = Evidence(
            id="evidence-1", report_id=report.id, original_filename="photo.jpg",
            stored_key="reports/report-1/photo.jpg", mime_type="image/jpeg",
            sha256="a" * 64, size_bytes=123,
        )
        incident = Incident(
            id="incident-1", incident_code="CIG-I-0001", title="Market flooding",
            summary="Verified flood report", disaster_type="flood", severity="high",
            status="verified", source_count=1,
        )
        db.add_all([evidence, incident])
        db.add(ReportIncident(report_id=report.id, incident_id=incident.id, relationship_type="supports"))
        db.commit()
        assert db.get(Report, "report-1") is not None
        assert db.get(Evidence, "evidence-1").report_id == "report-1"
        assert db.get(Incident, "incident-1").source_count == 1
        assert db.query(ReportIncident).count() == 1
    engine.dispose()


def test_report_idempotency_key_is_unique():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Report(
            id="r1", tracking_code="CIG-R-0001", title="Flood one", description="Water rising in street",
            status="submitted", language="en", source_type="citizen", source_name="Citizen",
            idempotency_key="same-key",
        ))
        db.commit()
        db.add(Report(
            id="r2", tracking_code="CIG-R-0002", title="Flood two", description="Water rising elsewhere",
            status="submitted", language="en", source_type="citizen", source_name="Citizen",
            idempotency_key="same-key",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    engine.dispose()


def test_incident_activity_keeps_actor_snapshot_and_is_separate_from_incident_state():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        actor = User(
            id="user-1", email="responder@example.invalid", display_name="Field Responder",
            password_hash="not-used", role="responder", is_active=True, mfa_enabled=True,
        )
        incident = Incident(
            id="incident-1", incident_code="CIG-I-0001", title="Verified event",
            summary="Human-verified incident", disaster_type="flood", severity="high",
            status="monitoring", source_count=1,
        )
        db.add_all([actor, incident])
        db.flush()
        activity = IncidentActivity(
            id="activity-1", incident_id=incident.id, actor_user_id=actor.id,
            actor_display_name=actor.display_name, actor_role=actor.role,
            activity_type="situation_update", message="The water level is lower than the previous observation.",
        )
        db.add(activity)
        db.commit()
        stored = db.get(IncidentActivity, "activity-1")
        assert stored.actor_display_name == "Field Responder"
        assert stored.actor_role == "responder"
        assert db.get(Incident, incident.id).status == "monitoring"
        assert db.query(IncidentActivity).filter_by(incident_id=incident.id).count() == 1
    engine.dispose()
