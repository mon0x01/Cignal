from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.intelligence.training import (
    TrainingDataError,
    load_labeled_text_csv,
    train_text_classifier,
)

SAMPLES = {
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
    "fire": [
        "Large flames and black smoke spread through the warehouse building.",
        "A forest wildfire burned trees and smoke covered the nearby road.",
        "જંગલમાં આગ લાગી અને ભારે ધુમાડો ફેલાયો.",
        "दुकान में आग लगी है और ऊपरी मंजिल से धुआं निकल रहा है।",
        "Electrical sparks started a fire in the industrial transformer room.",
    ],
    "cyclone": [
        "Cyclonic winds and a storm surge affected the coastal settlement.",
        "A severe hurricane warning was issued for the coast and port.",
        "દરિયાકાંઠે વાવાઝોડાના ભારે પવન અને ઊંચા મોજાં છે.",
        "चक्रवात के कारण तटीय इलाकों में तेज हवा और ऊंची लहरें हैं।",
        "Typhoon landfall brought damaging winds to the coastal district.",
    ],
    "landslide": [
        "Mud and rocks from the hillside blocked the mountain highway.",
        "A landslide buried part of the road after prolonged rainfall.",
        "પર્વત પરથી કાટમાળ ધસી પડ્યો અને રસ્તો બંધ થયો.",
        "पहाड़ी से मलबा गिरा और सड़क पर यातायात रुक गया।",
        "Slope failure sent rocks and soil into the valley road.",
    ],
    "other": [
        "Residents need drinking water, medicine and emergency shelter supplies.",
        "A power outage affected the district and people need assistance.",
        "રાહત કેમ્પમાં દવા અને પીવાના પાણીની જરૂર છે.",
        "बिजली गुल है और परिवारों को भोजन तथा दवाइयों की जरूरत है।",
        "Medical supplies and temporary shelter are requested for displaced families.",
    ],
}


def _write_dataset(path: Path):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["text", "category"])
        writer.writeheader()
        for category, samples in SAMPLES.items():
            for sample in samples:
                writer.writerow({"text": sample, "category": category})


def test_load_labeled_csv_counts_six_disaster_categories(tmp_path):
    dataset = tmp_path / "training.csv"
    _write_dataset(dataset)
    texts, labels, counts = load_labeled_text_csv(dataset)
    assert len(texts) == 30
    assert set(labels) == set(SAMPLES)
    assert counts == {category: 5 for category in sorted(SAMPLES)}


def test_training_exports_only_after_holdout_evaluation(tmp_path):
    dataset = tmp_path / "training.csv"
    output = tmp_path / "models" / "disaster-text-v1.joblib"
    _write_dataset(dataset)
    result = train_text_classifier(dataset, output, min_accuracy=0.0)
    assert output.is_file()
    assert result["training_samples"] == 30
    assert 0 <= result["holdout_accuracy"] <= 1
    assert result["holdout_samples"] == 6
    assert result["model_version"] == "custom-v1"


def test_load_labeled_csv_rejects_too_few_samples_per_category(tmp_path):
    dataset = tmp_path / "small.csv"
    dataset.write_text(
        "text,category\n"
        "Flood water entered the street near homes,flood\n"
        "Earthquake shaking damaged walls,earthquake\n",
        encoding="utf-8",
    )
    with pytest.raises(TrainingDataError, match="at least 5 unique samples"):
        load_labeled_text_csv(dataset)


def test_load_labeled_csv_rejects_unknown_categories(tmp_path):
    dataset = tmp_path / "invalid.csv"
    dataset.write_text("text,category\nA valid description at least ten chars,hailstorm\n", encoding="utf-8")
    with pytest.raises(TrainingDataError, match="Unsupported category"):
        load_labeled_text_csv(dataset)


def test_training_deduplicates_exact_labeled_rows(tmp_path):
    dataset = tmp_path / "duplicates.csv"
    with dataset.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["text", "category"])
        writer.writeheader()
        for category, samples in SAMPLES.items():
            for sample in samples:
                writer.writerow({"text": sample, "category": category})
                writer.writerow({"text": sample, "category": category})
    texts, labels, counts = load_labeled_text_csv(dataset)
    assert len(texts) == 30
    assert all(value == 5 for value in counts.values())
