"""Coalesce analyzed cross-source report changes into durable discovery runs.

This scheduler creates only candidate event hypotheses. It never creates incidents,
verifies reports, sends warnings, or starts response plans.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import EventDiscoveryRun, Report, utcnow
from app.services.job_outbox import enqueue_event_discovery

logger = logging.getLogger(__name__)
_SUPPORTED_TYPES = ("flood", "earthquake", "fire", "cyclone", "landslide")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def schedule_continuous_event_discovery(
    db: Session,
    *,
    now: datetime | None = None,
    enabled: bool | None = None,
    quiet_seconds: int | None = None,
    window_hours: int | None = None,
    max_reports: int | None = None,
) -> dict | None:
    """Atomically create one discovery run + outbox job for a settled report batch.

    The newest analyzed report identifies a batch generation. A unique dedupe key
    protects against multiple scheduler instances; the quiet period coalesces bursts.
    """
    active = settings.continuous_event_discovery_enabled if enabled is None else enabled
    if not active:
        return None

    current = _utc(now or utcnow())
    quiet = max(30, min(3600, int(
        settings.continuous_event_discovery_quiet_seconds if quiet_seconds is None else quiet_seconds
    )))
    hours = max(1, min(168, int(
        settings.continuous_event_discovery_window_hours if window_hours is None else window_hours
    )))
    limit = max(2, min(500, int(
        settings.continuous_event_discovery_max_reports if max_reports is None else max_reports
    )))
    cutoff = current - timedelta(hours=hours)
    eligible = (
        or_(
            Report.source_observed_at >= cutoff,
            and_(Report.source_observed_at.is_(None), Report.created_at >= cutoff),
        ),
        Report.analysis_revision <= Report.analyzed_revision,
        Report.status != "rejected",
        Report.latitude.is_not(None),
        Report.longitude.is_not(None),
        func.lower(Report.disaster_type).in_(_SUPPORTED_TYPES),
    )

    newest = (
        db.query(Report.id, Report.analysis_revision, Report.updated_at)
        .filter(*eligible)
        .order_by(Report.updated_at.desc(), Report.id.desc())
        .first()
    )
    if newest is None:
        return None

    newest_updated = _utc(newest.updated_at)
    if newest_updated is None or newest_updated > current - timedelta(seconds=quiet):
        return None

    origins = (
        db.query(Report.source_type, Report.source_name)
        .filter(*eligible)
        .distinct()
        .limit(2)
        .all()
    )
    normalized_origins = {
        f"{str(source_type or 'unknown').strip().lower()}:{str(source_name or 'unknown').strip().lower()}"
        for source_type, source_name in origins
    }
    if len(normalized_origins) < 2:
        return None

    fingerprint = "|".join((
        str(newest.id), str(int(newest.analysis_revision or 1)), newest_updated.isoformat(), str(hours),
    ))
    dedupe_key = "continuous:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    if db.query(EventDiscoveryRun.id).filter_by(dedupe_key=dedupe_key).first() is not None:
        return None

    parameters = {
        "window_hours": hours,
        "max_reports": limit,
        "minimum_pair_score": 0.62,
        "max_distance_km": 25.0,
        "trigger_kind": "continuous",
        "candidate_only": True,
        "human_review_required": True,
    }
    run = EventDiscoveryRun(
        id=str(uuid4()),
        requested_by=None,
        trigger_kind="continuous",
        dedupe_key=dedupe_key,
        status="queued",
        parameters=parameters,
        created_at=current,
    )
    db.add(run)
    job = enqueue_event_discovery(db, run.id, parameters)
    try:
        db.commit()
    except IntegrityError:
        # A competing scheduler committed the same generation. The unique key is
        # the authoritative cross-process guard; this is an expected no-op race.
        db.rollback()
        return None

    return {"run_id": run.id, "job_id": job.job_id, "dedupe_key": dedupe_key}
