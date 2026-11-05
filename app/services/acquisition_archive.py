"""Immutable normalized observation archive and replay helpers."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AcquisitionObservation, Report


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return normalized.isoformat()
    return str(value)


def normalized_observation_payload(record: dict) -> dict:
    """Build a bounded, JSON-safe snapshot; credentials are stripped upstream."""
    source_metadata = dict(record.get("source_metadata") or {})
    # Retrieval time is observation telemetry, not provider content. Excluding it
    # keeps an identical provider record idempotent across repeated polling.
    source_metadata.pop("retrieved_at", None)
    payload = {
        "source_type": str(record["source_type"])[:24],
        "source_name": str(record["source_name"])[:100],
        "source_record_id": str(record["source_record_id"])[:180],
        "title": str(record["title"])[:180],
        "description": str(record.get("description") or "")[:10000],
        "disaster_type": str(record.get("disaster_type") or "unknown")[:32],
        "urgency": str(record.get("urgency") or "unknown")[:16],
        "latitude": record.get("latitude"),
        "longitude": record.get("longitude"),
        "location_text": str(record.get("location_text") or "")[:2000] or None,
        "source_url": str(record.get("source_url") or "")[:700] or None,
        "source_observed_at": _iso(record.get("source_observed_at")),
        "source_metadata": source_metadata,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > 24_000:
        raise ValueError("normalized_observation_exceeds_24kb")
    return payload


def capture_observation(db: Session, record: dict, *, report_id: str | None = None) -> AcquisitionObservation:
    """Insert one immutable payload version or increment its idempotent seen counter."""
    payload = normalized_observation_payload(record)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    source_type = payload["source_type"]
    source_record_id = payload["source_record_id"]
    now = datetime.now(UTC)
    existing = db.query(AcquisitionObservation).filter_by(
        source_type=source_type,
        source_record_id=source_record_id,
        content_sha256=digest,
    ).with_for_update().first()
    if existing is not None:
        existing.last_seen_at = now
        existing.seen_count = int(existing.seen_count or 0) + 1
        if report_id and not existing.report_id:
            existing.report_id = report_id
        return existing

    observed_at = record.get("source_observed_at")
    observation = AcquisitionObservation(
        id=str(uuid4()), source_type=source_type, source_record_id=source_record_id,
        content_sha256=digest, observed_at=observed_at, source_url=payload["source_url"],
        normalized_payload=payload, report_id=report_id,
        first_seen_at=now, last_seen_at=now, seen_count=1,
    )
    try:
        with db.begin_nested():
            db.add(observation)
            db.flush()
        return observation
    except IntegrityError:
        # Another worker won the unique-key race. Re-read and update the winner.
        winner = db.query(AcquisitionObservation).filter_by(
            source_type=source_type, source_record_id=source_record_id, content_sha256=digest,
        ).with_for_update().one()
        winner.last_seen_at = now
        winner.seen_count = int(winner.seen_count or 0) + 1
        if report_id and not winner.report_id:
            winner.report_id = report_id
        return winner


def report_for_observation(db: Session, observation: AcquisitionObservation) -> Report | None:
    """Resolve the linked report without rewriting immutable report content."""
    if not observation.report_id:
        return None
    return db.get(Report, observation.report_id)
