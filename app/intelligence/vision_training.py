"""Offline training for the optional, human-labelled Cignal image classifier."""
from __future__ import annotations

import hashlib
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from app.intelligence.vision_features import FEATURE_VERSION, extract_visual_features

MIN_IMAGES_PER_CATEGORY = 10
MAX_TRAINING_IMAGES = 5000
ALLOWED_CATEGORIES = frozenset({
    "flood_water", "fire_smoke", "structural_damage", "landslide_debris",
    "blocked_road", "unaffected", "other",
})


class VisionTrainingDataError(ValueError):
    """Raised when a labelled image directory is unusable or too small."""


def load_labeled_image_directory(root: str | Path) -> tuple[np.ndarray, list[str], dict[str, int]]:
    """Load <root>/<category>/* images; category names are validated, files are decoded."""
    source = Path(root)
    if not source.is_dir():
        raise VisionTrainingDataError(f"Image dataset directory not found: {source}")
    features: list[np.ndarray] = []
    labels: list[str] = []
    seen: dict[str, str] = {}
    for category_dir in sorted(item for item in source.iterdir() if item.is_dir()):
        category = category_dir.name.strip().casefold()
        if category not in ALLOWED_CATEGORIES:
            raise VisionTrainingDataError(f"Unsupported image category directory: {category_dir.name!r}")
        for image_path in sorted(category_dir.rglob("*")):
            if not image_path.is_file() or image_path.is_symlink():
                continue
            if image_path.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
                continue
            try:
                content = image_path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                previous_category = seen.get(digest)
                if previous_category is not None:
                    if previous_category != category:
                        raise VisionTrainingDataError(
                            f"The same image appears under conflicting labels: {image_path.name}"
                        )
                    continue
                if len(features) >= MAX_TRAINING_IMAGES:
                    raise VisionTrainingDataError(
                        f"Image dataset exceeds the CPU/memory safety limit of {MAX_TRAINING_IMAGES} images"
                    )
                descriptor = extract_visual_features(content)
            except VisionTrainingDataError:
                raise
            except (OSError, ValueError) as exc:
                raise VisionTrainingDataError(f"Invalid image {image_path.name}: {type(exc).__name__}") from exc
            seen[digest] = category
            features.append(descriptor)
            labels.append(category)

    counts = dict(sorted(Counter(labels).items()))
    if len(counts) < 2:
        raise VisionTrainingDataError("At least two labelled image categories are required")
    insufficient = {label: count for label, count in counts.items() if count < MIN_IMAGES_PER_CATEGORY}
    if insufficient:
        details = ", ".join(f"{label}={count}" for label, count in insufficient.items())
        raise VisionTrainingDataError(
            f"Each category requires at least {MIN_IMAGES_PER_CATEGORY} unique images: {details}"
        )
    return np.stack(features), labels, counts


def train_vision_classifier(
    dataset_dir: str | Path,
    output_path: str | Path,
    *,
    min_accuracy: float = 0.60,
    test_fraction: float = 0.25,
    random_state: int = 42,
) -> dict:
    """Evaluate with stratified holdout, then refit and atomically export a trusted model."""
    if not 0.0 <= min_accuracy <= 1.0:
        raise ValueError("min_accuracy must be between 0 and 1")
    if not 0.1 <= test_fraction <= 0.4:
        raise ValueError("test_fraction must be between 0.1 and 0.4")

    features, labels, counts = load_labeled_image_directory(dataset_dir)
    try:
        import joblib
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import accuracy_score, classification_report, f1_score
        from sklearn.model_selection import train_test_split
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError("scikit-learn and joblib are required for offline image training") from exc

    train_x, test_x, train_y, test_y = train_test_split(
        features, labels, test_size=test_fraction, random_state=random_state, stratify=labels,
    )
    def make_model():
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1200, class_weight="balanced", C=1.0, random_state=random_state),
        )

    evaluation_model = make_model()
    evaluation_model.fit(train_x, train_y)
    predictions = evaluation_model.predict(test_x)
    accuracy = float(accuracy_score(test_y, predictions))
    macro_f1 = float(f1_score(test_y, predictions, average="macro", zero_division=0))
    report = classification_report(test_y, predictions, output_dict=True, zero_division=0)
    if accuracy < min_accuracy:
        raise VisionTrainingDataError(
            f"Holdout accuracy {accuracy:.3f} is below required threshold {min_accuracy:.3f}; artifact not exported"
        )

    final_model = make_model()
    final_model.fit(features, labels)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model_name": "cignal-disaster-image-classifier",
        "model_version": "hog-rgb-v1",
        "feature_version": FEATURE_VERSION,
        "classifier": final_model,
        "categories": sorted(set(labels)),
        "training_images": len(labels),
        "category_counts": counts,
        "evaluation": {
            "holdout_accuracy": round(accuracy, 4),
            "holdout_macro_f1": round(macro_f1, 4),
            "holdout_samples": len(test_y),
            "classification_report": report,
            "test_fraction": test_fraction,
            "random_state": random_state,
        },
        "trained_at": datetime.now(UTC).isoformat(),
        "dataset_name": Path(dataset_dir).name,
    }
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
        "training_images": len(labels),
        "category_counts": counts,
        "holdout_accuracy": round(accuracy, 4),
        "holdout_macro_f1": round(macro_f1, 4),
        "holdout_samples": len(test_y),
        "model_version": payload["model_version"],
    }
