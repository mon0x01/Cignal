from __future__ import annotations

import csv
import tarfile
from pathlib import Path

import joblib

from app.core.config import settings
from app.intelligence import analyzer
from app.intelligence.training import train_text_classifier
from app.services.model_artifacts import archive_model_directory

DATA = {
    "flood": [
        "River water overflowed into homes after heavy rainfall near the bridge.",
        "Flood water submerged streets and houses in the low lying neighbourhood.",
        "Waterlogging followed the river breach and residents were evacuated.",
        "પૂરના પાણીથી ઘરો અને રસ્તાઓ ડૂબી ગયા ભારે વરસાદ પડ્યો.",
        "बाढ़ का पानी घरों और बाजार की सड़कों में भर गया है।",
    ],
    "earthquake": [
        "A strong earthquake tremor shook buildings and cracked the walls.",
        "Seismic shaking damaged structures and residents felt aftershocks.",
        "ભૂકંપના આંચકાથી મકાનો ધ્રૂજ્યા અને દીવાલોમાં તિરાડ પડી.",
        "भूकंप के झटकों से इमारतों की दीवारों में दरारें आईं।",
        "Ground shaking caused structural cracks across the residential block.",
    ],
}


def _dataset(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["category", "text"])
        writer.writeheader()
        for category, rows in DATA.items():
            for text in rows:
                writer.writerow({"category": category, "text": text})


def test_operator_trained_artifact_is_loaded_and_used(tmp_path, monkeypatch):
    dataset = tmp_path / "labels.csv"
    artifact = tmp_path / "models" / "disaster-text-v1.joblib"
    _dataset(dataset)
    train_text_classifier(dataset, artifact, min_accuracy=0.0)
    monkeypatch.setattr(settings, "ai_enable_sklearn", True)
    monkeypatch.setattr(settings, "ai_text_model_path", artifact)
    analyzer._classifier.cache_clear()
    try:
        loaded = analyzer._classifier()
        assert loaded is not None
        assert loaded["kind"] == "operator_trained_artifact"
        assert loaded["version"] == "custom-v1"
        category, score = analyzer.classify_category(
            "Flood water overflowed into residential streets after rain"
        )
        assert category in {"flood", "earthquake"}
        assert 0.0 <= score <= 1.0
    finally:
        analyzer._classifier.cache_clear()


def test_unrecognized_joblib_bundle_is_not_accepted_as_operator_model(tmp_path, monkeypatch):
    artifact = tmp_path / "untrusted.joblib"
    joblib.dump({"model_name": "unrecognized", "pipeline": object(), "categories": ["flood", "fire"]}, artifact)
    monkeypatch.setattr(settings, "ai_enable_sklearn", True)
    monkeypatch.setattr(settings, "ai_text_model_path", artifact)
    analyzer._classifier.cache_clear()
    try:
        loaded = analyzer._classifier()
        assert loaded is not None
        assert loaded["kind"] == "builtin_baseline"
    finally:
        analyzer._classifier.cache_clear()


def test_model_archive_contains_nested_artifacts_without_following_symlinks(tmp_path):
    models = tmp_path / "models"
    nested = models / "nested"
    nested.mkdir(parents=True)
    (models / "text.joblib").write_bytes(b"text-model")
    (nested / "vision.joblib").write_bytes(b"vision-model")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be archived", encoding="utf-8")
    try:
        (models / "outside-link").symlink_to(outside)
    except OSError:
        pass

    archive_path = tmp_path / "backup" / "models.tar.gz"
    included = archive_model_directory(models, archive_path)
    assert included == ["nested/vision.joblib", "text.joblib"]
    with tarfile.open(archive_path, "r:gz") as archive:
        assert sorted(archive.getnames()) == sorted(included)
        assert all(not name.startswith("/") and ".." not in Path(name).parts for name in archive.getnames())


def test_model_archive_of_missing_directory_is_valid_and_empty(tmp_path):
    archive_path = tmp_path / "models.tar.gz"
    assert archive_model_directory(tmp_path / "missing", archive_path) == []
    with tarfile.open(archive_path, "r:gz") as archive:
        assert archive.getnames() == []
