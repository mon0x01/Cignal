"""Offline training utility for operator-supplied, human-labelled disaster text.

Runtime inference never learns from incoming reports. Only a separately reviewed
CSV can be used to train and export a versioned local model artifact.
"""
from __future__ import annotations

import csv
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ALLOWED_CATEGORIES = frozenset({"flood", "earthquake", "fire", "cyclone", "landslide", "other"})
MIN_SAMPLES_PER_CATEGORY = 5


class TrainingDataError(ValueError):
    """Raised when a training dataset is malformed or too small."""


def load_labeled_text_csv(path: str | Path) -> tuple[list[str], list[str], dict[str, int]]:
    source = Path(path)
    if not source.is_file():
        raise TrainingDataError(f"Dataset file not found: {source}")
    texts: list[str] = []
    labels: list[str] = []
    seen: set[tuple[str, str]] = set()
    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or [])
        if "category" not in fields or not ({"text"} <= fields or {"title", "description"} <= fields):
            raise TrainingDataError("CSV requires category and either text or both title and description columns")
        for line_number, row in enumerate(reader, start=2):
            label = (row.get("category") or "").strip().lower()
            if label not in ALLOWED_CATEGORIES:
                raise TrainingDataError(f"Unsupported category on CSV line {line_number}: {label!r}")
            text = (row.get("text") or "").strip()
            if not text:
                title = (row.get("title") or "").strip()
                description = (row.get("description") or "").strip()
                text = f"{title}. {description}".strip(" .")
            if len(text) < 10:
                raise TrainingDataError(f"Text on CSV line {line_number} must contain at least 10 characters")
            key = (label, " ".join(text.casefold().split()))
            if key in seen:
                continue
            seen.add(key)
            texts.append(text)
            labels.append(label)
    counts = dict(sorted(Counter(labels).items()))
    if len(counts) < 2:
        raise TrainingDataError("At least two distinct categories are required")
    insufficient = {label: count for label, count in counts.items() if count < MIN_SAMPLES_PER_CATEGORY}
    if insufficient:
        detail = ", ".join(f"{label}={count}" for label, count in insufficient.items())
        raise TrainingDataError(f"Each category requires at least {MIN_SAMPLES_PER_CATEGORY} unique samples: {detail}")
    return texts, labels, counts


def _build_pipeline():
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import FeatureUnion, Pipeline

    features = FeatureUnion([
        ("character", TfidfVectorizer(
            analyzer="char", ngram_range=(2, 5), min_df=1,
            max_features=30000, sublinear_tf=True,
        )),
        ("word", TfidfVectorizer(
            analyzer="word", ngram_range=(1, 2), min_df=1,
            max_features=15000, sublinear_tf=True,
        )),
    ])
    return Pipeline([
        ("features", features),
        ("classifier", LogisticRegression(
            max_iter=1000, class_weight="balanced", C=2.0, random_state=42,
        )),
    ])


def train_text_classifier(
    dataset_path: str | Path,
    output_path: str | Path,
    *,
    min_accuracy: float = 0.65,
    test_fraction: float = 0.2,
    random_state: int = 42,
) -> dict:
    """Evaluate on a stratified holdout, then train full data and atomically export."""
    if not 0.0 <= min_accuracy <= 1.0:
        raise ValueError("min_accuracy must be between 0 and 1")
    if not 0.1 <= test_fraction <= 0.4:
        raise ValueError("test_fraction must be between 0.1 and 0.4")

    texts, labels, counts = load_labeled_text_csv(dataset_path)
    try:
        import joblib
        from sklearn.metrics import accuracy_score, classification_report, f1_score
        from sklearn.model_selection import train_test_split
    except ImportError as exc:
        raise RuntimeError("scikit-learn and joblib are required for offline model training") from exc

    train_texts, test_texts, train_labels, test_labels = train_test_split(
        texts, labels, test_size=test_fraction, random_state=random_state, stratify=labels,
    )
    evaluation_model = _build_pipeline()
    evaluation_model.fit(train_texts, train_labels)
    predictions = evaluation_model.predict(test_texts)
    accuracy = float(accuracy_score(test_labels, predictions))
    macro_f1 = float(f1_score(test_labels, predictions, average="macro", zero_division=0))
    report = classification_report(test_labels, predictions, output_dict=True, zero_division=0)
    if accuracy < min_accuracy:
        raise TrainingDataError(
            f"Holdout accuracy {accuracy:.3f} is below required threshold {min_accuracy:.3f}; artifact not exported"
        )

    final_model = _build_pipeline()
    final_model.fit(texts, labels)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model_name": "cignal-disaster-text-classifier",
        "model_version": "custom-v1",
        "pipeline": final_model,
        "categories": sorted(set(labels)),
        "training_samples": len(texts),
        "category_counts": counts,
        "evaluation": {
            "holdout_accuracy": round(accuracy, 4),
            "holdout_macro_f1": round(macro_f1, 4),
            "holdout_samples": len(test_labels),
            "classification_report": report,
            "test_fraction": test_fraction,
            "random_state": random_state,
        },
        "trained_at": datetime.now(UTC).isoformat(),
        "dataset_name": Path(dataset_path).name,
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        joblib.dump(payload, temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)

    return {
        "output_path": str(destination),
        "training_samples": len(texts),
        "category_counts": counts,
        "holdout_accuracy": round(accuracy, 4),
        "holdout_macro_f1": round(macro_f1, 4),
        "holdout_samples": len(test_labels),
        "model_version": payload["model_version"],
    }
