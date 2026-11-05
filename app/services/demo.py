from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import Incident, Report, ReportIncident, User

DEMO_PASSWORD = "Cignal-Demo-2026!"


def seed_demo_data(db: Session) -> None:
    if not settings.demo_mode or db.query(User).first():
        return
    users = [
        ("admin@cignal.demo", "Demo Administrator", "admin"),
        ("analyst@cignal.demo", "Demo Analyst", "analyst"),
        ("responder@cignal.demo", "Demo Responder", "responder"),
        ("citizen@cignal.demo", "Demo Citizen", "citizen"),
    ]
    user_map = {}
    for email, name, role in users:
        user = User(id=str(uuid4()), email=email, display_name=name, password_hash=hash_password(DEMO_PASSWORD), role=role)
        db.add(user)
        user_map[role] = user
    db.flush()

    samples = [
        ("Urban flooding reported near riverfront", "Water is rising quickly after intense rainfall. Several roads are partially submerged and residents report difficulty crossing the area.", "flood", "high", 23.0225, 72.5714, "Ahmedabad, Gujarat", "verified"),
        ("Earthquake felt in Kutch region", "Residents report strong shaking and minor items falling from shelves. Damage is not yet confirmed.", "earthquake", "moderate", 23.2419, 69.6669, "Bhuj, Gujarat", "verified"),
        ("Smoke visible near industrial area", "A large column of smoke is visible from the industrial estate. Fire service status is unconfirmed.", "fire", "critical", 22.3072, 73.1812, "Vadodara, Gujarat", "submitted"),
        ("Coastal high-wind advisory", "Strong winds and rough sea conditions reported along the coast. Fishing activity may be affected.", "cyclone", "moderate", 21.1702, 72.8311, "Surat, Gujarat", "analyzed"),
    ]
    incident_rows = []
    for idx, (title, desc, kind, severity, lat, lon, loc, state) in enumerate(samples):
        report = Report(
            id=str(uuid4()), tracking_code=f"CIG-R-{1001+idx}", submitted_by=user_map["citizen"].id,
            title=title, description=desc, disaster_type=kind, urgency=severity, status=state,
            latitude=lat, longitude=lon, location_text=loc, language="en", source_type="citizen",
            source_name="Demonstration citizen report", provenance="simulated",
            ai_category=kind, ai_urgency=severity, ai_confidence=0.82,
            ai_summary=f"Demonstration analysis: {kind} signal near {loc}.",
            created_at=datetime.now(UTC) - timedelta(hours=idx * 3 + 1),
        )
        db.add(report)
        if state == "verified":
            incident = Incident(
                id=str(uuid4()), incident_code=f"CIG-I-{2001+idx}", title=title,
                summary=desc, disaster_type=kind, severity=severity, status="verified",
                latitude=lat, longitude=lon, location_text=loc, source_count=1,
                confidence=0.86, provenance="simulated", verified_by=user_map["analyst"].id,
                verified_at=datetime.now(UTC) - timedelta(hours=idx),
            )
            db.add(incident)
            incident_rows.append((report, incident))
    db.flush()
    for report, incident in incident_rows:
        db.add(ReportIncident(report_id=report.id, incident_id=incident.id, relationship_type="supports"))
    db.commit()
