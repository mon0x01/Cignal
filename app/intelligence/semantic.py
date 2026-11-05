"""Optional multilingual report embeddings with a content-addressed database cache.

The provider reuses the configured SentenceTransformer from the existing NLP path.
It never downloads or installs a model implicitly; when the optional model path is
not enabled/available, Event Intelligence continues with the explainable lexical
baseline and records that semantic inference was unavailable.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import ReportSemanticEmbedding, utcnow

SEMANTIC_CACHE_SCHEMA = "report-embedding-cache-v1"


@dataclass(frozen=True)
class EmbeddingBatch:
    status: str
    model_version: str | None
    similarities: dict[tuple[str, str], float]
    cache_hits: int = 0
    encoded_count: int = 0
    reason: str | None = None
    dimensions: int | None = None


def normalize_report_text(report: Any) -> str:
    """Return stable text used for model input and cache invalidation."""
    values = (
        getattr(report, "title", ""),
        getattr(report, "description", ""),
        getattr(report, "location_text", ""),
    )
    text = "\n".join(re.sub(r"\s+", " ", str(value or "")).strip() for value in values)
    return text.strip()


def content_digest(report: Any) -> str:
    text = normalize_report_text(report)
    language = str(getattr(report, "language", "") or "unknown").strip().lower()
    return hashlib.sha256(f"{language}\0{text}".encode()).hexdigest()


def semantic_model_version() -> str:
    model_id = str(settings.ai_nlp_model_id or "unknown").strip()
    return f"{model_id}:event-embedding-v2"[:100]


def semantic_model_status() -> dict:
    """Describe capability without triggering model downloads or heavyweight imports."""
    if not settings.ai_enable_pretrained_nlp:
        return {
            "enabled": False,
            "available": False,
            "status": "disabled",
            "model_id": settings.ai_nlp_model_id,
            "reason": "ai_enable_pretrained_nlp_is_false",
        }
    if importlib.util.find_spec("sentence_transformers") is None:
        return {
            "enabled": True,
            "available": False,
            "status": "dependency_missing",
            "model_id": settings.ai_nlp_model_id,
            "reason": "install_optional_requirements_ml",
        }
    return {
        "enabled": True,
        "available": None,
        "status": "configured_not_loaded",
        "model_id": settings.ai_nlp_model_id,
        "reason": None,
    }


def _load_model():
    if not settings.ai_enable_pretrained_nlp:
        return None, "disabled"
    if importlib.util.find_spec("sentence_transformers") is None:
        return None, "dependency_missing"
    try:
        # The existing analyzer owns the one-entry model cache. Reusing it avoids
        # loading two large copies of the same multilingual model in CPU memory.
        from app.intelligence.analyzer import _semantic_model

        configured = _semantic_model()
        if not configured:
            return None, "model_load_failed"
        return configured[0], None
    except Exception:
        return None, "model_load_failed"


def _cosine(first: list[float], second: list[float]) -> float:
    if len(first) != len(second) or not first:
        return 0.0
    dot = sum(left * right for left, right in zip(first, second, strict=True))
    left_norm = math.sqrt(sum(value * value for value in first))
    right_norm = math.sqrt(sum(value * value for value in second))
    if left_norm <= 1e-12 or right_norm <= 1e-12:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def embed_reports(db: Session, reports: list[Any]) -> EmbeddingBatch:
    """Return pairwise cosine similarities, filling/updating cached embeddings.

    Embeddings are cached by report ID, model version and canonical text digest.
    Model inference is optional and is never attempted when the configured NLP
    capability is disabled. The caller controls commit/rollback with its session.
    """
    if len(reports) < 2:
        return EmbeddingBatch("not_enough_reports", None, {})
    model, reason = _load_model()
    if model is None:
        return EmbeddingBatch(reason or "unavailable", None, {}, reason=reason)

    try:
        import numpy as np
    except ImportError:
        return EmbeddingBatch("dependency_missing", None, {}, reason="numpy_missing")

    model_version = semantic_model_version()
    report_ids = [str(report.id) for report in reports]
    digests = {str(report.id): content_digest(report) for report in reports}
    cached_rows = (
        db.query(ReportSemanticEmbedding)
        .filter(
            ReportSemanticEmbedding.report_id.in_(report_ids),
            ReportSemanticEmbedding.model_version == model_version,
        )
        .all()
    )
    cache = {row.report_id: row for row in cached_rows}
    vectors: dict[str, list[float]] = {}
    missing: list[Any] = []
    cache_hits = 0

    for report in reports:
        report_id = str(report.id)
        row = cache.get(report_id)
        if row and row.content_sha256 == digests[report_id]:
            vector = row.embedding
            if isinstance(vector, list) and len(vector) == row.dimensions and row.dimensions > 0:
                try:
                    checked = [float(value) for value in vector]
                    if all(math.isfinite(value) for value in checked):
                        vectors[report_id] = checked
                        cache_hits += 1
                        continue
                except (TypeError, ValueError):
                    pass
        missing.append(report)

    if missing:
        texts = [normalize_report_text(report) for report in missing]
        if any(not text for text in texts):
            return EmbeddingBatch("invalid_report_text", model_version, {}, cache_hits, 0, "empty_text")
        try:
            encoded = model.encode(
                texts,
                batch_size=32,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            matrix = np.asarray(encoded, dtype=np.float32)
            if matrix.ndim != 2 or matrix.shape[0] != len(missing) or matrix.shape[1] < 8:
                raise ValueError("invalid_embedding_shape")
            if not np.isfinite(matrix).all():
                raise ValueError("non_finite_embedding")
            norms = np.linalg.norm(matrix, axis=1)
            if np.any(norms <= 1e-8):
                raise ValueError("zero_norm_embedding")
            matrix = matrix / norms[:, None]
        except Exception as exc:
            return EmbeddingBatch(
                "inference_failed", model_version, {}, cache_hits, 0, type(exc).__name__
            )

        now = utcnow()
        for report, vector_array in zip(missing, matrix, strict=True):
            report_id = str(report.id)
            vector = [round(float(value), 7) for value in vector_array.tolist()]
            vectors[report_id] = vector
            row = cache.get(report_id)
            if row is None:
                row = ReportSemanticEmbedding(
                    report_id=report_id,
                    model_version=model_version,
                    content_sha256=digests[report_id],
                    dimensions=len(vector),
                    embedding=vector,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            else:
                row.content_sha256 = digests[report_id]
                row.dimensions = len(vector)
                row.embedding = vector
                row.updated_at = now

    similarities = {}
    for first, second in combinations(reports, 2):
        key = tuple(sorted((str(first.id), str(second.id))))
        similarities[key] = _cosine(vectors[str(first.id)], vectors[str(second.id)])

    db.flush()
    return EmbeddingBatch(
        "available",
        model_version,
        similarities,
        cache_hits=cache_hits,
        encoded_count=len(missing),
        dimensions=len(next(iter(vectors.values()))) if vectors else None,
    )


def cached_similarity_map(
    db: Session,
    reports: list[Any],
    model_version: str | None,
) -> dict[tuple[str, str], float]:
    """Use already-cached embeddings for review-time metric recomputation only."""
    if not model_version or len(reports) < 2:
        return {}
    ids = [str(report.id) for report in reports]
    rows = db.query(ReportSemanticEmbedding).filter(
        ReportSemanticEmbedding.report_id.in_(ids),
        ReportSemanticEmbedding.model_version == model_version,
    ).all()
    by_id = {
        row.report_id: row.embedding
        for row in rows
        if isinstance(row.embedding, list) and len(row.embedding) == row.dimensions
    }
    return {
        tuple(sorted((str(first.id), str(second.id)))): _cosine(
            by_id[str(first.id)], by_id[str(second.id)]
        )
        for first, second in combinations(reports, 2)
        if str(first.id) in by_id and str(second.id) in by_id
    }
