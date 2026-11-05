"""Explainable report-correlation scoring; candidates are advisory, never merged."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.intelligence.analyzer import haversine_km, similarity_score


def _value(item: Any, key: str, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def score_report_pair(report: Any, candidate: Any) -> dict:
    """Score text, category, time and geography with explicit component values."""
    report_text = f"{_value(report, 'title', '')} {_value(report, 'description', '')}".strip()
    candidate_text = f"{_value(candidate, 'title', '')} {_value(candidate, 'description', '')}".strip()
    text_score = similarity_score(report_text, candidate_text) / 100.0 if report_text and candidate_text else 0.0

    first_type = str(_value(report, "disaster_type", "unknown") or "unknown").lower()
    second_type = str(_value(candidate, "disaster_type", "unknown") or "unknown").lower()
    category_score = 1.0 if first_type == second_type and first_type not in {"unknown", "other"} else (
        0.55 if first_type in {"unknown", "other"} or second_type in {"unknown", "other"} else 0.0
    )

    distance_km = None
    lat1, lon1 = _value(report, "latitude"), _value(report, "longitude")
    lat2, lon2 = _value(candidate, "latitude"), _value(candidate, "longitude")
    if None not in (lat1, lon1, lat2, lon2):
        distance_km = haversine_km(float(lat1), float(lon1), float(lat2), float(lon2))
        geo_score = 1.0 if distance_km <= 3 else 0.8 if distance_km <= 10 else 0.45 if distance_km <= 25 else 0.0
    else:
        geo_score = 0.45  # unknown location is neutral, not evidence of a match

    first_time = _aware(_value(report, "source_observed_at") or _value(report, "created_at"))
    second_time = _aware(_value(candidate, "source_observed_at") or _value(candidate, "created_at"))
    age_hours = None
    if first_time and second_time:
        age_hours = abs((first_time - second_time).total_seconds()) / 3600.0
        time_score = 1.0 if age_hours <= 24 else 0.85 if age_hours <= 72 else 0.65 if age_hours <= 336 else 0.25 if age_hours <= 2160 else 0.0
    else:
        time_score = 0.45

    score = 0.55 * text_score + 0.15 * category_score + 0.20 * geo_score + 0.10 * time_score
    reasons = []
    if text_score >= 0.85:
        reasons.append("strong_text_similarity")
    elif text_score >= 0.65:
        reasons.append("partial_text_similarity")
    if category_score == 1.0:
        reasons.append("same_disaster_type")
    if distance_km is not None and distance_km <= 25:
        reasons.append("geographically_nearby")
    if age_hours is not None and age_hours <= 72:
        reasons.append("temporally_close")

    return {
        "candidate_id": _value(candidate, "id"),
        "score": round(score, 4),
        "text_similarity": round(text_score, 4),
        "category_score": category_score,
        "geographic_score": geo_score,
        "temporal_score": time_score,
        "distance_km": round(distance_km, 3) if distance_km is not None else None,
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "reasons": reasons,
        "automatic_duplicate_pointer_eligible": bool(score >= 0.93 and text_score >= 0.96 and (distance_km is None or distance_km <= 10)),
    }


def rank_duplicate_candidates(
    report: Any,
    candidates: Iterable[Any],
    *,
    limit: int = 10,
    minimum_score: float = 0.45,
) -> list[dict]:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if not 0 <= minimum_score <= 1:
        raise ValueError("minimum_score must be between 0 and 1")
    report_id = _value(report, "id")
    ranked = []
    for candidate in candidates:
        if _value(candidate, "id") == report_id:
            continue
        result = score_report_pair(report, candidate)
        if result["score"] >= minimum_score:
            ranked.append(result)
    ranked.sort(key=lambda item: (item["score"], item["text_similarity"]), reverse=True)
    return ranked[:limit]
