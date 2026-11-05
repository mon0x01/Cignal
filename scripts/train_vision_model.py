#!/usr/bin/env python3
"""Train Cignal's CPU-first image triage model from operator-labelled folders."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.config import settings
from app.intelligence.vision_training import VisionTrainingDataError, train_vision_classifier


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Folder containing one subfolder per image category")
    parser.add_argument("--output", type=Path, default=settings.ai_vision_model_path)
    parser.add_argument("--min-accuracy", type=float, default=0.60)
    parser.add_argument("--test-fraction", type=float, default=0.25)
    args = parser.parse_args()
    try:
        result = train_vision_classifier(
            args.dataset, args.output,
            min_accuracy=args.min_accuracy,
            test_fraction=args.test_fraction,
        )
    except (VisionTrainingDataError, RuntimeError, OSError) as exc:
        print(f"VISION TRAINING FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("Image model exported. Restart API and worker to load the trusted local artifact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
