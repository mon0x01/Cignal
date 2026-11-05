"""Leakage-aware offline training for a supervised event-pair ranker.

The split key is an operator-supplied incident/campaign group. Random row splits
are intentionally not supported because near-duplicate pairs from one disaster
must not leak across train and holdout sets.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.intelligence.correlation import score_report_pair
from app.intelligence.event_pair_model import (
    ARTIFACT_SCHEMA,
    FEATURE_NAMES,
    extract_pair_features,
    pair_text,
)


class EventPairDatasetError(ValueError):
    """Raised when a labelled event-pair dataset is unsafe or malformed."""


def load_event_pair_jsonl(path: str | Path) -> tuple[list[dict], str]:
    source = Path(path)
    if not source.is_file():
        raise EventPairDatasetError(f"Dataset file not found: {source}")
    raw = source.read_bytes()
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    pair_groups: dict[tuple[str, str], str] = {}
    report_text_groups: dict[str, str] = {}
    for line_number, line in enumerate(raw.decode("utf-8-sig").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventPairDatasetError(f"Invalid JSON on line {line_number}") from exc
        if not isinstance(row, dict):
            raise EventPairDatasetError(f"Line {line_number} must be a JSON object")
        required = ("split_group", "text_a", "text_b", "same_event")
        missing = [key for key in required if key not in row]
        if missing:
            raise EventPairDatasetError(
                f"Line {line_number} missing fields: {', '.join(missing)}"
            )
        group = str(row["split_group"]).strip()
        text_a = str(row["text_a"]).strip()
        text_b = str(row["text_b"]).strip()
        label = row["same_event"]
        if not group:
            raise EventPairDatasetError(f"Line {line_number} has an empty split_group")
        if len(text_a) < 8 or len(text_b) < 8:
            raise EventPairDatasetError(f"Line {line_number} texts must each have 8+ characters")
        if label not in (0, 1, False, True):
            raise EventPairDatasetError(f"Line {line_number} same_event must be 0 or 1")
        cleaned = dict(row)
        cleaned.update({
            "split_group": group,
            "text_a": text_a,
            "text_b": text_b,
            "same_event": int(label),
        })
        full_text_a = pair_text(_as_pair_item(cleaned, "a")).casefold()
        full_text_b = pair_text(_as_pair_item(cleaned, "b")).casefold()
        pair_key = tuple(sorted((full_text_a, full_text_b)))
        previous_group = pair_groups.get(pair_key)
        if previous_group is not None and previous_group != group:
            raise EventPairDatasetError(
                "Identical text pair appears under multiple split_group values; "
                "resolve the grouping to prevent exact-pair leakage."
            )
        pair_groups[pair_key] = group
        for report_text in (full_text_a, full_text_b):
            previous_report_group = report_text_groups.get(report_text)
            if previous_report_group is not None and previous_report_group != group:
                raise EventPairDatasetError(
                    "Identical source report content appears under multiple split_group values; "
                    "resolve report-level leakage before training."
                )
            report_text_groups[report_text] = group
        identity = (group, pair_key[0], pair_key[1])
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(cleaned)
    if len(rows) < 12:
        raise EventPairDatasetError("At least 12 unique labelled pairs are required")
    if len({row["same_event"] for row in rows}) != 2:
        raise EventPairDatasetError("Dataset must contain both same-event and different-event pairs")
    if len({row["split_group"] for row in rows}) < 4:
        raise EventPairDatasetError("At least four independent split_group values are required")
    return rows, hashlib.sha256(raw).hexdigest()


def validate_external_evaluation_groups(payload: dict, rows: list[dict]) -> None:
    """Fail closed if an evaluation set shares incident groups with training."""
    trained_groups = set(payload.get("training_split_groups", []))
    evaluation_groups = {str(row["split_group"]) for row in rows}
    if not trained_groups:
        raise EventPairDatasetError(
            "Model artifact has no recorded training split groups; cannot verify holdout independence."
        )
    overlap = sorted(trained_groups & evaluation_groups)
    if overlap:
        preview = ", ".join(overlap[:5])
        raise EventPairDatasetError(
            "Evaluation groups overlap model training groups; use untouched event groups. "
            f"Overlap: {preview}"
        )


def _as_pair_item(row: dict, side: str) -> SimpleNamespace:
    return SimpleNamespace(
        title=row.get(f"title_{side}", ""),
        description=row.get(f"text_{side}", ""),
        disaster_type=row.get(f"disaster_type_{side}", "unknown"),
        latitude=row.get(f"latitude_{side}"),
        longitude=row.get(f"longitude_{side}"),
        source_observed_at=_parse_time(row.get(f"observed_at_{side}")),
        created_at=_parse_time(row.get(f"observed_at_{side}")),
        language=row.get(f"language_{side}", "unknown"),
    )


def _parse_time(value: Any):
    if value is None or value == "":
        return None
    from datetime import datetime

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _training_texts(rows: list[dict]) -> list[str]:
    """Fit vocabulary on the same title+description text seen by feature extraction."""
    return [
        pair_text(_as_pair_item(row, side))
        for row in rows
        for side in ("a", "b")
    ]


def _make_vectorizer():
    from sklearn.feature_extraction.text import TfidfVectorizer

    return TfidfVectorizer(
        analyzer="char",
        ngram_range=(2, 5),
        min_df=1,
        max_features=40000,
        sublinear_tf=True,
    )


def _features(rows: list[dict], vectorizer) -> list[list[float]]:
    feature_rows = []
    for row in rows:
        first = _as_pair_item(row, "a")
        second = _as_pair_item(row, "b")
        feature_rows.append(extract_pair_features(first, second, vectorizer))
    return feature_rows


def _baseline_scores(rows: list[dict]) -> list[float]:
    """Score the same holdout pairs with Cignal's current heuristic ranker."""
    return [
        float(score_report_pair(_as_pair_item(row, "a"), _as_pair_item(row, "b"))["score"])
        for row in rows
    ]


def _group_holdout(rows: list[dict], test_fraction: float, random_state: int):
    from sklearn.model_selection import GroupShuffleSplit

    groups = [row["split_group"] for row in rows]
    labels = [row["same_event"] for row in rows]
    for offset in range(100):
        splitter = GroupShuffleSplit(
            n_splits=1,
            test_size=test_fraction,
            random_state=random_state + offset,
        )
        train_indices, test_indices = next(splitter.split(rows, labels, groups))
        train_labels = {labels[index] for index in train_indices}
        test_labels = {labels[index] for index in test_indices}
        if train_labels == {0, 1} and test_labels == {0, 1}:
            return train_indices, test_indices, random_state + offset
    raise EventPairDatasetError(
        "Unable to create a group-disjoint holdout containing both labels; "
        "add more independent split_group values for both classes."
    )


def _classification_metrics(labels, scores, threshold: float) -> dict:
    from sklearn.metrics import (
        average_precision_score,
        balanced_accuracy_score,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    predictions = [int(score >= threshold) for score in scores]
    matrix = confusion_matrix(labels, predictions, labels=[0, 1]).tolist()
    per_class_precision = precision_score(
        labels, predictions, labels=[0, 1], average=None, zero_division=0
    ).tolist()
    per_class_recall = recall_score(
        labels, predictions, labels=[0, 1], average=None, zero_division=0
    ).tolist()
    return {
        "threshold": threshold,
        "precision": round(float(precision_score(labels, predictions, zero_division=0)), 4),
        "recall": round(float(recall_score(labels, predictions, zero_division=0)), 4),
        "f1": round(float(f1_score(labels, predictions, zero_division=0)), 4),
        "macro_f1": round(float(f1_score(labels, predictions, average="macro", zero_division=0)), 4),
        "balanced_accuracy": round(float(balanced_accuracy_score(labels, predictions)), 4),
        "per_class_precision_label_0_1": [round(float(value), 4) for value in per_class_precision],
        "per_class_recall_label_0_1": [round(float(value), 4) for value in per_class_recall],
        "average_precision": round(float(average_precision_score(labels, scores)), 4),
        "roc_auc": round(float(roc_auc_score(labels, scores)), 4),
        "confusion_matrix_tn_fp_fn_tp": matrix,
        "score_interpretation": "uncalibrated_classifier_score_not_probability",
    }


def train_event_pair_model(
    dataset_path: str | Path,
    output_path: str | Path,
    *,
    test_fraction: float = 0.25,
    random_state: int = 42,
    threshold: float = 0.5,
) -> dict:
    if not 0.1 <= test_fraction <= 0.4:
        raise ValueError("test_fraction must be between 0.1 and 0.4")
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    rows, dataset_hash = load_event_pair_jsonl(dataset_path)
    train_indices, test_indices, used_seed = _group_holdout(
        rows, test_fraction, random_state
    )
    train_rows = [rows[index] for index in train_indices]
    test_rows = [rows[index] for index in test_indices]

    vectorizer = _make_vectorizer()
    vectorizer.fit(_training_texts(train_rows))
    train_features = _features(train_rows, vectorizer)
    test_features = _features(test_rows, vectorizer)
    train_labels = [row["same_event"] for row in train_rows]
    test_labels = [row["same_event"] for row in test_rows]

    from sklearn.linear_model import LogisticRegression

    evaluation_classifier = LogisticRegression(
        max_iter=1500,
        class_weight="balanced",
        C=1.0,
        random_state=used_seed,
    )
    evaluation_classifier.fit(train_features, train_labels)
    positive_index = list(evaluation_classifier.classes_).index(1)
    test_scores = evaluation_classifier.predict_proba(test_features)[:, positive_index].tolist()
    metrics = _classification_metrics(test_labels, test_scores, threshold)
    baseline_scores = _baseline_scores(test_rows)
    baseline_metrics = _classification_metrics(test_labels, baseline_scores, threshold)
    comparison = {
        "f1_delta_vs_heuristic": round(metrics["f1"] - baseline_metrics["f1"], 4),
        "average_precision_delta_vs_heuristic": round(
            metrics["average_precision"] - baseline_metrics["average_precision"], 4
        ),
        "roc_auc_delta_vs_heuristic": round(metrics["roc_auc"] - baseline_metrics["roc_auc"], 4),
        "beats_heuristic_on_f1": metrics["f1"] > baseline_metrics["f1"],
        "interpretation": "Holdout comparison only; no statistical significance or calibration claim.",
    }

    # The held-out group metrics above are frozen before fitting the deployable
    # artifact on all labelled groups. No holdout observations leak into evaluation.
    final_vectorizer = _make_vectorizer()
    final_vectorizer.fit(_training_texts(rows))
    final_features = _features(rows, final_vectorizer)
    final_classifier = LogisticRegression(
        max_iter=1500,
        class_weight="balanced",
        C=1.0,
        random_state=used_seed,
    )
    final_classifier.fit(final_features, [row["same_event"] for row in rows])

    payload = {
        "schema": ARTIFACT_SCHEMA,
        "model_name": "cignal-event-pair-ranker",
        "model_version": f"event-pair-logreg-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        "feature_names": list(FEATURE_NAMES),
        "vectorizer": final_vectorizer,
        "classifier": final_classifier,
        "dataset_sha256": dataset_hash,
        "training_samples": len(rows),
        "training_split_groups": sorted({row["split_group"] for row in rows}),
        "holdout": {
            "train_samples": len(train_rows),
            "test_samples": len(test_rows),
            "train_groups": sorted({row["split_group"] for row in train_rows}),
            "test_groups": sorted({row["split_group"] for row in test_rows}),
            "random_state": used_seed,
            "metrics": metrics,
            "heuristic_baseline_metrics": baseline_metrics,
            "comparison_vs_heuristic": comparison,
        },
        "created_at": datetime.now(UTC).isoformat(),
        "decision_policy": "ranking_candidate_only_human_review_required",
    }
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        import joblib
    except ImportError as exc:
        raise RuntimeError("joblib is required to export the event-pair model") from exc
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temp_name)
    try:
        joblib.dump(payload, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "output_path": str(destination),
        "model_version": payload["model_version"],
        "dataset_sha256": dataset_hash,
        "training_samples": len(rows),
        "training_groups": len(payload["training_split_groups"]),
        "holdout": payload["holdout"],
    }
