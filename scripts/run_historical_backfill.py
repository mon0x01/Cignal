#!/usr/bin/env python3
"""Create and execute one bounded historical acquisition run from the CLI."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db.models import AcquisitionBackfillRun
from app.db.session import SessionLocal, initialize_database
from app.worker import run_acquisition_backfill


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Datetime must include timezone, preferably Z/UTC")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("usgs", "nasa_eonet"), required=True)
    parser.add_argument("--start", required=True, type=_parse_datetime)
    parser.add_argument("--end", required=True, type=_parse_datetime)
    parser.add_argument("--window-days", type=int, default=1)
    parser.add_argument("--max-records", type=int, default=500)
    args = parser.parse_args()
    if args.start >= args.end:
        parser.error("--start must be earlier than --end")
    if args.end - args.start > timedelta(days=366):
        parser.error("Historical backfills are bounded to 366 days per run")
    if not 1 <= args.window_days <= 14:
        parser.error("--window-days must be in 1..14")
    if not 1 <= args.max_records <= 50000:
        parser.error("--max-records must be in 1..50000")
    initialize_database()
    run_id = str(uuid4())
    with SessionLocal() as db:
        run = AcquisitionBackfillRun(
            id=run_id, source_type=args.source, start_at=args.start, end_at=args.end,
            window_days=args.window_days, cursor_at=args.start, page_offset=1,
            max_records=args.max_records, status="queued", created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(run)
        db.commit()
    result = run_acquisition_backfill(run_id)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
