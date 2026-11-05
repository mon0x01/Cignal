#!/usr/bin/env python3
"""Read-only network smoke tests for bounded public disaster data providers."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.integrations.eonet import NASAEONETEvents
from app.integrations.usgs import USGSEarthquakes


def main() -> int:
    checks = []
    now = datetime.now(UTC)
    usgs = USGSEarthquakes()
    eonet = NASAEONETEvents()
    operations = [
        ("usgs_live_feed", usgs.configured(), usgs.fetch),
        ("eonet_live_feed", eonet.configured(), eonet.fetch),
        ("usgs_historical_window", usgs.configured(), lambda: usgs.fetch_window(now - timedelta(days=2), now - timedelta(days=1), limit=100)),
        ("eonet_historical_window", eonet.configured(), lambda: eonet.fetch_window(now - timedelta(days=3), now - timedelta(days=2))),
    ]
    for name, enabled, operation in operations:
        if not enabled:
            checks.append({"name": name, "status": "skipped", "reason": "source_disabled"})
            continue
        try:
            records = operation()
            checks.append({"name": name, "status": "pass", "records": len(records)})
        except Exception as exc:
            checks.append({"name": name, "status": "fail", "error_type": type(exc).__name__})
    passed = sum(item["status"] in {"pass", "skipped"} for item in checks)
    failed = sum(item["status"] == "fail" for item in checks)
    result = {"checks": checks, "passed_or_skipped": passed, "failed": failed, "total": len(checks)}
    print(json.dumps(result, indent=2))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
