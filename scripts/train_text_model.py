#!/usr/bin/env python3
"""Train an operator-labelled Cignal disaster-text classifier."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.config import settings
from app.intelligence.training import TrainingDataError, train_text_classifier


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="CSV containing category,text or category,title,description")
    parser.add_argument("--output", type=Path, default=settings.ai_text_model_path)
    parser.add_argument("--min-accuracy", type=float, default=0.65)
    parser.add_argument("--test-fraction", type=float, default=0.2)
    args = parser.parse_args()
    try:
        result = train_text_classifier(
            args.dataset, args.output,
            min_accuracy=args.min_accuracy,
            test_fraction=args.test_fraction,
        )
    except (TrainingDataError, RuntimeError, OSError) as exc:
        print(f"TRAINING FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("Model export is complete. Restart API and worker so both processes load the new trusted artifact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
