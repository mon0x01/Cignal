"""Database-backed report analysis generation management."""
from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import Report


def advance_report_analysis_revision(db: Session, report: Report) -> int:
    """Atomically advance a report's analysis generation.

    A Python-side ``current + 1`` can lose increments when two evidence uploads
    race. This SQL expression is evaluated by the database while holding the row
    update lock, so each committed change gets a distinct monotonically increasing
    generation. The report instance is refreshed for subsequent enqueue calls.
    """
    db.flush()
    result = db.execute(
        update(Report)
        .where(Report.id == report.id)
        .values(analysis_revision=Report.analysis_revision + 1),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        raise LookupError("report_not_found_while_advancing_analysis_revision")
    db.refresh(report, attribute_names=["analysis_revision"])
    return max(1, int(report.analysis_revision or 1))
