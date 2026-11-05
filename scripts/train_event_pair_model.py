#!/usr/bin/env python3
"""Train a supervised event-pair model from human-labelled JSONL pairs."""
from __future__ import annotations

import argparse
import json

from app.core.config import settings
from app.intelligence.event_pair_training import train_event_pair_model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="Labelled event-pair JSONL dataset")
    parser.add_argument("--output", default=str(settings.event_pair_model_path))
    parser.add_argument("--test-fraction", type=float, default=0.25)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()
    result = train_event_pair_model(
        args.dataset,
        args.output,
        test_fraction=args.test_fraction,
        random_state=args.random_state,
        threshold=args.threshold,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
