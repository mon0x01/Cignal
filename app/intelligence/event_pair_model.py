"""Optional operator-trained event-pair model and shared feature extraction."""
from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from rapidfuzz.fuzz import token_set_ratio

from app.core.config import settings
from app.intelligence.analyzer import haversine_km

FEATURE_NAMES = (
    "token_set_similarity",
    "char_tfidf_similarity",
    "token_jaccard",
    "numeric_token_overlap",
    "same_disaster_type",
    "geographic_proximity",
    "temporal_proximity",
    "same_language",
)
ARTIFACT_SCHEMA = "cignal-event-pair-model-v1"


def _value(item: Any, name: str, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def pair_text(item: Any) -> str:
    values = (_value(item, "title", ""), _value(item, "description", ""))
    return " ".join(re.sub(r"\s+", " ", str(value or "")).strip() for value in values).strip()


def _token_jaccard(first: str, second: str) -> float:
    left = set(re.findall(r"[\w]+", first.casefold()))
    right = set(re.findall(r"[\w]+", second.casefold()))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _numeric_overlap(first: str, second: str) -> float:
    left = set(re.findall(r"\d+(?:\.\d+)?", first))
    right = set(re.findall(r"\d+(?:\.\d+)?", second))
    if not left and not right:
        return 0.5
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _time_proximity(first: Any, second: Any) -> float:
    from datetime import UTC

    first_time = _value(first, "source_observed_at") or _value(first, "created_at")
    second_time = _value(second, "source_observed_at") or _value(second, "created_at")
    if first_time is None or second_time is None:
        return 0.5
    if first_time.tzinfo is None:
        first_time = first_time.replace(tzinfo=UTC)
    if second_time.tzinfo is None:
        second_time = second_time.replace(tzinfo=UTC)
    gap_hours = abs((first_time - second_time).total_seconds()) / 3600.0
    return math.exp(-gap_hours / 72.0)


def extract_pair_features(first: Any, second: Any, vectorizer=None) -> list[float]:
    first_text = pair_text(first)
    second_text = pair_text(second)
    token_score = token_set_ratio(first_text, second_text) / 100.0 if first_text and second_text else 0.0
    if vectorizer is None:
        tfidf_score = 0.0
    else:
        transformed = vectorizer.transform([first_text, second_text])
        tfidf_score = float((transformed[0] @ transformed[1].T).toarray()[0][0])
    first_type = str(_value(first, "disaster_type", "unknown") or "unknown").lower()
    second_type = str(_value(second, "disaster_type", "unknown") or "unknown").lower()
    same_type = float(first_type == second_type and first_type not in {"unknown", "other"})
    lat1, lon1 = _value(first, "latitude"), _value(first, "longitude")
    lat2, lon2 = _value(second, "latitude"), _value(second, "longitude")
    if None in (lat1, lon1, lat2, lon2):
        geo_proximity = 0.5
    else:
        distance = haversine_km(float(lat1), float(lon1), float(lat2), float(lon2))
        geo_proximity = math.exp(-distance / 25.0)
    first_language = str(_value(first, "language", "unknown") or "unknown").lower()
    second_language = str(_value(second, "language", "unknown") or "unknown").lower()
    same_language = float(first_language == second_language and first_language != "unknown")
    return [
        token_score,
        tfidf_score,
        _token_jaccard(first_text, second_text),
        _numeric_overlap(first_text, second_text),
        same_type,
        geo_proximity,
        _time_proximity(first, second),
        same_language,
    ]


@lru_cache(maxsize=4)
def _load_artifact_cached(path_text: str, modified_ns: int, file_size: int):
    """Load one immutable artifact version, keyed by its filesystem identity."""
    if not settings.ai_enable_sklearn:
        return None
    path = Path(path_text)
    try:
        import joblib

        payload = joblib.load(path)
        if payload.get("schema") != ARTIFACT_SCHEMA:
            return None
        if tuple(payload.get("feature_names", ())) != FEATURE_NAMES:
            return None
        if not callable(getattr(payload.get("classifier"), "predict_proba", None)):
            return None
        if not callable(getattr(payload.get("vectorizer"), "transform", None)):
            return None
        return payload
    except Exception:
        return None


def _load_artifact():
    """Reload automatically when training atomically replaces the model file.

    A no-argument lru_cache would cache a missing artifact forever in long-lived
    workers. Including mtime and size allows a new model to become visible
    without restarting the API or worker process.
    """
    if not settings.ai_enable_sklearn:
        return None
    path = Path(getattr(settings, "event_pair_model_path", Path("./data/models/event-pair-v1.joblib")))
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return _load_artifact_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def event_pair_model_status() -> dict:
    path = Path(getattr(settings, "event_pair_model_path", Path("./data/models/event-pair-v1.joblib")))
    payload = _load_artifact()
    return {
        "available": payload is not None,
        "path_configured": str(path),
        "model_version": payload.get("model_version") if payload else None,
        "artifact_schema": ARTIFACT_SCHEMA,
        "interpretation": "Uncalibrated supervised classification score; never an emergency probability.",
    }


def score_event_pair_model(first: Any, second: Any) -> dict | None:
    payload = _load_artifact()
    if payload is None:
        return None
    try:
        import numpy as np

        features = extract_pair_features(first, second, payload["vectorizer"])
        matrix = np.asarray([features], dtype=np.float64)
        classes = list(payload["classifier"].classes_)
        if 1 not in classes:
            return None
        positive_index = classes.index(1)
        score = float(payload["classifier"].predict_proba(matrix)[0][positive_index])
        if not math.isfinite(score):
            return None
        return {
            "score": max(0.0, min(1.0, score)),
            "model_version": str(payload.get("model_version", "unknown")),
            "feature_names": list(FEATURE_NAMES),
        }
    except Exception:
        return None


def pair_content_hash(text_a: str, text_b: str) -> str:
    first, second = sorted((text_a.strip(), text_b.strip()))
    return hashlib.sha256(f"{first}\0{second}".encode()).hexdigest()
