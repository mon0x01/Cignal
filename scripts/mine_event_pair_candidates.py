#!/usr/bin/env python3
"""Mine auditable unlabelled event-pair candidates for an active research dataset."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from uuid import uuid4

from app.db.models import PairCandidateMiningRun, ResearchDataset
from app.db.session import SessionLocal, initialize_database
from app.services.active_learning import mine_event_pair_candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-slug", required=True)
    parser.add_argument("--max-reports", type=int, default=1200)
    parser.add_argument("--window-hours", type=int, default=168)
    parser.add_argument("--max-distance-km", type=float, default=250.0)
    parser.add_argument("--max-pairs-scored", type=int, default=100000)
    parser.add_argument("--top-candidates", type=int, default=2000)
    args = parser.parse_args()
    initialize_database()
    with SessionLocal() as db:
        dataset = db.query(ResearchDataset).filter_by(slug=args.dataset_slug, status="active").one_or_none()
        if dataset is None:
            raise SystemExit(f"Active dataset not found: {args.dataset_slug}")
        run = PairCandidateMiningRun(
            id=str(uuid4()), dataset_id=dataset.id, status="running",
            parameters={
                "max_reports": args.max_reports, "window_hours": args.window_hours,
                "max_distance_km": args.max_distance_km,
                "max_pairs_scored": args.max_pairs_scored,
                "top_candidates": args.top_candidates,
            }, created_at=datetime.now(UTC),
        )
        db.add(run)
        db.commit()
        result = mine_event_pair_candidates(db, run_id=run.id)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
