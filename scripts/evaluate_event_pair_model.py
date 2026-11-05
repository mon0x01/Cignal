#!/usr/bin/env python3
"""Evaluate an event-pair artifact on a separate labelled JSONL dataset."""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.intelligence.event_pair_model import extract_pair_features
from app.intelligence.event_pair_training import (
    _as_pair_item,
    _baseline_scores,
    _classification_metrics,
    load_event_pair_jsonl,
    validate_external_evaluation_groups,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="Held-out labelled JSONL data")
    parser.add_argument("--model", required=True, help="Trained joblib artifact")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--persist", action="store_true", help="Persist benchmark metadata in Cignal PostgreSQL")
    args = parser.parse_args()
    if not 0.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be between 0 and 1")
    rows, digest = load_event_pair_jsonl(args.dataset)
    try:
        import joblib
    except ImportError as exc:
        raise SystemExit("joblib is required") from exc
    payload = joblib.load(Path(args.model))
    from app.intelligence.event_pair_model import ARTIFACT_SCHEMA, FEATURE_NAMES

    if payload.get("schema") != ARTIFACT_SCHEMA or tuple(payload.get("feature_names", ())) != FEATURE_NAMES:
        raise SystemExit("Model artifact schema does not match this evaluator")
    try:
        validate_external_evaluation_groups(payload, rows)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    features = [
        extract_pair_features(_as_pair_item(row, "a"), _as_pair_item(row, "b"), payload["vectorizer"])
        for row in rows
    ]
    classes = list(payload["classifier"].classes_)
    if 1 not in classes:
        raise SystemExit("Artifact has no positive class")
    scores = payload["classifier"].predict_proba(features)[:, classes.index(1)].tolist()
    labels = [row["same_event"] for row in rows]
    metrics = _classification_metrics(labels, scores, args.threshold)
    baseline_scores = _baseline_scores(rows)
    baseline_metrics = _classification_metrics(labels, baseline_scores, args.threshold)
    def ranking_at_k(targets, predicted_scores):
        order = sorted(range(len(predicted_scores)), key=lambda index: predicted_scores[index], reverse=True)
        positives = sum(targets)
        result = {}
        for k in (1, 5, 10, 20):
            actual_k = min(k, len(order))
            true_positives = sum(targets[index] for index in order[:actual_k])
            result[str(k)] = {
                "precision_at_k": round(true_positives / actual_k, 4) if actual_k else 0.0,
                "recall_at_k": round(true_positives / positives, 4) if positives else 0.0,
                "actual_k": actual_k,
            }
        return result
    metrics["ranking_at_k"] = ranking_at_k(labels, scores)
    baseline_metrics["ranking_at_k"] = ranking_at_k(labels, baseline_scores)
    comparison = {
        "f1_delta_vs_heuristic": round(metrics["f1"] - baseline_metrics["f1"], 4),
        "average_precision_delta_vs_heuristic": round(
            metrics["average_precision"] - baseline_metrics["average_precision"], 4
        ),
        "roc_auc_delta_vs_heuristic": round(metrics["roc_auc"] - baseline_metrics["roc_auc"], 4),
        "beats_heuristic_on_f1": metrics["f1"] > baseline_metrics["f1"],
        "interpretation": "External holdout comparison only; no statistical significance or calibration claim.",
    }
    result = {
        "model_version": payload.get("model_version"),
        "dataset_sha256": digest,
        "evaluation_samples": len(rows),
        "evaluation_groups": len({row["split_group"] for row in rows}),
        "metrics": metrics,
        "heuristic_baseline_metrics": baseline_metrics,
        "comparison_vs_heuristic": comparison,
        "decision_policy": "candidate_ranking_only_human_review_required",
        "artifact_sha256": hashlib.sha256(Path(args.model).read_bytes()).hexdigest(),
    }
    if args.persist:
        from app.db.models import ModelEvaluationRun
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            evaluation = ModelEvaluationRun(
                id=str(uuid.uuid4()), task_type="event_pair_classification",
                model_name="cignal-event-pair-ranker", model_version=str(payload.get("model_version") or "unknown"),
                dataset_sha256=digest, artifact_sha256=result["artifact_sha256"],
                sample_count=len(rows), group_count=len({row["split_group"] for row in rows}),
                metrics=metrics, baseline_metrics=baseline_metrics, comparison=comparison,
                evaluation_policy="external_group_disjoint_holdout_uncalibrated_scores",
                created_at=datetime.now(UTC),
            )
            db.add(evaluation)
            db.commit()
            result["evaluation_run_id"] = evaluation.id
        finally:
            db.close()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
