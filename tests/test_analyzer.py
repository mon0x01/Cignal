from __future__ import annotations

from app.core.config import settings
from app.intelligence.analyzer import (
    _classifier,
    _semantic_model,
    analyze_report,
    classify_category,
    classify_urgency,
    extract_location,
    haversine_km,
    similarity_score,
)


def _rules_only(monkeypatch):
    monkeypatch.setattr(settings, "ai_enable_sklearn", False)
    monkeypatch.setattr(settings, "ai_enable_pretrained_nlp", False)
    _classifier.cache_clear()
    _semantic_model.cache_clear()


def test_rule_fallback_classifies_disaster_and_urgency(monkeypatch):
    _rules_only(monkeypatch)
    assert classify_category("Flood water is rising near the homes") == ("flood", 0.55)
    urgency, confidence = classify_urgency("People are trapped and need rescue")
    assert urgency == "critical"
    assert confidence == 0.84


def test_extract_location_from_coordinates_and_city():
    assert extract_location("Reported at 23.0225, 72.5714")[:2] == (23.0225, 72.5714)
    lat, lon, label = extract_location("Flooding reported in Ahmedabad")
    assert (lat, lon, label) == (23.0225, 72.5714, "Ahmedabad, Gujarat")
    assert extract_location("Unknown village") == (None, None, None)


def test_haversine_and_similarity():
    distance = haversine_km(23.0225, 72.5714, 23.0225, 72.5714)
    assert distance == 0
    assert 0 < haversine_km(23.0225, 72.5714, 21.1702, 72.8311) < 250
    assert similarity_score("flood water rising", "flood water rising") == 100
    assert similarity_score("flood water rising", "earthquake tremor") < 100


def test_analysis_is_advisory_and_preserves_language(monkeypatch):
    _rules_only(monkeypatch)
    result = analyze_report("Flood in Ahmedabad", "Water rising, people trapped", "gu")
    assert result["category"] == "flood"
    assert result["urgency"] == "critical"
    assert result["latitude"] == 23.0225
    assert result["language"] == "gu"
    assert "Human review required" in result["summary"]
