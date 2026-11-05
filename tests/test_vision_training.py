from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.core.config import settings
from app.intelligence.vision import analyze_image_bytes
from app.intelligence.vision_features import extract_visual_features
from app.intelligence.vision_training import (
    VisionTrainingDataError,
    load_labeled_image_directory,
    train_vision_classifier,
)


def _make_image(category: str, index: int) -> bytes:
    from io import BytesIO

    if category == "flood_water":
        color = (20 + index, 75 + index, 155 + index)
    else:
        color = (155 + index, 45 + index, 10 + index)
    image = Image.new("RGB", (96, 96), color)
    draw = ImageDraw.Draw(image)
    for line in range(5):
        y = 15 + line * 15
        fill = (100 + index, 190 + index, 235) if category == "flood_water" else (255, 180 + index, 30)
        draw.line((0, y, 95, y + (index % 7)), fill=fill, width=2 + (index % 3))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _dataset(root: Path, samples: int = 12) -> None:
    for category in ("flood_water", "fire_smoke"):
        target = root / category
        target.mkdir(parents=True, exist_ok=True)
        for index in range(samples):
            (target / f"{category}-{index:02d}.png").write_bytes(_make_image(category, index))


def test_visual_feature_extraction_is_deterministic_and_finite():
    content = _make_image("flood_water", 1)
    first = extract_visual_features(content)
    second = extract_visual_features(content)
    assert first.shape == second.shape
    assert first.dtype.name == "float32"
    assert (first == second).all()
    assert all(value == value for value in first)


def test_labeled_image_loader_counts_unique_images(tmp_path):
    _dataset(tmp_path)
    features, labels, counts = load_labeled_image_directory(tmp_path)
    assert features.shape[0] == 24
    assert counts == {"fire_smoke": 12, "flood_water": 12}
    assert set(labels) == set(counts)


def test_vision_training_evaluates_then_exports_a_model(tmp_path):
    dataset = tmp_path / "images"
    _dataset(dataset)
    output = tmp_path / "models" / "vision.joblib"
    result = train_vision_classifier(dataset, output, min_accuracy=0.0)
    assert output.is_file()
    assert result["training_images"] == 24
    assert result["holdout_samples"] == 6
    assert 0 <= result["holdout_accuracy"] <= 1
    assert result["model_version"] == "hog-rgb-v1"


def test_operator_vision_artifact_is_used_only_as_review_candidate(tmp_path, monkeypatch):
    import app.intelligence.vision as vision

    dataset = tmp_path / "images"
    _dataset(dataset)
    output = tmp_path / "models" / "vision.joblib"
    train_vision_classifier(dataset, output, min_accuracy=0.0)
    monkeypatch.setattr(settings, "ai_vision_model_path", output)
    vision._operator_vision_model.cache_clear()
    try:
        result = analyze_image_bytes(_make_image("flood_water", 2))
        classification = result["visual_classification"]
        assert classification["available"] is True
        assert classification["candidate_label"] in {"flood_water", "fire_smoke"}
        assert classification["human_review_required"] is True
        assert classification["verified"] is False
    finally:
        vision._operator_vision_model.cache_clear()


def test_vision_training_rejects_too_small_dataset(tmp_path):
    _dataset(tmp_path, samples=3)
    with pytest.raises(VisionTrainingDataError, match="at least 10 unique images"):
        load_labeled_image_directory(tmp_path)


def test_vision_training_rejects_conflicting_duplicate_labels(tmp_path):
    content = _make_image("flood_water", 0)
    (tmp_path / "flood_water").mkdir(parents=True)
    (tmp_path / "fire_smoke").mkdir(parents=True)
    (tmp_path / "flood_water" / "same.png").write_bytes(content)
    (tmp_path / "fire_smoke" / "same.png").write_bytes(content)
    with pytest.raises(VisionTrainingDataError, match="conflicting labels"):
        load_labeled_image_directory(tmp_path)


def test_multimodal_pipeline_exposes_image_candidates_without_changing_text_triage(tmp_path, monkeypatch):
    import app.intelligence.vision as vision
    from app.intelligence.pipeline import analyze_multimodal_report

    dataset = tmp_path / "images"
    _dataset(dataset)
    output = tmp_path / "models" / "vision.joblib"
    train_vision_classifier(dataset, output, min_accuracy=0.0)
    monkeypatch.setattr(settings, "ai_vision_model_path", output)
    vision._operator_vision_model.cache_clear()
    try:
        result = analyze_multimodal_report(
            "Flood report", "Flood water entered homes after heavy rainfall.",
            evidence_items=[{
                "id": "evidence-1", "filename": "field.png", "mime_type": "image/png",
                "content": _make_image("flood_water", 3),
            }],
        )
        assert result["fusion"]["category"] == result["text"]["category"]
        assert result["fusion"]["urgency"] == result["text"]["urgency"]
        assert result["fusion"]["visual_candidates"]
        assert result["fusion"]["visual_candidates"][0]["candidate_only"] is True
        assert result["fusion"]["visual_candidates"][0]["evidence_ids"] == ["evidence-1"]
    finally:
        vision._operator_vision_model.cache_clear()
