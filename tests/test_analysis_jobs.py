from __future__ import annotations

from uuid import uuid4

from app.db.models import Report
from app.db.session import SessionLocal, initialize_database
from app.services.analysis_jobs import advance_report_analysis_revision


def test_analysis_generation_increment_is_atomic_against_stale_orm_objects():
    initialize_database()
    report_id = str(uuid4())
    with SessionLocal() as setup_db:
        setup_db.add(Report(
            id=report_id, tracking_code=f"CIG-R-{uuid4().hex[:24].upper()}",
            title="Flood report generation check",
            description="Residents report rising water at the community health center.",
            disaster_type="flood", urgency="high", status="submitted", language="en",
            source_type="citizen", source_name="Test", provenance="submitted",
        ))
        setup_db.commit()

    first_db = SessionLocal()
    second_db = SessionLocal()
    try:
        first = first_db.get(Report, report_id)
        second = second_db.get(Report, report_id)
        assert first.analysis_revision == second.analysis_revision == 1

        assert advance_report_analysis_revision(first_db, first) == 2
        first_db.commit()
        # The second ORM object still started from generation 1. The database-side
        # increment must produce 3, not overwrite generation 2 with another 2.
        assert advance_report_analysis_revision(second_db, second) == 3
        second_db.commit()
    finally:
        first_db.close()
        second_db.close()

    with SessionLocal() as db:
        report = db.get(Report, report_id)
        assert report.analysis_revision == 3
        assert report.analyzed_revision == 0
        db.delete(report)
        db.commit()
