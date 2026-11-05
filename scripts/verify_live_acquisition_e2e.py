#!/usr/bin/env python3
"""Bounded live USGS backfill + repeat-run idempotency verification."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db.models import AcquisitionBackfillRun, Report
from app.db.session import SessionLocal, initialize_database
from app.worker import run_acquisition_backfill


def create_run(start: datetime, end: datetime) -> str:
    run_id = str(uuid4())
    with SessionLocal() as db:
        db.add(AcquisitionBackfillRun(
            id=run_id, source_type="usgs", start_at=start, end_at=end,
            window_days=1, cursor_at=start, page_offset=1, max_records=500,
            status="queued", created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ))
        db.commit()
    return run_id


def main() -> int:
    initialize_database()
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    # Use a closed UTC calendar day to avoid a moving end boundary.
    end = now - timedelta(days=1, microseconds=1)
    start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        before = db.query(Report).filter(
            Report.source_type == "usgs",
            Report.source_observed_at >= start,
            Report.source_observed_at <= end,
        ).count()
    first = run_acquisition_backfill(create_run(start, end))
    second = run_acquisition_backfill(create_run(start, end))
    with SessionLocal() as db:
        after = db.query(Report).filter(
            Report.source_type == "usgs",
            Report.source_observed_at >= start,
            Report.source_observed_at <= end,
        ).count()
    result = {
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "reports_before": before, "reports_after": after,
        "first_run": first, "repeat_run": second,
        "idempotency_no_new_reports_on_repeat": second.get("records_inserted") == 0,
    }
    print(json.dumps(result, indent=2))
    if first.get("status") != "completed" or second.get("status") != "completed":
        return 2
    if first.get("records_fetched", 0) <= 0:
        print("No provider observations were returned; this run cannot prove live ingestion.")
        return 4
    return 0 if result["idempotency_no_new_reports_on_repeat"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
