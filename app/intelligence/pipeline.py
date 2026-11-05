"""Orchestrate advisory text, image and location analysis for disaster reports."""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from datetime import UTC, datetime

from app.core.config import settings
from app.intelligence.analyzer import analyze_report
from app.intelligence.vision import ImageEvidenceError, analyze_image_bytes, hash_distance

PIPELINE_VERSION = "cignal-multimodal-v1"


def _pairwise_image_duplicates(images: list[dict], max_distance: int = 7) -> list[dict]:
    matches = []
    for index, first in enumerate(images):
        first_hash = (first.get("quality") or {}).get("average_hash")
        if not first_hash:
            continue
        for second in images[index + 1:]:
            second_hash = (second.get("quality") or {}).get("average_hash")
            if not second_hash:
                continue
            distance = hash_distance(first_hash, second_hash)
            if distance <= max_distance:
                matches.append({
                    "first_evidence_id": first.get("evidence_id"),
                    "second_evidence_id": second.get("evidence_id"),
                    "hash_distance": distance,
                    "candidate_only": True,
                })
    return matches



def _aggregate_visual_candidates(images: list[dict]) -> list[dict]:
    """Group image-model suggestions by label without treating scores as probabilities."""
    grouped: dict[str, dict] = {}
    for image in images:
        classification = image.get("visual_classification") or {}
        if not classification.get("available"):
            continue
        label = classification.get("candidate_label")
        score = classification.get("candidate_score")
        if not isinstance(label, str) or not isinstance(score, (int, float)):
            continue
        bucket = grouped.setdefault(label, {"label": label, "evidence_ids": [], "scores": []})
        evidence_id = image.get("evidence_id")
        if evidence_id:
            bucket["evidence_ids"].append(evidence_id)
        bucket["scores"].append(float(score))
    return [
        {
            "label": row["label"],
            "evidence_ids": row["evidence_ids"],
            "image_count": len(row["scores"]),
            "mean_candidate_score": round(sum(row["scores"]) / len(row["scores"]), 4),
            "candidate_only": True,
            "human_review_required": True,
        }
        for row in sorted(grouped.values(), key=lambda item: len(item["scores"]), reverse=True)
    ]

def analyze_multimodal_report(
    title: str,
    description: str,
    declared_language: str = "en",
    evidence_items: Iterable[dict] | None = None,
    *,
    submitted_latitude: float | None = None,
    submitted_longitude: float | None = None,
    submitted_location_text: str | None = None,
) -> dict:
    """Return versioned, JSON-safe, human-reviewable analysis without auto-verification."""
    text = analyze_report(title, description, declared_language)
    images: list[dict] = []
    skipped_non_images = 0
    skipped_image_limit = 0
    failures = 0
    images_seen = 0
    max_images = max(1, min(32, int(settings.ai_max_images_per_report)))
    for evidence in evidence_items or []:
        mime_type = str(evidence.get("mime_type") or "")
        if not mime_type.startswith("image/"):
            skipped_non_images += 1
            continue
        images_seen += 1
        if images_seen > max_images:
            skipped_image_limit += 1
            continue
        try:
            content = evidence.get("content", b"")
            expected_sha256 = evidence.get("expected_sha256")
            expected_size = evidence.get("expected_size")
            actual_sha256 = hashlib.sha256(content).hexdigest() if isinstance(content, bytes) and content else None
            actual_size = len(content) if isinstance(content, bytes) else -1
            if expected_sha256 and (
                not isinstance(expected_sha256, str)
                or not hmac.compare_digest(expected_sha256, actual_sha256 or "")
            ):
                failures += 1
                images.append({
                    "available": False,
                    "evidence_id": str(evidence.get("id") or ""),
                    "filename": str(evidence.get("filename") or "evidence")[:255],
                    "reason": "integrity_hash_mismatch",
                    "expected_sha256": str(expected_sha256)[:64],
                    "actual_sha256": actual_sha256,
                })
                continue
            if expected_size is not None and expected_size != actual_size:
                failures += 1
                images.append({
                    "available": False,
                    "evidence_id": str(evidence.get("id") or ""),
                    "filename": str(evidence.get("filename") or "evidence")[:255],
                    "reason": "integrity_size_mismatch",
                    "expected_size": expected_size,
                    "actual_size": actual_size,
                })
                continue
            result = analyze_image_bytes(
                content,
                stored_metadata=evidence.get("stored_metadata") or {},
            )
            result["evidence_id"] = str(evidence.get("id") or "")
            result["filename"] = str(evidence.get("filename") or "evidence")[:255]
            images.append(result)
        except ImageEvidenceError as exc:
            failures += 1
            images.append({
                "available": False,
                "evidence_id": str(evidence.get("id") or ""),
                "filename": str(evidence.get("filename") or "evidence")[:255],
                "reason": str(exc),
            })
        except Exception as exc:
            failures += 1
            images.append({
                "available": False,
                "evidence_id": str(evidence.get("id") or ""),
                "filename": str(evidence.get("filename") or "evidence")[:255],
                "reason": type(exc).__name__,
            })

    image_matches = _pairwise_image_duplicates([image for image in images if image.get("available")])
    if (submitted_latitude is None) != (submitted_longitude is None):
        raise ValueError("Submitted latitude and longitude must be provided together")
    if submitted_latitude is not None and not (-90 <= submitted_latitude <= 90 and -180 <= submitted_longitude <= 180):
        raise ValueError("Submitted coordinates are outside valid geographic bounds")

    if submitted_latitude is not None:
        location = {
            "latitude": float(submitted_latitude),
            "longitude": float(submitted_longitude),
            "location_text": submitted_location_text,
            "source": "citizen_or_source_submission",
            "confidence": 1.0,
        }
    elif text.get("latitude") is not None:
        location = {
            "latitude": text.get("latitude"),
            "longitude": text.get("longitude"),
            "location_text": text.get("location_text") or submitted_location_text,
            "source": "report_text_coordinates",
            "confidence": 0.9,
        }
    else:
        location = {
            "latitude": None, "longitude": None,
            "location_text": submitted_location_text,
            "source": "submitted_location_text" if submitted_location_text else None,
            "confidence": 0.35 if submitted_location_text else 0.0,
        }

    exif_locations = [
        image["exif_location"] for image in images if image.get("exif_location")
    ]
    geo_conflicts = []
    if location["latitude"] is None and exif_locations:
        gps = exif_locations[0]
        location = {
            "latitude": gps["latitude"],
            "longitude": gps["longitude"],
            "location_text": "Image EXIF coordinates; requires human confirmation",
            "source": gps["source"],
            "confidence": gps["confidence"],
        }
    elif location["latitude"] is not None:
        from app.intelligence.analyzer import haversine_km
        for gps in exif_locations:
            distance = haversine_km(
                location["latitude"], location["longitude"],
                gps["latitude"], gps["longitude"],
            )
            if distance > 1.0:
                geo_conflicts.append({
                    "type": "report_location_vs_image_exif",
                    "distance_km": round(distance, 3),
                    "candidate_location": gps,
                    "resolution": "submitted_or_text_location_preserved_for_human_review",
                })

    modality_coverage = ["text"]
    if any(image.get("available") for image in images):
        modality_coverage.append("image")
    visual_candidates = _aggregate_visual_candidates(images)
    if location["latitude"] is not None:
        modality_coverage.append("geospatial")

    quality_warnings = []
    for image in images:
        for flag in image.get("quality_flags", []):
            quality_warnings.append({"evidence_id": image.get("evidence_id"), "flag": flag})

    return {
        "schema_version": "1.0",
        "pipeline_version": PIPELINE_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "advisory_only": True,
        "human_review_required": True,
        "text": text,
        "vision": {
            "images_received": len(images),
            "images_analyzed": sum(1 for item in images if item.get("available")),
            "images_failed": failures,
            "images_skipped_limit": skipped_image_limit,
            "max_images_per_report": max_images,
            "non_image_evidence_skipped": skipped_non_images,
            "images": images,
            "near_duplicate_image_candidates": image_matches,
            "quality_warnings": quality_warnings,
            "model_scope": "image integrity, quality and optional operator-trained candidate classification; not autonomous damage verification",
        },
        "geospatial": location,
        "fusion": {
            "category": text["category"],
            "urgency": text["urgency"],
            "confidence": text["confidence"],
            "modality_coverage": modality_coverage,
            "visual_candidates": visual_candidates,
            "conflicts": geo_conflicts,
            "policy": "Text remains the primary triage signal; vision suggestions are candidate-only and do not independently raise urgency or verify an incident.",
        },
        "summary": text["summary"],
    }
